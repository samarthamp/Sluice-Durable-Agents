"""``sluice smoke``: layer-by-layer check of a running distributed topology.

Talks to the services at their configured URLs (``SLUICE_*_URL``, default
127.0.0.1:8100-8103) and to Redis at ``REDIS_URL``.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[status]
    print(f"[{mark}] {name}" + (f"\n           {detail}" if detail else ""), flush=True)


def _key(workflow: str) -> str:
    from ..core.types import effect_key

    return effect_key(workflow, "br-smoke-" + uuid.uuid4().hex[:6], 4)


# ------------------------------------------------------------------ dependencies


def check_deps() -> bool:
    """Is the package installed? Answered WITHOUT executing it."""
    import importlib.util

    ok = True
    for mod, why, required in (
        ("fastapi", "effect services", True),
        ("uvicorn", "serving them", True),
        ("httpx", "HttpWorld client", True),
        ("redis", "stream ingest (optional)", False),
    ):
        try:
            found = importlib.util.find_spec(mod) is not None
        except Exception as e:
            found, why = False, f"{why} -- {type(e).__name__}: {e}"

        if found:
            check(f"{mod} installed", PASS, why)
        elif required:
            check(f"{mod} installed", FAIL, f'pip install -e ".[http,redis]"  ({why})')
            ok = False
        else:
            check(f"{mod} installed", SKIP, f"not installed; {why}")

    return ok


# ------------------------------------------------------------------------- HTTP


def check_http() -> None:
    try:
        from ..world.http_client import HttpLedger, HttpWorld
    except ImportError as e:
        check("http topology", SKIP, f"{e}")
        return

    world = HttpWorld()
    ledger = HttpLedger()

    health = world.health()
    down = [s for s, v in health["services"].items() if v != "up"]
    if down:
        check("services reachable", FAIL,
              f"down: {down}.  start them:  sluice services")
        return
    check("services reachable", PASS, f"{sorted(health['services'])} all up")

    # Separate OS processes, not threads in one interpreter.
    pids = {}
    for name in ("ticket", "channel", "pager", "ledger"):
        try:
            r = world.client.get(f"{world.urls[name].rstrip('/')}/health", timeout=2.0)
            pids[name] = r.json().get("pid")
        except Exception:
            pids[name] = None
    distinct = len({p for p in pids.values() if p})
    if distinct >= 4:
        check("separate OS processes", PASS, f"distinct pids {pids}")
    else:
        check("separate OS processes", FAIL,
              f"expected 4 distinct pids, got {pids}."
              " `sluice services` (without --service) spawns subprocesses")

    world.reset()
    ledger.reset()

    wf = "wf-smoke" + uuid.uuid4().hex[:8]

    # --- effect round trip over a real socket
    k = _key(wf)
    res = world.execute("create_ticket", {"service": "payments-api"}, k, 1, 2.0)
    if res.status == "ok":
        check("execute over HTTP", PASS, "create_ticket committed on the ticket service")
    else:
        check("execute over HTTP", FAIL, f"{res.status}: {res.error}")
        return

    # --- idempotency across a real network boundary
    again = world.execute("create_ticket", {"service": "payments-api"}, k, 1, 2.0)
    rows = ledger.effects(tool_name="create_ticket", workflow_id=wf)
    if again.status == "ok" and len(rows) == 1:
        check("idempotency key collapses a retry", PASS,
              "two requests, one ledger entry -- this is what makes crash recovery safe")
    else:
        check("idempotency key collapses a retry", FAIL,
              f"retry={again.status}, ledger rows={len(rows)} (expected 1)")

    # --- ground truth is a separate process and it saw the effect
    if rows and rows[0]["workflow_id"] == wf:
        check("ground-truth ledger recorded it", PASS,
              "the oracle is a different process from the one that acted")
    else:
        check("ground-truth ledger recorded it", FAIL, f"rows={rows}")

    # --- compensation
    comp = world.compensate("create_ticket", {"service": "payments-api"}, k, 1, 2.0)
    comps = ledger.compensations(workflow_id=wf)
    if comp.status == "ok" and comps:
        check("compensate over HTTP", PASS, "close_ticket recorded against the same key")
    else:
        check("compensate over HTTP", FAIL, f"{comp.status}: {comp.error}")

    # --- probe: the type parameter, not an assumption
    k2 = _key(wf)
    world.execute("post_to_channel", {"incident": "inc-smoke"}, k2, 1, 2.0)
    obs = world.probe("post_to_channel", k2, 2.0)
    unobs = world.probe("page_oncall", _key(wf), 2.0)
    if obs == "done" and unobs == "unknown":
        check("probe honours observability", PASS,
              "observable effect probes 'done'; the pager cannot be queried at all")
    else:
        check("probe honours observability", FAIL,
              f"observable={obs} (want done), unobservable={unobs} (want unknown)")

    # --- fencing, enforced by the service and not by the caller's good manners
    kf = _key(wf)
    world.execute("create_ticket", {"service": "x"}, kf, 5, 2.0)
    stale = world.execute("create_ticket", {"service": "x"}, _key(wf), 2, 2.0)
    if stale.status == "failed" and "epoch" in (stale.error or "").lower():
        check("stale epoch fenced by the service", PASS,
              f"HTTP 409 -- {stale.error}")
    else:
        check("stale epoch fenced by the service", FAIL,
              f"expected a refusal, got {stale.status}: {stale.error}")

    # --- a real socket timeout, not a simulated one
    world.set_faults("pager", {"latency_s": 1.5})
    t0 = time.time()
    slow = world.execute("page_oncall", {"rota": "rota-Y"}, _key(wf), 9, 0.4)
    elapsed = time.time() - t0
    world.set_faults("pager", {"latency_s": 0.0})
    if slow.status == "unknown":
        check("real read timeout yields 'unknown'", PASS,
              f"gave up after {elapsed:.2f}s with the request in flight;"
              " slow and crashed are indistinguishable, so the answer is not 'failed'")
    else:
        check("real read timeout yields 'unknown'", FAIL,
              f"got {slow.status} after {elapsed:.2f}s: {slow.error}")

    # --- a dead port is a definite failure, not an ambiguity
    from ..world.http_client import HttpWorld as _HW

    dead = _HW(urls={**world.urls, "ticket": "http://127.0.0.1:9"})
    refused = dead.execute("create_ticket", {"service": "x"}, _key(wf), 9, 1.0)
    dead.close()
    if refused.status == "failed":
        check("refused connection is 'failed', not 'unknown'", PASS,
              "the request never left, so nothing committed -- calling this ambiguous"
              " would manufacture escalations out of a closed port")
    else:
        check("refused connection is 'failed', not 'unknown'", FAIL,
              f"got {refused.status}: {refused.error}")

    world.reset()
    ledger.reset()
    world.close()
    ledger.close()


# ------------------------------------------------------------------------ Redis


def check_redis() -> None:
    try:
        from ..core.types import Alert
        from ..ingest import RedisAlertSource
    except ImportError as e:
        check("redis ingest", SKIP, f"{e}")
        return

    stream = "alerts:smoke:" + uuid.uuid4().hex[:8]
    group = "smoke-group"

    try:
        a = RedisAlertSource(stream=stream, group=group, consumer="smoke-a", block_ms=200)
    except Exception as e:
        check("redis reachable", SKIP,
              f"{e}\n           optional -- start it with:  docker compose up -d")
        return
    check("redis reachable", PASS, f"consumer group {group} on {stream}")

    alert = Alert("a-smoke-1", "payments-api", "prometheus", {"msg": "smoke"})
    a.publish(alert)
    a.publish(alert)  # at-least-once, deliberately

    got = list(a.consume())
    if len(got) == 2 and all(g.alert_id == "a-smoke-1" for g in got):
        check("consumer group delivers", PASS,
              "the same alert twice -- absorbed downstream by workflow_id derivation,"
              " not by the step-4 marker")
    else:
        check("consumer group delivers", FAIL, f"received {[g.alert_id for g in got]}")

    pending = a.pending()
    if len(pending) == 2:
        check("pending entries visible", PASS,
              f"{len(pending)} delivered-but-unacked -- the recovery signal after a"
              " node dies")
    else:
        check("pending entries visible", FAIL, f"expected 2 pending, got {len(pending)}")

    a.ack("a-smoke-1")

    # A second consumer takes over what the first never acknowledged.
    b = RedisAlertSource(stream=stream, group=group, consumer="smoke-b", block_ms=200)
    time.sleep(0.3)
    claimed = b.claim_stale(min_idle_ms=100)
    if claimed:
        check("stale entries reclaimed by a second consumer", PASS,
              f"XAUTOCLAIM moved {len(claimed)} entry(s) to smoke-b -- node loss is"
              " recoverable, not merely observable")
        for c in claimed:
            b.ack(c.alert_id)
    else:
        check("stale entries reclaimed by a second consumer", FAIL,
              "XAUTOCLAIM returned nothing; orphaned alerts would be stranded")

    stats = b.stats()
    check("stream stats readable", PASS,
          f"depth={stats['depth']} lag={stats['lag']} pending={len(stats['pending'])}")

    try:
        a.r.delete(stream)
    except Exception:
        pass
    a.close()
    b.close()


# ------------------------------------------------------------- end-to-end shape


def check_end_to_end() -> None:
    """One real workflow, real sockets, checked against the out-of-process oracle."""
    try:
        from ..scenarios import ALERT, make_ctx, scenario_poison
        from ..verification.checker import check_eeo
    except ImportError as e:
        check("end-to-end over HTTP", SKIP, str(e))
        return

    try:
        ctx = make_ctx(".sluice/smoke.db", world_kind="http")
    except Exception as e:
        check("end-to-end over HTTP", FAIL, f"could not build an HTTP context: {e}")
        return

    try:
        res = scenario_poison(ctx, "sluice", {})
        board = res["state"]["scoreboard"]
        got = (board["tickets"], board["posts"], board["pages"])
        verdict = check_eeo(ctx.journal, ctx.ledger, res["workflow_id"],
                            outcome=res["result"].get("outcome", ""))
        if got == (1, 1, 1) and verdict["pass"]:
            check("poison step end-to-end over HTTP", PASS,
                  f"1/1/1, rota-Y paged once, EEO clean -- across four processes"
                  f" and {ALERT.service}'s eight effects")
        else:
            check("poison step end-to-end over HTTP", FAIL,
                  f"scoreboard {got} (want 1/1/1), EEO pass={verdict['pass']},"
                  f" unexplained={verdict['unexplained']}")
    except Exception as e:
        check("poison step end-to-end over HTTP", FAIL, f"{type(e).__name__}: {e}")
    finally:
        ctx.close()


def main(argv=None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Sluice topology smoke test")
    ap.add_argument("--http", action="store_true", help="only the effect services")
    ap.add_argument("--redis", action="store_true", help="only the stream")
    ap.add_argument("--skip-deps", action="store_true",
                    help="skip the installed-packages check")
    args = ap.parse_args(argv)

    only_http = args.http and not args.redis
    only_redis = args.redis and not args.http
    run_all = not (args.http or args.redis)

    print("\nSluice topology smoke\n" + "-" * 60)

    try:
        if run_all and not args.skip_deps:
            check_deps()

        if run_all or only_http:
            print("\nHTTP effect layer")
            check_http()

        if run_all or only_redis:
            print("\nRedis ingest")
            check_redis()

        if run_all or only_http:
            print("\nEnd to end")
            check_end_to_end()
    except KeyboardInterrupt:
        # Partial results are still worth printing; an interrupted check is usually a hang.
        print("\n  interrupted -- results so far:")

    failed = [r for r in _results if r[0] == FAIL]
    skipped = [r for r in _results if r[0] == SKIP]
    print("\n" + "-" * 60)
    print(f"  {len(_results) - len(failed) - len(skipped)} passed,"
          f" {len(failed)} failed, {len(skipped)} skipped")
    for _s, name, detail in failed:
        print(f"    FAIL  {name}: {detail.splitlines()[0] if detail else ''}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
