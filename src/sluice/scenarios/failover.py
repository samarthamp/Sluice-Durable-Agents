"""Orchestrator failover: lease, epoch, fencing token (3.4)."""

from __future__ import annotations

import os

from ..core.engine import CrashPolicy, Orchestrator, ProcessCrash
from ..core.journal import Journal, LeaseUnavailable
from ..core.tools import DEMO_ALERT as ALERT
from ..core.tools import STEP_LABELS
from ..core.types import effect_key, workflow_id_for
from ..verification.checker import check_eeo
from ..world import FaultConfig, GroundTruthLedger, InProcessWorld


def run_failover(db: str = "sluice-failover.db", pause_at_seq: int = 5) -> dict:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)

    journal = Journal(db)
    ledger = GroundTruthLedger()
    faults = FaultConfig()
    world = InProcessWorld(ledger, faults)
    workflow_id = workflow_id_for(ALERT.alert_id)
    log: list[str] = []

    def say(line: str) -> None:
        log.append(line)

    # 1. A takes leadership and starts the workflow, then the node is lost.
    a = Orchestrator(
        journal, world, owner="orch-a", mode="sluice", lease_ttl_s=60.0,
        crash=CrashPolicy(at_seq=pause_at_seq, phase="after_result"),
    )
    try:
        a.run(ALERT, "P1")
        say("orch-a: completed unexpectedly (no pause injected)")
    except ProcessCrash:
        say(f"orch-a: leader at epoch {a.epoch}, node lost after"
            f" {STEP_LABELS[pause_at_seq]}")
    epoch_a = a.epoch

    # 2. B tries to take over while A's lease is still valid, and is refused.
    b = Orchestrator(journal, world, owner="orch-b", mode="sluice", lease_ttl_s=60.0)
    try:
        journal.acquire_lease(workflow_id, "orch-b", 60.0)
        refused = False
        say("orch-b: acquired the lease while orch-a still held it -- SPLIT BRAIN")
    except LeaseUnavailable as e:
        refused = True
        say(f"orch-b: refused, {e}")

    # 3. A's lease expires.  B takes over and the epoch increments.
    journal.expire_lease(workflow_id)
    say("orch-a lease expired")
    result = b.run(ALERT, "P1")
    epoch_b = b.epoch
    say(f"orch-b: took over at epoch {epoch_b}, resumed from the journal,"
        f" outcome={result.get('outcome')} rota={result.get('rota')}")

    # 4. A wakes from its pause still believing it leads, and tries to page. The
    #    effect layer refuses the stale epoch rather than trusting A to stand down.
    stale_key = effect_key(workflow_id, "br-zombie-leader", 6)
    stale = world.execute(
        "page_oncall",
        {"rota": "rota-X", "severity": "P2", "incident": f"inc-{ALERT.alert_id}"},
        stale_key,
        epoch_a,
        2.0,
    )
    fenced = stale.status == "failed" and "stale epoch" in (stale.error or "")
    say(
        f"orch-a: woke at epoch {epoch_a} and tried to page rota-X -> "
        f"{stale.status}: {stale.error}"
    )

    verdict = check_eeo(journal, ledger, workflow_id, outcome=result.get("outcome", ""))
    pages = ledger.effects("page_oncall", workflow_id=workflow_id)
    say(f"ground truth: {len(pages)} page(s) sent"
        f" -> {[p['args'].get('rota') for p in pages]}")

    return {
        "log": log,
        "standby_refused_while_lease_held": refused,
        "epoch_before": epoch_a,
        "epoch_after": epoch_b,
        "stale_leader_fenced": fenced,
        "stale_result": stale.to_dict(),
        "result": result,
        "verdict": verdict,
        "pages": [p["args"].get("rota") for p in pages],
        "journal": journal,
        "ledger": ledger,
        "workflow_id": workflow_id,
    }


def format_failover(res: dict) -> str:
    lines = ["=== FAILOVER: lease, epoch, fencing token ===", ""]
    lines += [f"  {line}" for line in res["log"]]
    lines += [
        "",
        f"  standby refused while lease held: {res['standby_refused_while_lease_held']}",
        f"  epoch {res['epoch_before']} -> {res['epoch_after']}",
        f"  deposed leader fenced by the effect layer: {res['stale_leader_fenced']}",
        f"  pages sent: {len(res['pages'])} {res['pages']}",
        f"  EEO: {'PASS' if res['verdict']['pass'] else 'FAIL'}",
    ]
    return "\n".join(lines)
