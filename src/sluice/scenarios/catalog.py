"""The runnable scenarios, including the poison step and both baselines (5.5)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..core.engine import MODES as _CAPABILITIES
from ..core.engine import CrashPolicy, Orchestrator, recover
from ..core.journal import Journal
from ..core.tools import DEMO_ALERT, PAGE_SEQ, STEP_LABELS
from ..core.types import Alert, workflow_id_for
from ..ingest import InProcessAlertSource, drain
from ..observability.view import derive_state
from ..verification.checker import check_eeo, write_verdict
from ..world import NO_FAULTS, FaultConfig, GroundTruthLedger, InProcessWorld

# The demo alert every scenario runs.
ALERT = DEMO_ALERT

# The three recovery strategies, in pane order: pinned replay, naive re-run, sluice.
MODES = tuple(_CAPABILITIES)


@dataclass
class Ctx:
    db: str
    journal: Journal
    ledger: object
    world: object
    faults: FaultConfig | None
    kind: str = "inprocess"
    events: list = field(default_factory=list)
    # Optional live consumer of engine events (the demo narrator).
    event_sink: object = None

    def on_event(self, ev: dict) -> None:
        self.events.append(ev)
        if self.event_sink is not None:
            self.event_sink(ev)

    def set_faults(self, **kw) -> None:
        if self.kind == "inprocess":
            self.faults.update({k: v for k, v in kw.items()})
            return
        payload = {k: (sorted(v) if isinstance(v, (set, list)) else v) for k, v in kw.items()}
        for service in ("ticket", "channel", "pager"):
            self.world.set_faults(service, payload)

    def close(self) -> None:
        self.journal.close()
        for obj in (self.world, self.ledger):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass


def make_ctx(db: str, world_kind: str = "inprocess", fresh: bool = True) -> Ctx:
    parent = os.path.dirname(os.path.abspath(db))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if fresh:
        for suffix in ("", "-wal", "-shm"):
            path = db + suffix
            if os.path.exists(path):
                os.remove(path)

    journal = Journal(db)

    if world_kind == "http":
        # Same Protocol, flag swap (3.6).  Nothing above World changes.
        from ..world.http_client import HttpLedger, HttpWorld

        world = HttpWorld()
        ledger = HttpLedger()
        ctx = Ctx(db, journal, ledger, world, None, kind="http")
        if fresh:
            # The services and the ledger are long-lived processes shared by every pane.
            world.reset()
            ledger.reset()
        ctx.set_faults(**NO_FAULTS)
        return ctx

    ledger = GroundTruthLedger()
    faults = FaultConfig()
    return Ctx(db, journal, ledger, InProcessWorld(ledger, faults), faults, kind="inprocess")


def _finish(ctx: Ctx, scenario: str, mode: str, out: dict, note: str = "") -> dict:
    wf = out.get("workflow_id") or workflow_id_for(ALERT.alert_id)
    verdict = check_eeo(ctx.journal, ctx.ledger, wf, outcome=out.get("outcome", ""))
    # Drop the verdict beside the journal so the read-only dashboard can show it.
    write_verdict(ctx.db, verdict, mode=mode)
    return {
        "scenario": scenario,
        "mode": mode,
        "note": note,
        "result": out,
        "workflow_id": wf,
        "verdict": verdict,
        "state": derive_state(ctx.journal, wf, mode=mode),
        "counts": ctx.ledger.counts(workflow_id=wf),
        "gross": ctx.ledger.gross_counts(workflow_id=wf),
        "events": list(ctx.events),
        "ctx": ctx,
    }


# --------------------------------------------------------------------- scenarios


def scenario_poison(ctx: Ctx, mode: str, opts: dict) -> dict:
    """The poison step (GATE 2): P2 routes to rota-X, which has nobody on call."""
    ctx.set_faults(empty_rotas={"rota-X"})
    out = recover(
        ctx.journal, ctx.world, ALERT, "P2", mode=mode,
        owner=f"orch-{mode}", on_event=ctx.on_event,
        step_pause_s=opts.get("step_pause_s", 0.0),
        max_attempts=opts.get("max_attempts", 4),
        barrier_deadline_s=opts.get("barrier_deadline_s", 5.0),
    )
    return _finish(ctx, "poison", mode, out)


def scenario_residue(ctx: Ctx, mode: str, opts: dict) -> dict:
    """The wrong page already rang before the misclassification was found (2.4)."""
    diverge = None if mode == "pinned" else PAGE_SEQ
    out = recover(
        ctx.journal, ctx.world, ALERT, "P2", mode=mode,
        owner=f"orch-{mode}", on_event=ctx.on_event,
        step_pause_s=opts.get("step_pause_s", 0.0),
        diverge_after_seq=diverge,
        diverge_reason="post-commit review: payments-api checkout is P1, not P2",
        max_attempts=opts.get("max_attempts", 4),
    )
    note = (
        "pinned never reconsiders, so it commits the misclassification"
        if mode == "pinned"
        else ""
    )
    return _finish(ctx, "residue", mode, out, note)


def scenario_compfail(ctx: Ctx, mode: str, opts: dict) -> dict:
    """Compensation fails for good; the bounded barrier escalates instead of hanging (2.3)."""
    ctx.set_faults(empty_rotas={"rota-X"})
    heal_after = opts.get("heal_after_attempts")

    def kill_ticket_service_at_the_gate(ev: dict) -> None:
        ctx.on_event(ev)
        if ev["kind"] == "barrier_blocked":
            # Killed exactly when the gate closes, after the recovery branch has re-run
            # steps 1-6: the abandoned branch's ticket can no longer be closed.
            ctx.set_faults(down_services={"ticket"}, empty_rotas={"rota-X"})
        if (
            heal_after
            and ev["kind"] == "compensation_failed"
            and ev.get("attempt", 0) >= heal_after
        ):
            # Transient outage: the service comes back, and the NEXT compensation attempt
            # succeeds.
            ctx.set_faults(down_services=set(), empty_rotas={"rota-X"})

    out = recover(
        ctx.journal, ctx.world, ALERT, "P2", mode=mode,
        owner=f"orch-{mode}", on_event=kill_ticket_service_at_the_gate,
        step_pause_s=opts.get("step_pause_s", 0.0),
        barrier_deadline_s=opts.get("barrier_deadline_s", 2.0),
        max_comp_attempts=opts.get("max_comp_attempts", 3),
        max_attempts=opts.get("max_attempts", 2),
    )
    label = "compretry" if heal_after else "compfail"
    note = (
        f"ticket service recovered after {heal_after} failed attempt(s)"
        if heal_after
        else ""
    )
    return _finish(ctx, label, mode, out, note)


def scenario_compretry(ctx: Ctx, mode: str, opts: dict) -> dict:
    """The same outage, but transient: a compensation retry succeeds (2.3)."""
    return scenario_compfail(ctx, mode, {**opts, "heal_after_attempts": 1})


def scenario_zombie(ctx: Ctx, mode: str, opts: dict) -> dict:
    """The page times out, is surfaced as unknown, then lands late (3.3)."""
    delay = opts.get("late_delivery_delay_s", 8.0)
    ctx.set_faults(
        timeout_tools={"page_oncall"},
        late_delivery_tools={"page_oncall"},
        late_delivery_delay_s=delay,
    )

    out = recover(
        ctx.journal, ctx.world, ALERT, "P2", mode=mode,
        owner=f"orch-{mode}", on_event=ctx.on_event,
        step_pause_s=opts.get("step_pause_s", 0.0),
        max_attempts=opts.get("max_attempts", 2),
    )
    wf = out.get("workflow_id") or workflow_id_for(ALERT.alert_id)

    if opts.get("flush", False):
        if hasattr(ctx.world, "flush_late_deliveries"):
            ctx.world.flush_late_deliveries()
        elif hasattr(ctx.world, "flush_late"):
            ctx.world.flush_late()
    else:
        # Forty seconds in the crash sweep, compressed here so the demo finishes on
        # time (3.3).
        time.sleep(delay + 0.4)

    # The pager is left timing out on purpose.
    reconciler = Orchestrator(
        ctx.journal, ctx.world, owner=f"orch-{mode}", mode=mode, on_event=ctx.on_event
    )
    reconciler.epoch = ctx.journal.lease_info(wf).epoch if ctx.journal.lease_info(wf) else 1
    reconciler.renew_lease = False
    resolved = reconciler.reconcile_unknowns(wf)

    res = _finish(ctx, "zombie", mode, out, note=f"reconciled: {resolved}")
    res["reconciled"] = resolved
    return res


def scenario_crash(ctx: Ctx, mode: str, opts: dict) -> dict:
    """Crash at a chosen step boundary, then recover."""
    ctx.set_faults(empty_rotas={"rota-X"} if opts.get("poison", True) else set())
    crash = CrashPolicy(
        at_seq=opts.get("crash_seq", 4), phase=opts.get("crash_phase", "after_effect")
    )
    out = recover(
        ctx.journal, ctx.world, ALERT, "P2", mode=mode,
        owner=f"orch-{mode}", on_event=ctx.on_event, crash=crash,
        step_pause_s=opts.get("step_pause_s", 0.0),
        max_attempts=opts.get("max_attempts", 4),
        barrier_deadline_s=opts.get("barrier_deadline_s", 5.0),
    )
    res = _finish(
        ctx, "crash", mode, out,
        note=f"crashed at {STEP_LABELS[crash.at_seq]} ({crash.phase})",
    )
    res["crash"] = crash.label()
    return res


def scenario_redelivery(ctx: Ctx, mode: str, opts: dict) -> dict:
    """At-least-once delivery (3.2)."""
    source = InProcessAlertSource([ALERT])
    source.redeliver(ALERT)

    def handle(alert: Alert) -> dict:
        return recover(
            ctx.journal, ctx.world, alert, "P1", mode=mode,
            owner=f"orch-{mode}", on_event=ctx.on_event,
            max_attempts=opts.get("max_attempts", 3),
        )

    delivered = drain(source, handle)
    out = delivered[-1]["result"] if delivered else {"outcome": "no_alerts"}
    res = _finish(ctx, "redelivery", mode, out, note=f"{len(delivered)} deliveries, 1 workflow")
    res["deliveries"] = len(delivered)
    res["workflows"] = len(ctx.journal.workflows())
    return res


SCENARIOS = {
    "poison": (scenario_poison, "GATE 2: the poison step, three panes"),
    "residue": (scenario_residue, "irreversible residue and the supersede annotation"),
    "compfail": (scenario_compfail, "compensation fails, the bounded barrier escalates"),
    "compretry": (scenario_compretry, "compensation fails, retries, and succeeds"),
    "zombie": (scenario_zombie, "page times out, lands late, caught on the key"),
    "crash": (scenario_crash, "crash at a step boundary, recover without duplicating"),
    "redelivery": (scenario_redelivery, "the same alert delivered twice, absorbed"),
}


def run_scenario(
    name: str,
    mode: str,
    db: str,
    world_kind: str = "inprocess",
    opts: dict | None = None,
) -> dict:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}, expected one of {sorted(SCENARIOS)}")
    fn, _desc = SCENARIOS[name]
    ctx = make_ctx(db, world_kind=world_kind)
    return fn(ctx, mode, opts or {})
