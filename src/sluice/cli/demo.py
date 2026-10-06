"""``sluice demo``: the three-pane scenarios, the crash sweep, failover, the benchmark."""

from __future__ import annotations

import argparse
import os
import sys
import time

from ..observability.audit import write_post_mortem
from ..observability.view import render_terminal
from ..scenarios import MODES, SCENARIOS, make_ctx
from ..verification.checker import evidence_table, explain

BAR = "=" * 78

# Events worth narrating while --slow is pacing the run.  The rest are noise on stage.
NARRATE = {
    "intent": lambda e: f"  -> {e['tool']}({_args(e.get('args'))})",
    "result": lambda e: f"     {e['tool']}: {e['status']}"
                        + (f" -- {e['error']}" if e.get("error") else ""),
    "step_replayed": lambda e: f"     {e['tool']}: replayed from the journal",
    "probed": lambda e: f"     probe {e['tool']}: {e['probe']}",
    "forked": lambda e: f"  ** FORK to {e['severity']} at depth {e['depth']}"
                        f" -- {e['reason']}",
    "branch_abandoned": lambda e: f"  ** branch {e['status']}"
                                  + (f", residue {e['residue']}" if e["residue"] else ""),
    "barrier_blocked": lambda e: f"  ## GATE HOLDS: {e['reason']} {e['effects']}",
    "compensated": lambda e: f"     undo {e['tool']} ok  ({e['remaining']} left)",
    "compensation_failed": lambda e: f"     undo {e['tool']} FAILED"
                                     f" (attempt {e['attempt']}) -- {e['error']}",
    "compensation_deadline": lambda e: "  ## compensation budget exhausted",
    "barrier_released": lambda e: "  ## GATE LIFTS: cleanup proven",
    "late_delivery_suppressed": lambda e: f"  ** zombie {e['tool']} caught on the key",
    "escalated": lambda e: f"  ## ESCALATED: {e['reason']}",
    "replay_failed": lambda e: f"  ## replay failed again: {e['reason']}",
}


def _args(args) -> str:
    if not args:
        return ""
    keep = ("severity", "rota", "service", "incident", "source")
    return ", ".join(f"{k}={args[k]}" for k in keep if k in args)


def narrator(mode: str):
    """Print events as they happen.  --slow without this is dead air."""

    def emit(ev: dict) -> None:
        fmt = NARRATE.get(ev["kind"])
        if fmt:
            print(f"[{mode:<10}]{fmt(ev)}", flush=True)

    return emit


def db_for(db_dir: str, mode: str) -> str:
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, f"demo-{mode}.db")


def summarise(res: dict) -> None:
    st = res["state"]
    verdict = res["verdict"]

    print(f"\n--- {res['mode'].upper():<11} {res['scenario']} ---")
    if st.get("empty"):
        print(f"  outcome:       {res['result'].get('outcome')} (no journal records)")
        return
    board = st["scoreboard"]
    print(f"  outcome:       {res['result'].get('outcome')}"
          f"   {res['result'].get('reason', '')}")
    if res.get("note"):
        print(f"  note:          {res['note']}")
    print(f"  tickets {board['tickets']}   posts {board['posts']}   pages {board['pages']}"
          f"      (gross {board['gross'].get('create_ticket', 0)}/"
          f"{board['gross'].get('post_to_channel', 0)}/"
          f"{board['gross'].get('page_oncall', 0)})")

    for p in st["phones"]:
        sup = f"  supersedes {p['supersedes']['rota']}" if p.get("supersedes") else ""
        print(f"  phone rang:    {p['rota']}{sup}")
    if not st["phones"]:
        print("  phone rang:    nobody")

    gate = st["barrier"]
    if gate["state"] != "idle":
        print(f"  gate:          [{gate['state'].upper()}] {gate['label']}")

    for r in st["residue"]:
        rota = (r["args"] or {}).get("rota")
        # Supersede is a fact about the journal, not something to assume from the mode.
        superseded = any(
            (p.get("supersedes") or {}).get("rota") == rota for p in st["phones"]
        )
        tag = "superseded by a later page" if superseded else "NOT superseded, nothing explains it"
        print(f"  residue:       {r['tool']} to {rota} (permanent, {tag})")
    for ld in st["late_deliveries"]:
        print(f"  zombie caught: {ld['tool']} on key {ld['key'][:28]}...")
    for e in st["escalations"]:
        print(f"  escalated:     {e['reason']}")

    print(f"  EEO:           {'PASS' if verdict['pass'] else 'FAIL'}")
    for v in verdict["violations"]:
        tag = "explained" if v["explained"] else "UNEXPLAINED"
        print(f"                 [{tag}] {v['clause']}: {v['message']}")


def run_panes(scenario: str, modes: list[str], args) -> list[dict]:
    fn, desc = SCENARIOS[scenario]
    print(f"\n{BAR}\nSCENARIO: {scenario} -- {desc}\n{BAR}")

    out = []
    narrate = args.narrate or args.slow > 0
    for mode in modes:
        ctx = make_ctx(db_for(args.db_dir, mode), world_kind=args.world)
        if narrate:
            ctx.event_sink = narrator(mode)
            print(f"\n[{mode}] running...")
        try:
            res = fn(
                ctx,
                mode,
                {
                    "step_pause_s": args.slow,
                    "late_delivery_delay_s": args.late_delay,
                    "flush": args.flush_late,
                    "crash_seq": args.crash_seq,
                    "crash_phase": args.crash_phase,
                },
            )
            summarise(res)
            out.append(res)
            if args.audit_dir:
                os.makedirs(args.audit_dir, exist_ok=True)
                path = os.path.join(args.audit_dir, f"{scenario}-{mode}.md")
                write_post_mortem(ctx.journal, res["workflow_id"], path, mode=mode)
                print(f"  post-mortem:   {path}")
            if args.verbose:
                print("\n" + render_terminal(res["state"]) + "\n")
        except Exception as exc:  # a broken pane must not take the other two down
            print(f"\n--- {mode.upper():<11} {scenario} ---\n  halted: {exc!r}")
            if args.traceback:
                import traceback

                traceback.print_exc()
        finally:
            ctx.close()
    return out


def http_topology_down() -> list[str]:
    """Which of the four services cannot be reached; ``["httpx"]`` if the client is missing."""
    try:
        import httpx

        from ..world.http_client import default_urls
    except ImportError:
        return ["httpx"]
    down = []
    for name, url in default_urls().items():
        try:
            if httpx.get(f"{url.rstrip('/')}/health", timeout=1.0).status_code != 200:
                down.append(name)
        except Exception:
            down.append(name)
    return down


def baseline_summary(results: list[dict]) -> str:
    """Report what the baselines actually did in THIS run."""
    failed = [r for r in results if not r["verdict"]["pass"]]
    if not failed:
        return ("Every pane satisfied EEO in this run. The baselines are not always"
                " wrong -- they are wrong under divergence, which is the point.")

    lines = ["What the baselines actually did in this run:"]
    for r in failed:
        clauses = sorted({v["clause"] for v in r["verdict"]["unexplained"]})
        lines.append(f"  {r['mode']:<11}{r['scenario']:<12}FAIL  {', '.join(clauses)}")

    passed_baselines = [
        r for r in results if r["mode"] != "sluice" and r["verdict"]["pass"]
    ]
    for r in passed_baselines:
        lines.append(
            f"  {r['mode']:<11}{r['scenario']:<12}pass"
            f"  -- exactly-once holds; it is the decision that is wrong"
        )
    return "\n".join(lines)


def main(argv=None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Sluice demo")
    ap.add_argument("--scenario", default="poison",
                    choices=sorted(SCENARIOS) + ["all"],
                    help="which scenario to run across the panes (default: %(default)s)")
    ap.add_argument("--modes", default="pinned,naive,sluice",
                    help="comma-separated subset of pinned,naive,sluice (default: all three)")
    ap.add_argument("--world", default="inprocess", choices=["inprocess", "http"],
                    help="http requires `sluice services` to be up")
    ap.add_argument("--db-dir", default=".sluice",
                    help="where journals are written; point the dashboard here"
                         " (default: %(default)s)")
    ap.add_argument("--slow", type=float, default=0.0, metavar="SECONDS",
                    help="pause between steps so the room can watch (try 0.35)")
    ap.add_argument("--late-delay", type=float, default=8.0,
                    help="late-delivery window: 8 on stage, 40 in the sweep (3.3)")
    ap.add_argument("--flush-late", action="store_true",
                    help="fire late deliveries immediately instead of waiting")
    ap.add_argument("--crash-seq", type=int, default=4,
                    help="step to crash at, 0-7, for --scenario crash (default: %(default)s)")
    ap.add_argument("--crash-phase", default="after_effect",
                    choices=["before_intent", "after_intent", "after_effect", "after_result"],
                    help="where in the step the crash lands (default: %(default)s)")
    ap.add_argument("--audit-dir", default=None,
                    help="write an incident post-mortem per pane")
    ap.add_argument("--sweep", action="store_true", help="run the crash sweep (2.8)")
    ap.add_argument("--sweep-quick", action="store_true", help="a short sweep")
    ap.add_argument("--failover", action="store_true", help="lease and fencing demo (3.4)")
    ap.add_argument("--bench", action="store_true",
                    help="overhead benchmark: what durable execution costs (7.6)")
    ap.add_argument("--bench-n", type=int, default=200,
                    help="workflows per configuration (default: %(default)s)")
    ap.add_argument("--bench-concurrency", type=int, default=0,
                    help="also measure K threads sharing one journal (0 = skip)")
    ap.add_argument("--bench-effect-latency-ms", type=float, default=25.0,
                    help="per-effect latency for the realistic-denominator rows"
                         " (0 = skip those rows)")
    ap.add_argument("--narrate", action="store_true",
                    help="stream events live; implied by --slow")
    ap.add_argument("--verbose", action="store_true",
                    help="also print each pane's branch tree, step by step")
    ap.add_argument("--traceback", action="store_true",
                    help="print the traceback when a pane halts")
    args = ap.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for m in modes:
        if m not in MODES:
            ap.error(f"unknown mode {m!r}; expected a subset of {','.join(MODES)}")

    if args.world == "http" and not (args.bench or args.failover or args.sweep
                                     or args.sweep_quick):
        down = http_topology_down()
        if down == ["httpx"]:
            print('--world http needs the HTTP extras:  pip install -e ".[http]"',
                  file=sys.stderr)
            return 2
        if down:
            print(f"--world http: services down: {down}. Start them with:"
                  "  sluice services", file=sys.stderr)
            return 2

    if args.bench:
        from ..verification.bench import benchmark, format_benchmark

        print(f"\n{BAR}\nOVERHEAD BENCHMARK\n{BAR}")
        print(f"  measuring {args.bench_n} workflows per configuration, four"
              f" configurations...\n", flush=True)
        report = benchmark(
            n=args.bench_n,
            concurrency=args.bench_concurrency,
            effect_latency_ms=args.bench_effect_latency_ms,
        )
        print(format_benchmark(report))
        return 0

    if args.failover:
        from ..scenarios.failover import format_failover, run_failover

        os.makedirs(args.db_dir, exist_ok=True)
        res = run_failover(os.path.join(args.db_dir, "failover.db"))
        print("\n" + format_failover(res))
        return 0 if res["stale_leader_fenced"] and res["verdict"]["pass"] else 1

    if args.sweep or args.sweep_quick:
        from ..verification.checker import format_escalations
        from ..verification.sweep import format_by_fault_mode, format_failures, sweep

        started = time.time()
        print(f"\n{BAR}\nCRASH SWEEP\n{BAR}")

        def progress(v, i, n):
            if i % 16 == 0 or i == n:
                print(f"  {i}/{n} runs, {time.time() - started:.1f}s", flush=True)

        report = sweep(
            steps=[2, 4, 5, 6] if args.sweep_quick else None,
            fault_modes=(
                ["crash", "partition", "partition-transient"] if args.sweep_quick else None
            ),
            progress=progress,
        )
        print("\n" + report["table"])
        print(f"\n  wall clock:                {report['elapsed_s']:.1f}s")
        print("\n" + format_by_fault_mode(report))
        print("\n" + format_escalations(report["results"]))
        print("\n" + format_failures(report))
        return 0 if not any(r.get("unexplained") for r in report["results"]) else 1

    scenarios = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    verdicts = []
    every = []
    for name in scenarios:
        results = run_panes(name, modes, args)
        every += results
        verdicts += [r["verdict"] for r in results if r["mode"] == "sluice"]

    print(f"\n{BAR}")
    print(baseline_summary(every))
    print(f"{BAR}\n")
    print(evidence_table(verdicts, sweep=False))
    for v in verdicts:
        if not v["pass"]:
            print("\n" + explain(v))

    return 0 if all(v["pass"] for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
