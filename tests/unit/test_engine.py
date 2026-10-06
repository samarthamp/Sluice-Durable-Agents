"""The orchestrator: replay, divergence, the barrier, compensation, escalation, crashes.

Part 0.2 of the plan singles these paths out for review by hand. Each test drives one
path and asserts on the journal, which is what recovery and the dashboard both read.
"""

from __future__ import annotations

import time

import pytest

from sluice.core.engine import (
    MODES,
    CrashPolicy,
    Orchestrator,
    ProcessCrash,
    recover,
)
from sluice.core.tools import CLASSIFY_SEQ, DEMO_ALERT, PAGE_SEQ, STEP_LABELS
from sluice.core.types import ToolResult, workflow_id_for

WF = workflow_id_for(DEMO_ALERT.alert_id)


class ScriptedWorld:
    """Wraps a real world and overrides chosen answers, one call at a time."""

    def __init__(self, inner):
        self.inner = inner
        self.execute_script: dict[str, list[ToolResult]] = {}
        self.compensate_script: dict[str, list] = {}
        self.probe_script: dict[str, list[str]] = {}

    def execute(self, tool_name, args, key, epoch, timeout_s):
        if self.execute_script.get(tool_name):
            return self.execute_script[tool_name].pop(0)
        return self.inner.execute(tool_name, args, key, epoch, timeout_s)

    def compensate(self, tool_name, args, key, epoch, timeout_s):
        if self.compensate_script.get(tool_name):
            answer = self.compensate_script[tool_name].pop(0)
            return answer() if callable(answer) else answer
        return self.inner.compensate(tool_name, args, key, epoch, timeout_s)

    def probe(self, tool_name, key, timeout_s):
        if self.probe_script.get(tool_name):
            return self.probe_script[tool_name].pop(0)
        return self.inner.probe(tool_name, key, timeout_s)


def kinds(journal, *wanted):
    return [r.kind for r in journal.records(WF) if not wanted or r.kind in wanted]


def run(journal, world, severity="P2", **kw):
    events: list[dict] = []
    out = recover(journal, world, DEMO_ALERT, severity, owner="orch-test",
                  on_event=events.append, **kw)
    return out, events


# --------------------------------------------------------------- happy path


def test_a_clean_run_completes_with_intent_before_every_result(journal, world, ledger):
    out, _ = run(journal, world, "P1")
    assert out["outcome"] == "completed" and out["rota"] == "rota-Y" and out["attempts"] == 1

    for seq, tool in enumerate(STEP_LABELS):
        step = [r for r in journal.records(WF) if r.seq == seq and r.tool_name == tool]
        assert [r.kind for r in step] == ["INTENT", "RESULT"], tool
        assert step[1].result.status == "ok" and step[1].epoch == 1
    # No abandoned branch, so the barrier never had anything to hold.
    assert not kinds(journal, "BARRIER_BLOCKED", "BARRIER_RELEASED")
    assert ledger.scoreboard(WF) == (1, 1, 1)


def test_unknown_mode_is_rejected(journal, world):
    with pytest.raises(ValueError, match="unknown mode"):
        Orchestrator(journal, world, mode="optimistic")


def test_the_three_strategies_differ_only_in_capability_flags():
    assert MODES["sluice"] == dict.fromkeys(
        ("replays", "diverges", "barrier", "compensates"), True
    )
    assert MODES["pinned"] == {"replays": True, "diverges": False,
                               "barrier": False, "compensates": False}
    assert MODES["naive"] == {"replays": False, "diverges": True,
                              "barrier": False, "compensates": False}


# ---------------------------------------------------- the poison step (GATE 2)


def test_sluice_forks_holds_the_gate_drains_lifo_then_pages_once(
    journal, world, faults, ledger
):
    faults.empty_rotas = {"rota-X"}
    out, events = run(journal, world)

    assert out["outcome"] == "completed" and out["rota"] == "rota-Y"
    old, new = journal.branches(WF)
    assert (old.status, new.status) == ("compensated", "active")
    assert (new.parent_branch_id, new.depth) == (old.branch_id, 1)

    forked = next(r for r in journal.records(WF) if r.kind == "BRANCH_FORKED")
    assert forked.detail["abandoned_severity"] == "P2"
    assert forked.detail["new_severity"] == "P1"
    assert forked.detail["reason"] == "no responder on rota-X"

    lifecycle = [
        k for k in kinds(journal)
        if k in ("BRANCH_ABANDONED", "BRANCH_FORKED", "BARRIER_BLOCKED", "COMP_RESULT",
                 "BRANCH_COMPENSATED", "BARRIER_RELEASED")
    ]
    assert lifecycle == [
        "BRANCH_ABANDONED", "BRANCH_FORKED", "BARRIER_BLOCKED",
        "COMP_RESULT", "COMP_RESULT", "BRANCH_COMPENSATED", "BARRIER_RELEASED",
    ]
    # Reverse execution order: the post goes before the ticket it references.
    assert [e["tool"] for e in events if e["kind"] == "compensated"] == [
        "post_to_channel", "create_ticket",
    ]
    blocked = next(r for r in journal.records(WF) if r.kind == "BARRIER_BLOCKED")
    assert blocked.detail["count"] == 2 and blocked.detail["blocked_tool"] == "page_oncall"
    assert ledger.scoreboard(WF) == (1, 1, 1)
    assert [e["args"]["rota"] for e in ledger.effects("page_oncall", WF)] == ["rota-Y"]


def test_pinned_replays_the_failed_decision_until_the_budget_runs_out(
    journal, world, faults, ledger
):
    faults.empty_rotas = {"rota-X"}
    out, events = run(journal, world, mode="pinned", max_attempts=3)

    assert out["outcome"] == "livelocked" and out["attempts"] == 3
    assert out["replay_reason"] == "no responder on rota-X"
    assert len(journal.branches(WF)) == 1
    # Later attempts replay the journaled classification rather than re-deciding.
    assert any(e["kind"] == "step_replayed" and e["tool"] == "classify" for e in events)
    assert ledger.scoreboard(WF) == (1, 1, 0)


def test_naive_rerun_abandons_the_old_branch_without_cleaning_up(
    journal, world, faults, ledger
):
    faults.empty_rotas = {"rota-X"}
    out, _ = run(journal, world, mode="naive")

    assert out["outcome"] == "completed"
    assert [b.status for b in journal.branches(WF)] == ["abandoned", "active"]
    assert len(journal.uncompensated(WF)) == 2
    assert not kinds(journal, "BARRIER_BLOCKED", "COMP_RESULT")
    assert ledger.scoreboard(WF) == (2, 2, 1)


# ---------------------------------------------------------- bounded divergence


def test_the_fork_bound_escalates_instead_of_forking_forever(journal, world, faults):
    faults.empty_rotas = {"rota-X"}
    out, _ = run(journal, world, max_fork_depth=0)

    assert out["outcome"] == "escalated"
    assert out["reason"] == "fork bound reached at depth 0"
    (esc,) = [r for r in journal.records(WF) if r.kind == "ESCALATED"]
    # A human inherits the whole account, not a bare alarm.
    assert {"branch_tree", "uncompensated", "residue", "max_fork_depth"} <= set(esc.detail)


def test_running_out_of_alternatives_escalates(journal, world, faults, ledger):
    faults.empty_rotas = {"rota-X", "rota-Y", "rota-Z"}
    out, _ = run(journal, world, max_fork_depth=5, max_attempts=2)

    assert out["outcome"] == "escalated" and out["reason"] == "alternatives exhausted"
    assert sorted(out["detail"]["tried"]) == ["P1", "P2", "P3"]
    assert ledger.scoreboard(WF)[2] == 0


def test_a_reversible_step_failure_escalates_rather_than_diverging(journal, world, faults):
    faults.fail_tools = {"create_ticket"}
    out, _ = run(journal, world)
    assert out["outcome"] == "escalated"
    assert out["reason"].startswith("create_ticket failed")
    assert not kinds(journal, "BRANCH_FORKED")


# ------------------------------------------------------------ bounded barrier


def test_compensation_retries_then_escalates_when_attempts_run_out(journal, world, faults):
    faults.empty_rotas = {"rota-X"}
    faults.fail_compensation_tools = {"create_ticket"}
    out, events = run(journal, world, max_comp_attempts=2, barrier_deadline_s=5.0)

    assert out["outcome"] == "escalated"
    assert out["reason"] == "compensation exhausted, barrier not satisfied"
    ticket_attempts = [
        r.detail["attempt"] for r in journal.records(WF)
        if r.kind == "COMP_RESULT" and r.tool_name == "create_ticket"
    ]
    assert ticket_attempts == [1, 2]
    assert [u["tool"] for u in out["detail"]["uncompensated"]] == ["create_ticket"]
    assert out["detail"]["attempts"] == 2
    # The irreversible page never fired.
    assert not [r for r in journal.records(WF)
                if r.kind == "RESULT" and r.tool_name == "page_oncall"
                and r.result.status == "ok"]


def test_the_compensation_deadline_bounds_the_wait(journal, world, faults):
    faults.empty_rotas = {"rota-X"}
    scripted = ScriptedWorld(world)

    def slow_failure():
        time.sleep(0.15)
        return ToolResult("failed", error="still down")

    scripted.compensate_script["post_to_channel"] = [slow_failure] * 5
    out, events = run(journal, scripted, barrier_deadline_s=0.1, max_comp_attempts=5)

    assert out["outcome"] == "escalated"
    assert any(e["kind"] == "compensation_deadline" for e in events)


def test_a_transient_compensation_failure_is_retried_to_success(journal, world, faults):
    faults.empty_rotas = {"rota-X"}
    scripted = ScriptedWorld(world)
    scripted.compensate_script["create_ticket"] = [ToolResult("failed", error="blip")]
    out, _ = run(journal, scripted)

    assert out["outcome"] == "completed" and out["rota"] == "rota-Y"
    ticket = [(r.detail["attempt"], r.result.status) for r in journal.records(WF)
              if r.kind == "COMP_RESULT" and r.tool_name == "create_ticket"]
    assert ticket == [(1, "failed"), (2, "ok")]


# ------------------------------------------------------------------ residue


def test_a_page_already_sent_becomes_residue_and_the_next_page_supersedes_it(
    journal, world, ledger
):
    out, _ = run(journal, world, diverge_after_seq=PAGE_SEQ,
                 diverge_reason="post-commit review")

    assert out["outcome"] == "completed" and out["rota"] == "rota-Y"
    old, new = journal.branches(WF)
    # Its ticket and post were compensated, but the page cannot be: the branch keeps the
    # honest status rather than graduating to "compensated".
    assert (old.status, new.status) == ("abandoned_with_residue", "active")
    # The residue does not block the barrier: only compensatable effects do.
    assert kinds(journal, "BARRIER_RELEASED") == ["BARRIER_RELEASED"]
    abandoned = next(r for r in journal.records(WF) if r.kind == "BRANCH_ABANDONED")
    assert abandoned.detail["status"] == "abandoned_with_residue"
    assert [r["tool"] for r in abandoned.detail["residue"]] == ["page_oncall"]

    pages = ledger.effects("page_oncall", WF)
    assert [p["args"]["rota"] for p in pages] == ["rota-X", "rota-Y"]
    assert pages[1]["args"]["supersedes"]["rota"] == "rota-X"


# ------------------------------------------------- unknown state (2.7, 3.3)


def test_an_unknown_observable_effect_is_probed_and_found_done(journal, world):
    scripted = ScriptedWorld(world)
    scripted.execute_script["create_ticket"] = [ToolResult("unknown", error="timeout")]
    scripted.probe_script["create_ticket"] = ["done"]
    out, events = run(journal, scripted, "P1")

    assert out["outcome"] == "completed"
    assert {"kind": "probed", "probe": "done"}.items() <= next(
        e for e in events if e["kind"] == "probed").items()
    ticket = next(r for r in journal.records(WF)
                  if r.kind == "RESULT" and r.tool_name == "create_ticket")
    assert ticket.result.status == "ok" and ticket.result.value["probed"] == "done"


def test_an_unknown_effect_that_probes_not_done_is_a_failure(journal, world):
    scripted = ScriptedWorld(world)
    scripted.execute_script["create_ticket"] = [ToolResult("unknown", error="timeout")]
    scripted.probe_script["create_ticket"] = ["not_done"]
    out, _ = run(journal, scripted, "P1")
    assert out["outcome"] == "escalated"
    assert "probe says not_done" in out["reason"]


def test_an_unobservable_unknown_page_is_surfaced_not_guessed(journal, world, faults):
    faults.timeout_tools = {"page_oncall"}
    out, _ = run(journal, world, "P1")

    assert out["outcome"] == "escalated"
    assert out["reason"] == "page_oncall in unknown state, bounded ambiguity surfaced"
    page = next(r for r in journal.records(WF)
                if r.kind == "RESULT" and r.tool_name == "page_oncall")
    assert page.result.status == "unknown"
    assert page.detail == {"probe": "unknown", "bounded_ambiguity": True}


def test_a_late_page_is_caught_on_the_key_when_the_workflow_is_driven_again(
    journal, world, faults, ledger
):
    faults.timeout_tools = {"page_oncall"}
    faults.late_delivery_tools = {"page_oncall"}
    faults.late_delivery_delay_s = 60.0
    first, _ = run(journal, world, "P1")
    assert first["outcome"] == "escalated"

    world.flush_late_deliveries()           # the page lands after all
    faults.timeout_tools = set()
    again = Orchestrator(journal, world, owner="orch-test").run(DEMO_ALERT, "P1")

    assert again["outcome"] == "completed"
    (suppressed,) = [r for r in journal.records(WF) if r.kind == "LATE_DELIVERY_SUPPRESSED"]
    assert suppressed.detail["late"] is True
    assert len(ledger.effects("page_oncall", WF)) == 1


def test_reconcile_unknowns_commits_a_late_effect_or_reports_it_still_unknown(
    journal, world, faults, ledger
):
    faults.timeout_tools = {"page_oncall"}
    faults.late_delivery_tools = {"page_oncall"}
    faults.late_delivery_delay_s = 60.0
    run(journal, world, "P1")

    reconciler = Orchestrator(journal, world, owner="orch-test", renew_lease=False)
    reconciler.epoch = journal.lease_info(WF).epoch
    assert [r["resolved"] for r in reconciler.reconcile_unknowns(WF)] == ["still_unknown"]

    world.flush_late_deliveries()
    resolved = reconciler.reconcile_unknowns(WF)
    assert [r["resolved"] for r in resolved] == ["committed"]
    assert kinds(journal, "LATE_DELIVERY_SUPPRESSED") == ["LATE_DELIVERY_SUPPRESSED"]
    reconciled = [r for r in journal.records(WF)
                  if r.kind == "RESULT" and (r.detail or {}).get("reconciled")]
    assert reconciled and reconciled[0].result.status == "ok"
    assert len(ledger.effects("page_oncall", WF)) == 1


# --------------------------------------------------------- crashes and leases


def test_a_crash_policy_fires_exactly_once_at_its_boundary():
    crash = CrashPolicy(at_seq=4, phase="after_effect")
    crash.check(3, "after_effect")
    crash.check(4, "before_intent")
    with pytest.raises(ProcessCrash, match="seq 4"):
        crash.check(4, "after_effect")
    assert crash.fired_at == (4, "after_effect") and crash.armed is False
    crash.check(4, "after_effect")  # disarmed
    assert crash.label() == "seq4:after_effect"
    assert CrashPolicy().label() == "none"


@pytest.mark.parametrize("phase", ["before_intent", "after_intent", "after_effect",
                                   "after_result"])
def test_recovery_after_a_crash_never_duplicates_the_effect(journal, world, ledger, phase):
    out, _ = run(journal, world, "P1", crash=CrashPolicy(at_seq=4, phase=phase))
    assert out["outcome"] == "completed" and out["attempts"] == 2
    assert ledger.gross_counts(WF)["create_ticket"] == 1
    keys = [e["key"] for e in ledger.effects(workflow_id=WF)]
    assert len(keys) == len(set(keys))


def test_the_nastiest_crash_leaves_an_intent_with_no_result(journal, world, ledger):
    """after_effect: the world changed but the journal never heard. The key absorbs it."""
    with pytest.raises(ProcessCrash):
        Orchestrator(journal, world, owner="orch-test",
                     crash=CrashPolicy(at_seq=4, phase="after_effect")).run(DEMO_ALERT, "P1")
    ticket = [r.kind for r in journal.records(WF) if r.tool_name == "create_ticket"]
    assert ticket == ["INTENT"]
    assert ledger.gross_counts(WF)["create_ticket"] == 1


def test_a_live_lease_held_elsewhere_means_not_leader(journal, world):
    journal.acquire_lease(WF, "orch-other", ttl=60)
    out, _ = run(journal, world)
    assert out["outcome"] == "not_leader" and out["attempts"] == 1
    assert journal.records(WF) == []


def test_each_run_renews_the_lease_and_carries_its_epoch(journal, world, faults):
    faults.empty_rotas = {"rota-X"}
    first = Orchestrator(journal, world, owner="orch-a", mode="pinned")
    first.run(DEMO_ALERT, "P2")
    second = Orchestrator(journal, world, owner="orch-a", mode="pinned")
    second.run(DEMO_ALERT, "P2")
    assert (first.epoch, second.epoch) == (1, 2)
    page_epochs = [r.epoch for r in journal.records(WF)
                   if r.kind == "INTENT" and r.tool_name == "page_oncall"]
    assert page_epochs == [1, 2]


def test_the_classification_is_recovered_from_the_journal_not_the_caller(journal, world):
    """A resumed forked branch keeps the severity it forked to."""
    branch = journal.create_branch(WF, depth=1)
    journal.append(WF, branch.branch_id, 0, "BRANCH_FORKED", detail={"new_severity": "P1"})
    out = Orchestrator(journal, world, owner="orch-test").run(DEMO_ALERT, "P2")
    assert out["rota"] == "rota-Y" and out["severity"] == "P1"


def test_alternatives_come_from_the_decision_step(journal, world, faults, monkeypatch):
    """2.5 selects from the decision's own ranked list, not a hardcoded one."""
    import sluice.core.engine as engine

    original = engine.trace_for

    def p3_first(alert, severity, supersede=None):
        steps = original(alert, severity, supersede=supersede)
        steps[CLASSIFY_SEQ]["alternatives"] = ["P3", "P1", "P2"]
        return steps

    monkeypatch.setattr(engine, "trace_for", p3_first)
    faults.empty_rotas = {"rota-X"}
    out, _ = run(journal, world)
    assert out["rota"] == "rota-Z"
