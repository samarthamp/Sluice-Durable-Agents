"""The crash sweep (5.5, 2.8)."""

from __future__ import annotations

import os
import shutil
import tempfile
import time

from ..core.engine import CRASH_PHASES, CrashPolicy, Orchestrator, recover
from ..core.journal import Journal
from ..core.tools import DEMO_ALERT, STEP_LABELS
from ..core.types import workflow_id_for
from ..world import FaultConfig, GroundTruthLedger, InProcessWorld
from .checker import check_eeo, evidence_table, outcome_counts

# The fault modes of 2.8 (crash, timeout, partition, late delivery), plus a partition
# that heals partway through the compensation retries.
FAULT_MODES: dict[str, dict] = {
    "crash": {},
    "timeout": {"timeout_tools": {"page_oncall"}},
    "partition": {"down_at_barrier": "ticket"},
    # Heals after one failed attempt, so the backoff loop is shown succeeding and not
    # only terminating.
    "partition-transient": {"down_at_barrier": "ticket", "heal_after_attempts": 1},
    "late-delivery": {
        "timeout_tools": {"page_oncall"},
        "late_delivery_tools": {"page_oncall"},
        # Forty seconds in the sweep, per 3.3.
        "late_delivery_delay_s": 40.0,
    },
}


def _one_run(
    db: str,
    fault_mode: str,
    crash: CrashPolicy | None,
    barrier_deadline_s: float,
) -> dict:
    ledger = GroundTruthLedger()
    faults = FaultConfig()
    faults.empty_rotas = {"rota-X"}

    spec = FAULT_MODES[fault_mode]
    for name, value in spec.items():
        if name in ("down_at_barrier", "heal_after_attempts"):
            continue
        setattr(faults, name, value)

    world = InProcessWorld(ledger, faults)
    journal = Journal(db)
    heal_after = spec.get("heal_after_attempts")

    def on_event(ev):
        if ev["kind"] == "barrier_blocked" and spec.get("down_at_barrier"):
            faults.down_services = {spec["down_at_barrier"]}
        if (
            heal_after
            and ev["kind"] == "compensation_failed"
            and ev.get("attempt", 0) >= heal_after
        ):
            faults.down_services = set()

    started = time.time()
    out = recover(
        journal,
        world,
        DEMO_ALERT,
        "P2",
        mode="sluice",
        owner="orch-sweep",
        crash=crash,
        on_event=on_event,
        max_attempts=6,
        barrier_deadline_s=barrier_deadline_s,
        max_comp_attempts=2,
    )

    # Quiescence: fire any pending late delivery, then reconcile whatever is still unknown.
    world.flush_late_deliveries()
    wf = out.get("workflow_id") or workflow_id_for(DEMO_ALERT.alert_id)

    reconciler = Orchestrator(journal, world, owner="orch-sweep", mode="sluice")
    reconciler.renew_lease = False
    lease = journal.lease_info(wf)
    reconciler.epoch = lease.epoch if lease else 1
    # The injected fault is deliberately NOT cleared first.
    reconciler.reconcile_unknowns(wf)

    verdict = check_eeo(journal, ledger, wf, outcome=out.get("outcome", ""))
    verdict["fault_mode"] = fault_mode
    verdict["crash_point"] = crash.label() if crash else "none"
    verdict["elapsed_s"] = time.time() - started
    verdict["result_outcome"] = out.get("outcome")
    journal.close()
    return verdict


def sweep(
    steps: list[int] | None = None,
    phases: tuple[str, ...] = CRASH_PHASES,
    fault_modes: list[str] | None = None,
    barrier_deadline_s: float = 0.4,
    workdir: str | None = None,
    progress=None,
) -> dict:
    fault_modes = fault_modes or list(FAULT_MODES)
    steps = list(range(len(STEP_LABELS))) if steps is None else steps

    owned_dir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="sluice-sweep-")
    os.makedirs(workdir, exist_ok=True)

    crash_points: list[CrashPolicy | None] = [None]
    for seq in steps:
        for phase in phases:
            crash_points.append(CrashPolicy(at_seq=seq, phase=phase))

    results: list[dict] = []
    started = time.time()
    try:
        for mode in fault_modes:
            for i, cp in enumerate(crash_points):
                db = os.path.join(workdir, f"sweep-{mode}-{i}.db")
                crash = CrashPolicy(at_seq=cp.at_seq, phase=cp.phase) if cp else None
                verdict = _one_run(db, mode, crash, barrier_deadline_s)
                results.append(verdict)
                if progress:
                    progress(verdict, len(results), len(crash_points) * len(fault_modes))
    finally:
        if owned_dir:
            shutil.rmtree(workdir, ignore_errors=True)

    return {
        "results": results,
        "crash_points": len(crash_points),
        "fault_modes": fault_modes,
        "elapsed_s": time.time() - started,
        "table": evidence_table(results, len(crash_points), fault_modes),
    }


def by_fault_mode(report: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for mode in report["fault_modes"]:
        rows = [r for r in report["results"] if r["fault_mode"] == mode]
        counts = outcome_counts(rows)
        n = len(rows) or 1
        out[mode] = {
            "runs": len(rows),
            "outcomes": counts,
            "escalation_rate": 100.0 * counts.get("escalated", 0) / n,
            "ba": sum(r.get("ba_surfaced", 0) for r in rows),
            "unexplained": sum(len(r.get("unexplained", [])) for r in rows),
        }
    return out


def format_by_fault_mode(report: dict) -> str:
    """Which fault mode actually forces a human in."""
    rows = by_fault_mode(report)
    width = max([len("mode"), *(len(mode) for mode in rows)]) + 2
    lines = [
        "BY FAULT MODE",
        f"  {'mode':<{width}}{'runs':>6}{'acted':>8}{'escalated':>11}"
        f"{'esc rate':>10}{'BA':>6}{'unexpl':>8}",
    ]
    for mode, r in rows.items():
        lines.append(
            f"  {mode:<{width}}{r['runs']:>6}{r['outcomes'].get('completed', 0):>8}"
            f"{r['outcomes'].get('escalated', 0):>11}{r['escalation_rate']:>9.1f}%"
            f"{r['ba']:>6}{r['unexplained']:>8}"
        )
    return "\n".join(lines)


def failures(report: dict) -> list[dict]:
    return [r for r in report["results"] if r.get("unexplained")]


def format_failures(report: dict, limit: int = 20) -> str:
    bad = failures(report)
    if not bad:
        return "no unexplained violations"
    lines = [f"{len(bad)} run(s) with unexplained violations:"]
    for r in bad[:limit]:
        lines.append(
            f"  [{r['fault_mode']:<19} {r['crash_point']:<22}] -> {r['result_outcome']}"
        )
        for v in r["unexplained"]:
            lines.append(f"      {v['clause']}: {v['message']}")
    if len(bad) > limit:
        lines.append(f"  ... and {len(bad) - limit} more")
    return "\n".join(lines)
