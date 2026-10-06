"""Scenario-level invariants: the properties the design rests on, checked end to end
against the ground-truth ledger with the in-process world.

These are the original hackathon invariants. The four that test a single unit (key
stability, epoch fencing, lease epochs, ranked alternatives) now live, strengthened,
in tests/unit/.
"""

from __future__ import annotations

import pytest

from sluice.core.engine import Orchestrator, recover
from sluice.core.journal import Journal
from sluice.core.tools import EFFECT_TYPES
from sluice.core.types import ToolResult, effect_key
from sluice.scenarios import (
    ALERT,
    make_ctx,
    run_failover,
    scenario_compfail,
    scenario_compretry,
    scenario_crash,
    scenario_poison,
    scenario_redelivery,
    scenario_residue,
    scenario_zombie,
)
from sluice.verification.checker import (
    check_eeo,
    escalation_reasons,
    evidence_table,
    outcome_counts,
)

# ------------------------------------------------------------------ GATE 2


def test_poison_sluice_pages_the_right_rota_once(ctx):
    res = scenario_poison(ctx, "sluice", {})
    board = res["state"]["scoreboard"]

    assert res["result"]["outcome"] == "completed"
    assert (board["tickets"], board["posts"], board["pages"]) == (1, 1, 1)
    assert [p["rota"] for p in res["state"]["phones"]] == ["rota-Y"]
    # Gross shows the work that was done and then undone; net shows the world.
    assert board["gross"]["create_ticket"] == 2
    assert res["verdict"]["pass"], res["verdict"]["unexplained"]


def test_poison_pinned_livelocks_and_loses_the_signal(ctx):
    res = scenario_poison(ctx, "pinned", {})
    board = res["state"]["scoreboard"]

    assert res["result"]["outcome"] == "livelocked"
    assert (board["tickets"], board["posts"], board["pages"]) == (1, 1, 0)
    assert not res["verdict"]["pass"]
    assert any(v["clause"] == "no_loss" for v in res["verdict"]["unexplained"])


def test_poison_naive_duplicates_and_never_cleans_up(ctx):
    res = scenario_poison(ctx, "naive", {})
    board = res["state"]["scoreboard"]

    assert (board["tickets"], board["posts"]) == (2, 2)
    assert not res["verdict"]["pass"]
    assert any(v["clause"] == "clean_abandonment" for v in res["verdict"]["unexplained"])


# ------------------------------------------------------- barrier scope (2.2)


def test_barrier_scope_catches_an_aunt_not_just_a_sibling(journal):
    """v1's rule said 'abandoned sibling'. Fork twice and the first abandoned branch
    is an aunt of the active one, and a sibling check would let it slip through."""
    wf = "wf-scope"
    b0 = journal.create_branch(wf)
    b1 = journal.create_branch(wf, b0.branch_id, None, 1)
    b2 = journal.create_branch(wf, b1.branch_id, None, 2)  # b1 is b2's parent, b0 its aunt

    for branch in (b0, b1):
        journal.append(
            wf, branch.branch_id, 4, "RESULT",
            tool_name="create_ticket", effect_type=EFFECT_TYPES["create_ticket"],
            args={}, key=effect_key(wf, branch.branch_id, 4), result=ToolResult("ok"),
        )
        journal.set_branch_status(branch.branch_id, "abandoned")

    branches = {p.branch_id for p in journal.uncompensated(wf)}
    assert b0.branch_id in branches, "the aunt's uncompensated effect must block the gate"
    assert b1.branch_id in branches
    assert b2.branch_id not in branches


# -------------------------------------------------- compensation order (2.6)


def test_compensation_runs_in_reverse_execution_order(ctx):
    res = scenario_poison(ctx, "sluice", {})
    drained = [
        r.tool_name for r in ctx.journal.records(res["workflow_id"])
        if r.kind == "COMP_RESULT" and r.result and r.result.status == "ok"
    ]
    # Delete the channel post before closing the ticket it references, or a live post
    # is left pointing at a closed ticket.
    assert drained == ["post_to_channel", "create_ticket"]
    assert res["verdict"]["pass"]


def test_gate_records_carry_the_reason_the_ui_renders(ctx):
    res = scenario_poison(ctx, "sluice", {})
    recs = ctx.journal.records(res["workflow_id"])

    blocked = [r for r in recs if r.kind == "BARRIER_BLOCKED"]
    released = [r for r in recs if r.kind == "BARRIER_RELEASED"]
    assert blocked and released
    assert blocked[0].detail["count"] == 2
    assert sorted(blocked[0].detail["effects"]) == ["create_ticket", "post_to_channel"]
    assert res["state"]["barrier"]["state"] == "released"


# ----------------------------------------------------- bounded divergence (2.5)


def test_fork_bound_escalates_instead_of_livelocking(ctx):
    ctx.set_faults(empty_rotas={"rota-X", "rota-Y", "rota-Z"})
    out = recover(ctx.journal, ctx.world, ALERT, "P2", mode="sluice",
                  owner="orch-test", max_fork_depth=1, max_attempts=3)
    assert out["outcome"] == "escalated"
    assert "fork bound" in out["reason"]

    verdict = check_eeo(ctx.journal, ctx.ledger, out["workflow_id"], out["outcome"])
    assert verdict["clauses"]["no_loss"], "escalation is a terminal outcome, not a loss"


# ------------------------------------------------ bounded barrier (2.3, 7.6)


def test_compensation_failure_escalates_rather_than_hanging(ctx):
    res = scenario_compfail(ctx, "sluice", {"barrier_deadline_s": 0.5})

    assert res["result"]["outcome"] == "escalated"
    assert res["state"]["barrier"]["state"] == "escalated"
    assert res["state"]["scoreboard"]["pages"] == 0
    # The shortfall is real, and it is surfaced: that is what makes it explained rather
    # than a violation.
    assert res["verdict"]["pass"], res["verdict"]["unexplained"]
    assert any(v["clause"] == "clean_abandonment" and v["explained"]
               for v in res["verdict"]["violations"])
    escalations = res["state"]["escalations"]
    assert escalations and escalations[0]["detail"]["uncompensated"]


def test_compensation_retry_succeeds_when_the_outage_is_transient(ctx):
    """The other half of the bounded barrier."""
    res = scenario_compretry(ctx, "sluice", {"barrier_deadline_s": 2.0})

    assert res["result"]["outcome"] == "completed"
    assert res["state"]["barrier"]["state"] == "released"
    assert res["state"]["scoreboard"]["pages"] == 1

    comps = [r for r in ctx.journal.records(res["workflow_id"]) if r.kind == "COMP_RESULT"]
    failed = [r for r in comps if r.result and r.result.status == "failed"]
    ok = [r for r in comps if r.result and r.result.status == "ok"]
    assert failed, "the first attempt must actually have failed"
    assert {r.tool_name for r in ok} == {"create_ticket", "post_to_channel"}
    # The ticket compensation succeeded on a later attempt than the one that failed.
    ticket_ok = [r for r in ok if r.tool_name == "create_ticket"][0]
    assert (ticket_ok.detail or {}).get("attempt", 1) > 1
    assert res["verdict"]["pass"], res["verdict"]["unexplained"]


# ------------------------------------------------------------- residue (2.4)


def test_residue_does_not_block_the_barrier_and_is_superseded(ctx):
    res = scenario_residue(ctx, "sluice", {})
    board = res["state"]["scoreboard"]

    # The abandoned branch carries a page that cannot be undone.
    assert res["state"]["residue"], "an irreversible effect was stranded; record it"
    assert "abandoned_with_residue" in {b["status"] for b in res["state"]["branches"]}
    # The barrier still held for the two compensatable effects, and still lifted.
    assert (board["tickets"], board["posts"]) == (1, 1)
    assert res["state"]["barrier"]["state"] == "released"
    # Two phones ring, and the second one explains the first.
    assert board["pages"] == 2
    assert res["state"]["phones"][1]["supersedes"]["rota"] == "rota-X"
    assert res["verdict"]["pass"], res["verdict"]["unexplained"]


def test_naive_double_buzz_has_no_supersede_and_fails_clause_1(ctx):
    res = scenario_residue(ctx, "naive", {})
    board = res["state"]["scoreboard"]

    assert (board["tickets"], board["posts"], board["pages"]) == (2, 2, 2)
    assert res["state"]["phones"][1]["supersedes"] is None
    assert any(v["clause"] == "no_duplication" for v in res["verdict"]["unexplained"])


# ------------------------------------------------------ crash recovery (2.7 c1)


@pytest.mark.parametrize("phase", ["before_intent", "after_intent", "after_effect",
                                   "after_result"])
def test_crash_at_a_boundary_never_duplicates_an_effect(tmp_path, phase):
    ctx = make_ctx(str(tmp_path / f"c-{phase}.db"))
    res = scenario_crash(ctx, "sluice", {"crash_seq": 4, "crash_phase": phase})

    keys = [e["key"] for e in ctx.ledger.effects(workflow_id=res["workflow_id"])]
    assert len(keys) == len(set(keys)), "an idempotency key committed twice"
    assert not any(v["clause"] == "no_duplication" for v in res["verdict"]["unexplained"])
    ctx.close()


def test_replay_skips_committed_steps_but_retries_failed_ones(ctx):
    """A failed step is not a completed step."""
    ctx.set_faults(empty_rotas={"rota-X"})
    first = Orchestrator(ctx.journal, ctx.world, owner="orch-test", mode="pinned").run(
        ALERT, "P2")
    assert first["outcome"] == "replay_failed"

    branch = ctx.journal.active_branch(first["workflow_id"])
    assert set(ctx.journal.completed_results(branch.branch_id)) == {0, 1, 2, 3, 4, 5}, (
        "the failed page must not count as done"
    )


# ----------------------------------------------------- at-least-once (3.2, 1.4)


def test_redelivered_alert_lands_on_one_workflow(ctx):
    res = scenario_redelivery(ctx, "sluice", {})

    assert res["deliveries"] == 2
    assert res["workflows"] == 1
    assert res["state"]["scoreboard"]["tickets"] == 1
    assert res["verdict"]["pass"], res["verdict"]["unexplained"]


# ------------------------------------------------- bounded ambiguity (2.7, 3.3)


def test_zombie_page_is_caught_on_the_key_not_paged_twice(ctx):
    res = scenario_zombie(ctx, "sluice", {"flush": True})

    pages = ctx.ledger.effects("page_oncall", workflow_id=res["workflow_id"])
    assert len(pages) == 1, "the late delivery must not become a second page"
    assert res["state"]["late_deliveries"], "the suppression must be journaled"
    assert res["state"]["escalations"], "the unknown must have been surfaced first"
    assert res["verdict"]["pass"], res["verdict"]["unexplained"]


def test_unknown_page_that_never_lands_stays_surfaced(ctx):
    """No probe, no inverse: the honest outcome is an escalation, not a guess."""
    ctx.set_faults(timeout_tools={"page_oncall"})
    out = recover(ctx.journal, ctx.world, ALERT, "P1", mode="sluice",
                  owner="orch-test", max_attempts=2)
    assert out["outcome"] == "escalated"
    assert "bounded ambiguity" in out["reason"]

    verdict = check_eeo(ctx.journal, ctx.ledger, out["workflow_id"], out["outcome"])
    assert verdict["ba_surfaced"] == 1
    assert verdict["clauses"]["bounded_ambiguity"] and verdict["clauses"]["no_loss"]


# ------------------------------------------------------------- failover (3.4)


def test_failover_fences_the_deposed_leader(tmp_path):
    res = run_failover(str(tmp_path / "failover.db"))
    try:
        assert res["standby_refused_while_lease_held"]
        assert (res["epoch_before"], res["epoch_after"]) == (1, 2)
        assert res["stale_leader_fenced"]
        assert "stale epoch 1 < 2" in res["stale_result"]["error"]
        assert res["pages"] == ["rota-Y"]
        assert res["result"]["outcome"] == "completed"
        assert res["verdict"]["pass"], res["verdict"]["unexplained"]
    finally:
        res["journal"].close()


# ------------------------------------------------------ escalation rate (7.6)


def test_escalation_rate_is_derived_not_asserted(tmp_path):
    """7.6 quotes the escalation rate as a measured number."""
    clean = make_ctx(str(tmp_path / "clean.db"))
    clean_res = scenario_poison(clean, "sluice", {})
    broken = make_ctx(str(tmp_path / "broken.db"))
    broken_res = scenario_compfail(broken, "sluice", {"barrier_deadline_s": 0.5})

    verdicts = [clean_res["verdict"], broken_res["verdict"]]
    assert outcome_counts(verdicts) == {"completed": 1, "escalated": 1}

    reasons = escalation_reasons(verdicts)
    assert sum(reasons.values()) == 1
    assert any("compensation exhausted" in r for r in reasons)

    table = evidence_table(verdicts, sweep=True)
    assert "escalation rate:           50.0%" in table
    assert "neither:                 0 / 2" in table
    assert "escalation rate:           0.0%" in evidence_table([clean_res["verdict"]],
                                                              sweep=True)
    # Two runs is not a sample.
    scenario_table = evidence_table(verdicts, sweep=False)
    assert "EEO VERDICT" in scenario_table and "50.0%" not in scenario_table
    assert "not a rate at this sample size" in scenario_table
    clean.close()
    broken.close()


def test_a_journal_reopened_after_a_run_still_holds_the_whole_story(ctx):
    """What the dashboard and the audit export rely on: another process can read it."""
    res = scenario_poison(ctx, "sluice", {})
    reader = Journal(ctx.db, read_only=True)
    try:
        kinds = [r.kind for r in reader.records(res["workflow_id"])]
        assert kinds.count("BARRIER_BLOCKED") == kinds.count("BARRIER_RELEASED") == 1
        assert len(reader.branches(res["workflow_id"])) == 2
    finally:
        reader.close()
