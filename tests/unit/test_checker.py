"""The EEO checker (2.7), the evidence table (2.8) and the verdict sidecar.

Each clause is triggered in isolation with a hand-built journal and ledger, so a red
test names the exact rule that fired.
"""

from __future__ import annotations

import os

import pytest

from sluice.core.tools import EFFECT_TYPES
from sluice.core.types import ToolResult, effect_key, workflow_id_for
from sluice.verification.checker import (
    check_eeo,
    escalation_reasons,
    evidence_table,
    explain,
    format_escalations,
    outcome_counts,
    read_verdict,
    verdict_path,
    write_verdict,
)

WF = workflow_id_for("a-check")


def k(branch_id: str, seq: int) -> str:
    return effect_key(WF, branch_id, seq)


def result(journal, branch_id, seq, tool, status="ok", args=None, detail=None):
    journal.append(WF, branch_id, seq, "RESULT", tool_name=tool,
                   effect_type=EFFECT_TYPES[tool], args=args or {}, key=k(branch_id, seq),
                   epoch=1, result=ToolResult(status), detail=detail)


def comp(journal, branch_id, seq, tool, attempt=1):
    journal.append(WF, branch_id, seq, "COMP_RESULT", tool_name=tool,
                   effect_type=EFFECT_TYPES[tool], key=k(branch_id, seq), epoch=1,
                   result=ToolResult("ok"), detail={"attempt": attempt})


def escalate(journal, branch_id, uncompensated=(), reason="test escalation"):
    journal.append(WF, branch_id, 99, "ESCALATED", detail={
        "reason": reason,
        "uncompensated": [{"tool": t, "key": key} for t, key in uncompensated],
    })


def clauses_failed(verdict) -> set[str]:
    return {v["clause"] for v in verdict["unexplained"]}


# ----------------------------------------------------------------- baseline


def test_a_clean_completed_workflow_passes_every_clause(journal, ledger):
    b = journal.create_branch(WF)
    for seq, tool in [(4, "create_ticket"), (5, "post_to_channel"), (6, "page_oncall")]:
        result(journal, b.branch_id, seq, tool, args={"rota": "rota-Y"})
        ledger.record(tool, k(b.branch_id, seq), {"rota": "rota-Y"})

    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert v["pass"] and all(v["clauses"].values())
    assert v["violations"] == [] and v["ba_surfaced"] == 0
    assert v["scoreboard"] == (1, 1, 1)
    assert v["workflow_id"] == WF and v["outcome"] == "completed"


# ------------------------------------------------------- clause 1: no dup


def test_clause_1_catches_one_key_committed_twice(journal, ledger):
    ledger.record("create_ticket", k("br", 4), {})
    ledger.record("create_ticket", k("br", 4), {})
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert clauses_failed(v) == {"no_duplication"}
    assert "committed 2 times" in v["unexplained"][0]["message"]


def test_clause_1_catches_a_second_page_that_explains_nothing(journal, ledger):
    ledger.record("page_oncall", k("br-a", 6), {"rota": "rota-X"})
    ledger.record("page_oncall", k("br-b", 6), {"rota": "rota-Y"})
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert clauses_failed(v) == {"no_duplication"}
    assert "carries no supersede annotation" in v["unexplained"][0]["message"]


def test_clause_1_accepts_a_second_page_that_supersedes_the_first(journal, ledger):
    ledger.record("page_oncall", k("br-a", 6), {"rota": "rota-X"})
    ledger.record("page_oncall", k("br-b", 6),
                  {"rota": "rota-Y", "supersedes": {"rota": "rota-X"}})
    assert check_eeo(journal, ledger, WF, outcome="completed")["pass"]


def test_clause_1_rejects_a_supersede_pointing_at_a_page_that_never_happened(journal, ledger):
    ledger.record("page_oncall", k("br-a", 6), {"rota": "rota-X"})
    ledger.record("page_oncall", k("br-b", 6),
                  {"rota": "rota-Y", "supersedes": {"rota": "rota-Z"}})
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert "which was never committed" in v["unexplained"][0]["message"]


# ------------------------------------------------------ clause 2: no loss


def test_clause_2_catches_a_workflow_that_ends_silently(journal, ledger):
    v = check_eeo(journal, ledger, WF, outcome="livelocked")
    assert clauses_failed(v) == {"no_loss"}


def test_clause_2_counts_an_escalation_as_terminal(journal, ledger):
    b = journal.create_branch(WF)
    escalate(journal, b.branch_id)
    v = check_eeo(journal, ledger, WF, outcome="escalated")
    assert v["clauses"]["no_loss"]
    assert v["escalations"] == ["test escalation"]


def test_clause_2_counts_a_committed_page_as_terminal(journal, ledger):
    ledger.record("page_oncall", k("br", 6), {"rota": "rota-Y"})
    assert check_eeo(journal, ledger, WF, outcome="")["clauses"]["no_loss"]


# ------------------------------------------------ clause 3: clean abandonment


def _dead_branch_with_ticket_and_post(journal):
    b = journal.create_branch(WF)
    result(journal, b.branch_id, 4, "create_ticket")
    result(journal, b.branch_id, 5, "post_to_channel")
    journal.set_branch_status(b.branch_id, "abandoned")
    return b


def test_clause_3_catches_an_abandoned_effect_left_standing(journal, ledger):
    _dead_branch_with_ticket_and_post(journal)
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert clauses_failed(v) == {"clean_abandonment"}
    assert "2 uncompensated on abandoned branch" in v["unexplained"][0]["message"]


def test_clause_3_shortfall_named_by_an_escalation_is_explained(journal, ledger):
    b = _dead_branch_with_ticket_and_post(journal)
    escalate(journal, b.branch_id, [("create_ticket", k(b.branch_id, 4)),
                                    ("post_to_channel", k(b.branch_id, 5))])
    v = check_eeo(journal, ledger, WF, outcome="escalated")
    assert v["pass"]
    (violation,) = v["violations"]
    assert violation["explained"] and "escalated rather than acting blind" in violation["why"]


def test_clause_3_escalation_must_name_every_stranded_effect(journal, ledger):
    b = _dead_branch_with_ticket_and_post(journal)
    escalate(journal, b.branch_id, [("create_ticket", k(b.branch_id, 4))])
    assert clauses_failed(check_eeo(journal, ledger, WF, "escalated")) == {"clean_abandonment"}


def test_clause_3_forbids_compensating_a_live_branch(journal, ledger):
    live = journal.create_branch(WF)
    result(journal, live.branch_id, 4, "create_ticket")
    comp(journal, live.branch_id, 4, "create_ticket")
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert "on live branch" in v["unexplained"][0]["message"]


def test_clause_3_requires_reverse_execution_order(journal, ledger):
    b = _dead_branch_with_ticket_and_post(journal)
    comp(journal, b.branch_id, 4, "create_ticket")      # forward: ticket first
    comp(journal, b.branch_id, 5, "post_to_channel")
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert "did not run in reverse execution order" in v["unexplained"][0]["message"]


def test_clause_3_accepts_lifo_compensation(journal, ledger):
    b = _dead_branch_with_ticket_and_post(journal)
    comp(journal, b.branch_id, 5, "post_to_channel")
    comp(journal, b.branch_id, 4, "create_ticket")
    assert check_eeo(journal, ledger, WF, outcome="completed")["clauses"]["clean_abandonment"]


# ---------------------------------------------------- bounded ambiguity


def _unknown_page(journal, branch_id):
    result(journal, branch_id, 6, "page_oncall", status="unknown")


def test_one_surfaced_unknown_page_is_the_expected_hard_corner(journal, ledger):
    b = journal.create_branch(WF)
    _unknown_page(journal, b.branch_id)
    escalate(journal, b.branch_id)
    v = check_eeo(journal, ledger, WF, outcome="escalated")
    assert v["pass"] and v["ba_surfaced"] == 1


def test_an_unknown_page_that_was_never_surfaced_fails(journal, ledger):
    b = journal.create_branch(WF)
    _unknown_page(journal, b.branch_id)
    v = check_eeo(journal, ledger, WF, outcome="completed")
    assert any("never surfaced" in u["message"] for u in v["unexplained"])


def test_two_unknown_pages_at_once_break_bounded_ambiguity(journal, ledger):
    a, b = journal.create_branch(WF), journal.create_branch(WF)
    _unknown_page(journal, a.branch_id)
    _unknown_page(journal, b.branch_id)
    escalate(journal, b.branch_id)
    v = check_eeo(journal, ledger, WF, outcome="escalated")
    assert any("2 irreversible effects in unknown state" in u["message"]
               for u in v["unexplained"])


@pytest.mark.parametrize("resolution", ["late_delivery", "later_ok"])
def test_an_unknown_that_is_later_resolved_no_longer_counts(journal, ledger, resolution):
    b = journal.create_branch(WF)
    _unknown_page(journal, b.branch_id)
    if resolution == "late_delivery":
        journal.append(WF, b.branch_id, 6, "LATE_DELIVERY_SUPPRESSED",
                       tool_name="page_oncall", key=k(b.branch_id, 6))
    else:
        result(journal, b.branch_id, 6, "page_oncall")
    ledger.record("page_oncall", k(b.branch_id, 6), {"rota": "rota-Y"})
    assert check_eeo(journal, ledger, WF, outcome="completed")["ba_surfaced"] == 0


# --------------------------------------------------------------- reporting


def test_explain_lists_each_violation_with_its_tag(journal, ledger):
    _dead_branch_with_ticket_and_post(journal)
    text = explain(check_eeo(journal, ledger, WF, outcome="livelocked"))
    assert text.startswith(f"EEO verdict for {WF}: FAIL")
    assert "[UNEXPLAINED] no_loss" in text and "[UNEXPLAINED] clean_abandonment" in text


def test_outcome_counts_prefer_the_engine_outcome():
    rows = [{"result_outcome": "escalated", "outcome": "x"}, {"outcome": "completed"}, {}]
    assert outcome_counts(rows) == {"escalated": 1, "completed": 1, "unknown": 1}


def test_escalation_reasons_and_their_histogram():
    rows = [{"escalations": ["a", "b"]}, {"escalations": ["a"]}, {"escalations": None}]
    assert escalation_reasons(rows) == {"a": 2, "b": 1}
    text = format_escalations(rows)
    assert text.splitlines()[0] == "WHY IT ESCALATED  (3 escalation records)"
    assert text.splitlines()[1].split() == ["2", "a"]
    assert format_escalations([]) == "no escalations"


def _verdict(outcome, ok=True, ba=0, unexplained=0):
    clauses = {c: ok for c in ("no_duplication", "no_loss", "clean_abandonment",
                               "bounded_ambiguity")}
    return {"outcome": outcome, "clauses": clauses, "ba_surfaced": ba,
            "unexplained": [{}] * unexplained}


def test_evidence_table_for_a_sweep():
    rows = [_verdict("completed"), _verdict("escalated", ba=1), _verdict("livelocked", ok=False,
                                                                         unexplained=2)]
    table = evidence_table(rows, crash_points=1, fault_modes=["crash", "timeout", "partition"])
    assert table.startswith("CRASH SWEEP RESULTS")
    assert "fault modes:               3   (crash, timeout, partition)" in table
    assert "EEO clause 1 (no dup):     pass 2 / 3" in table
    assert "neither:                 1 / 3    <- each one is a clause 2 loss" in table
    assert "escalation rate:           33.3%" in table
    assert "BA surfaced (unknown):     1" in table
    assert "unexplained violations:    2" in table


def test_evidence_table_refuses_to_quote_a_rate_for_a_handful_of_runs():
    table = evidence_table([_verdict("completed")], sweep=False)
    assert table.startswith("EEO VERDICT  (1 run(s))")
    assert "not a rate at this sample size" in table and "%" not in table


# ---------------------------------------------------------- verdict sidecar


def test_verdict_sidecar_round_trips(tmp_path, journal, ledger):
    panes = tmp_path / "panes"
    db = str(panes / "pane.db")
    verdict = check_eeo(journal, ledger, WF, outcome="livelocked")
    write_verdict(db, verdict, mode="pinned")

    assert verdict_path(db) == db + ".verdict.json"
    got = read_verdict(db, workflow_id=WF)
    assert got["mode"] == "pinned" and got["pass"] is False and got["outcome"] == "livelocked"
    assert got["unexplained"][0]["clause"] == "no_loss"
    # Written atomically: no temporary file left behind.
    assert sorted(os.listdir(panes)) == ["pane.db.verdict.json"]


def test_verdict_for_another_workflow_is_not_shown(tmp_path, journal, ledger):
    db = str(tmp_path / "pane.db")
    write_verdict(db, check_eeo(journal, ledger, WF, outcome="completed"))
    assert read_verdict(db, workflow_id="wf-someone-else") is None
    assert read_verdict(db) is not None


def test_a_missing_or_corrupt_sidecar_reads_as_none(tmp_path):
    db = str(tmp_path / "pane.db")
    assert read_verdict(db) is None
    with open(verdict_path(db), "w") as fh:
        fh.write("{not json")
    assert read_verdict(db) is None


def test_writing_a_verdict_never_breaks_a_run(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # The parent "directory" is a file, so the write must fail -- quietly.
    write_verdict(str(blocker / "pane.db"), {"workflow_id": WF})
    assert blocker.read_text(encoding="utf-8") == "x"
    assert read_verdict(str(blocker / "pane.db")) is None
