"""``sluice orchestrator``: one orchestrator, as its own OS process (3.1, 3.4).

Run two with different ``--owner`` values against the same ``--db`` for a real leader race.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from ..core.engine import LEASE_TTL_S, recover
from ..core.journal import Journal
from ..core.tools import DEMO_ALERT
from ..core.types import Alert, workflow_id_for
from ..ingest import alert_source
from ..verification.checker import check_eeo, explain, write_verdict

_STOP = False


def _install_signal_handlers() -> dict:
    """Stop after the current alert on SIGINT/SIGTERM. Returns the handlers it replaced."""

    def stop(_sig, _frm):
        global _STOP
        _STOP = True
        print("\n  [stopping after the current alert]", flush=True)

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, stop)
        except (ValueError, OSError):  # not the main thread, or not supported here
            pass
    return previous


def build_world(kind: str, ledger_holder: dict):
    if kind == "http":
        from ..world.http_client import HttpLedger, HttpWorld

        world = HttpWorld()
        health = world.health()
        down = [s for s, v in health["services"].items() if v != "up"]
        if down:
            print(f"  WARNING: services down: {down}. Start them: sluice services")
        ledger_holder["ledger"] = HttpLedger()
        return world

    from ..world import FaultConfig, GroundTruthLedger, InProcessWorld

    ledger = GroundTruthLedger()
    ledger_holder["ledger"] = ledger
    return InProcessWorld(ledger, FaultConfig())


def handle(alert: Alert, journal, world, args, ledger) -> dict:
    def narrate(ev: dict) -> None:
        print(
            f"    [{args.owner}] {ev['kind']}"
            f" {ev.get('tool') or ev.get('reason') or ''}",
            flush=True,
        )

    started = time.time()
    out = recover(
        journal,
        world,
        alert,
        args.severity,
        mode=args.mode,
        owner=args.owner,
        max_attempts=args.max_attempts,
        lease_ttl_s=args.lease_ttl,
        on_event=narrate if args.narrate else None,
    )
    elapsed = time.time() - started

    outcome = out.get("outcome")
    if outcome == "not_leader":
        # The other orchestrator holds the lease.
        print(f"  {alert.alert_id}: not leader ({out.get('reason', '')[:60]})", flush=True)
        return out

    verdict = check_eeo(journal, ledger, out.get("workflow_id", ""), outcome=outcome or "")
    # Beside the journal, for the dashboard's live pane.
    write_verdict(args.db, verdict, mode=args.mode)
    flag = "PASS" if verdict["pass"] else "FAIL"
    print(
        f"  {alert.alert_id}: {outcome:<11} rota={out.get('rota') or '-':<8}"
        f" {elapsed * 1000:7.0f}ms  EEO {flag}",
        flush=True,
    )
    if not verdict["pass"]:
        print(explain(verdict), flush=True)
    return out


def main(argv=None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Sluice orchestrator process")
    ap.add_argument("--owner", default="orch-a",
                    help="lease owner id; make it unique (default: %(default)s)")
    ap.add_argument("--db", default=".sluice/shared.db",
                    help="shared journal; both orchestrators must point at the same file"
                         " (default: %(default)s)")
    ap.add_argument("--world", default="http", choices=["http", "inprocess"],
                    help="effect layer: the HTTP services, or in-process stubs"
                         " (default: %(default)s)")
    ap.add_argument("--source", default="redis", choices=["redis", "inprocess"],
                    help="where alerts come from (default: %(default)s)")
    ap.add_argument("--mode", default="sluice", choices=["sluice", "pinned", "naive"],
                    help="recovery strategy (default: %(default)s)")
    ap.add_argument("--severity", default="P2",
                    help="the scripted agent's first classification (default: %(default)s)")
    ap.add_argument("--lease-ttl", type=float, default=LEASE_TTL_S,
                    help="seconds before a silent leader's lease can be taken"
                         " (default: %(default)s)")
    ap.add_argument("--max-attempts", type=int, default=4,
                    help="recovery attempts per alert before giving up (default: %(default)s)")
    ap.add_argument("--claim-idle-ms", type=int, default=8000,
                    help="reclaim entries a dead consumer left pending for this long"
                         " (default: %(default)s)")
    ap.add_argument("--poll-s", type=float, default=1.0,
                    help="sleep between empty polls of the stream (default: %(default)s)")
    ap.add_argument("--once", action="store_true", help="drain what is queued, then exit")
    ap.add_argument("--narrate", action="store_true",
                    help="print every engine event as it happens")
    ap.add_argument("--empty-rota", default="rota-X",
                    help="poison the demo rota (in-process world only; for the HTTP world use"
                         " `sluice faults poison`); '' to disable (default: %(default)s)")
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)

    holder: dict = {}
    world = build_world(args.world, holder)
    ledger = holder["ledger"]

    if args.world == "inprocess" and args.empty_rota:
        world.faults.empty_rotas = {args.empty_rota}

    journal = Journal(args.db)
    source = alert_source(
        use_redis=(args.source == "redis"), consumer=args.owner, verbose=True
    )

    print(f"\n  orchestrator {args.owner}  pid {os.getpid()}")
    print(f"  journal  {args.db}")
    print(f"  world    {args.world}   source {getattr(source, 'name', args.source)}")
    print(f"  mode     {args.mode}   lease ttl {args.lease_ttl}s\n", flush=True)

    global _STOP
    _STOP = False
    previous_handlers = _install_signal_handlers()

    handled = 0
    try:
        while not _STOP:
            work = 0

            # Entries some other consumer claimed and never acknowledged.
            for alert in source.claim_stale(args.claim_idle_ms):
                print(f"  reclaimed {alert.alert_id} from a stalled consumer", flush=True)
                out = handle(alert, journal, world, args, ledger)
                if out.get("outcome") != "not_leader":
                    source.ack(alert.alert_id)
                work += 1
                handled += 1

            for alert in source.consume():
                out = handle(alert, journal, world, args, ledger)
                # Ack only after the handler returns.
                if out.get("outcome") != "not_leader":
                    source.ack(alert.alert_id)
                work += 1
                handled += 1
                if _STOP:
                    break

            if args.once and work == 0:
                break
            if work == 0:
                time.sleep(args.poll_s)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        stats = source.stats() if hasattr(source, "stats") else {}
        print(f"\n  {args.owner}: handled {handled} alert(s)")
        if stats:
            print(f"  stream depth {stats.get('depth')}  lag {stats.get('lag')}"
                  f"  pending {len(stats.get('pending') or [])}")
        lease = journal.lease_info(workflow_id_for(DEMO_ALERT.alert_id))
        if lease:
            print(f"  last demo-workflow lease: {lease.owner} @ epoch {lease.epoch}")
        journal.close()
        source.close()
        if hasattr(world, "close"):
            world.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
