"""Journal -> dashboard state (view.py) and the incident post-mortem (audit.py).

The dashboard rule (6.6) is that every visual is derived from the journal. These tests
pin the derivation, including the gate's three still frames.
"""

from __future__ import annotations

import pytest

from sluice.core.types import JournalRecord, ToolResult
from sluice.observability.audit import post_mortem, write_post_mortem
from sluice.observability.view import (
    BARRIER_LABELS,
    barrier_state,
    derive_state,
    phones,
    render_terminal,
    scoreboard,
)
from sluice.scenarios import (
    scenario_compfail,
    scenario_poison,
    scenario_residue,
    scenario_zombie,
)


def rec(rid, kind, tool=None, status=None, key=None, detail=None, args=None, seq=0):
    return JournalRecord(
        record_id=rid, ts=float(rid), workflow_id="wf", branch_id="br", seq=seq, kind=kind,
        tool_name=tool, key=key, args=args, detail=detail,
        result=ToolResult(status) if status else None,
    )


# --------------------------------------------------------------- the gate


def test_gate_is_idle_until_something_blocks_it():
    assert barrier_state([]) == {
        "state": "idle", "label": BARRIER_LABELS["idle"], "count": 0, "effects": [],
    }


def test_gate_label_comes_from_the_record_and_counts_down_as_it_drains():
    blocked = rec(1, "BARRIER_BLOCKED", detail={
        "reason": "2 uncompensated effects on abandoned branch", "count": 2,
        "effects": ["create_ticket", "post_to_channel"],
    })
    s = barrier_state([blocked])
    assert s["state"] == "blocked" and s["count"] == 2
    assert s["label"] == "irreversible effect blocked: 2 uncompensated effects on abandoned branch"

    s = barrier_state([blocked, rec(2, "COMP_RESULT", "post_to_channel", "ok")])
    assert s["count"] == 1 and s["effects"] == ["create_ticket"]
    # A failed compensation does not move the counter.
    s = barrier_state([blocked, rec(2, "COMP_RESULT", "post_to_channel", "failed")])
    assert s["count"] == 2

    s = barrier_state([blocked, rec(2, "COMP_RESULT", "post_to_channel", "ok"),
                       rec(3, "COMP_RESULT", "create_ticket", "ok"),
                       rec(4, "BARRIER_RELEASED")])
    assert s == {"state": "released", "label": BARRIER_LABELS["released"],
                 "count": 0, "effects": []}
    # The record's own list is not mutated by the countdown.
    assert blocked.detail["effects"] == ["create_ticket", "post_to_channel"]


def test_gate_goes_amber_on_escalation():
    blocked = rec(1, "BARRIER_BLOCKED", detail={"reason": "r", "count": 1, "effects": ["x"]})
    s = barrier_state([blocked, rec(2, "ESCALATED", detail={"reason": "exhausted"})])
    assert s["state"] == "escalated" and s["label"] == BARRIER_LABELS["escalated"]
    # An escalation with no gate involved leaves the gate alone.
    assert barrier_state([rec(1, "ESCALATED")])["state"] == "idle"


# ---------------------------------------------------------- scoreboard, phones


def test_scoreboard_is_net_of_compensation_and_deduplicated_by_key():
    records = [
        rec(1, "RESULT", "create_ticket", "ok", key="k1"),
        rec(2, "RESULT", "create_ticket", "ok", key="k1"),   # replay of the same key
        rec(3, "RESULT", "create_ticket", "ok", key="k2"),
        rec(4, "COMP_RESULT", "create_ticket", "ok", key="k2"),
        rec(5, "RESULT", "page_oncall", "failed", key="k3"),
    ]
    board = scoreboard(records)
    assert (board["tickets"], board["posts"], board["pages"]) == (1, 0, 0)
    assert board["gross"] == {"create_ticket": 2} and board["compensated"] == 1


def test_one_phone_per_committed_page():
    records = [
        rec(1, "RESULT", "page_oncall", "failed", key="k1", args={"rota": "rota-X"}),
        rec(2, "RESULT", "page_oncall", "ok", key="k2", args={"rota": "rota-Y"}),
        rec(3, "RESULT", "page_oncall", "ok", key="k2", args={"rota": "rota-Y"}),
    ]
    assert [p["rota"] for p in phones(records)] == ["rota-Y"]


# ----------------------------------------------------- derived from real runs


def test_an_unknown_workflow_derives_an_empty_state(journal):
    s = derive_state(journal, "wf-nothing")
    assert s["empty"] is True and s["workflow_id"] == "wf-nothing"


def test_poison_state_tells_the_whole_story(ctx):
    res = scenario_poison(ctx, "sluice", {})
    s = res["state"]

    assert s["running"] is False and s["empty"] is False and s["mode"] == "sluice"
    assert s["barrier"]["state"] == "released"
    old, new = s["branches"]
    assert (old["status"], old["dead"], old["severity"], old["rota"]) == (
        "compensated", False, "P2", "rota-X")
    assert (new["status"], new["severity"], new["rota"], new["depth"]) == (
        "active", "P1", "rota-Y", 1)
    old_steps = {st["tool"]: st["status"] for st in old["steps"]}
    assert old_steps["create_ticket"] == old_steps["post_to_channel"] == "compensated"
    assert old_steps["page_oncall"] == "failed"
    assert old_steps["update_status_page"] == "pending"
    assert all(st["status"] == "ok" for st in new["steps"])
    assert [p["rota"] for p in s["phones"]] == ["rota-Y"]
    assert s["lease"]["owner"] == "orch-sluice" and s["lease"]["epoch"] == 1
    assert {e["kind"] for e in s["events"]} >= {
        "BRANCH_FORKED", "BARRIER_BLOCKED", "COMP_RESULT", "BARRIER_RELEASED",
    }
    assert s["uncompensated"] == [] and s["residue"] == []


def test_a_livelocked_workflow_keeps_its_timer_running(ctx):
    res = scenario_poison(ctx, "pinned", {})
    assert res["state"]["running"] is True
    assert res["state"]["phones"] == []


def test_render_terminal_is_a_usable_fallback_surface(ctx):
    res = scenario_poison(ctx, "sluice", {})
    text = render_terminal(res["state"])
    assert "gate: [RELEASED] cleanup proven, gate lifted" in text
    assert "scoreboard: tickets 1  posts 1  pages 1   (gross 2/2/1)" in text
    assert "phone: rota-Y rang" in text
    # One line per branch with a step-by-step path: # ok, x failed, ~ compensated.
    assert "####~~x." in text and "########" in text
    assert render_terminal({"empty": True, "workflow_id": "wf"}) == "wf: no records"


# --------------------------------------------------------------- post-mortem


def test_post_mortem_for_an_unknown_workflow(journal):
    assert "No records for wf-x" in post_mortem(journal, "wf-x")


def test_post_mortem_of_the_poison_step(ctx):
    res = scenario_poison(ctx, "sluice", {})
    text = post_mortem(ctx.journal, res["workflow_id"], mode="sluice")
    for heading in ("# Incident post-mortem", "## Effects standing at the end",
                    "## Branch tree", "## Compensation", "## Full journal"):
        assert heading in text
    assert "| `create_ticket` | 1 | 2 | compensated on an abandoned branch |" in text
    section = text.split("## Compensation", 1)[1].split("\n## ", 1)[0]
    undo_rows = [line for line in section.splitlines() if line.endswith("| ok |")]
    # Reverse execution order: the post is undone before the ticket it references.
    assert ["post_to_channel" in undo_rows[0], "create_ticket" in undo_rows[1]] == [True, True]


@pytest.mark.parametrize(
    "scenario, opts, sections",
    [
        (scenario_residue, {}, ["## Irreversible residue", "## Supersede annotations"]),
        (scenario_zombie, {"flush": True}, ["## Late-arriving effects", "## Escalations"]),
        (scenario_compfail, {"barrier_deadline_s": 0.5},
         ["## Escalations", "## Still uncompensated at quiescence"]),
    ],
)
def test_post_mortem_sections_follow_what_happened(ctx, scenario, opts, sections):
    res = scenario(ctx, "sluice", opts)
    text = post_mortem(ctx.journal, res["workflow_id"], mode="sluice")
    for heading in sections:
        assert heading in text


def test_write_post_mortem(ctx, tmp_path):
    res = scenario_poison(ctx, "naive", {})
    path = write_post_mortem(ctx.journal, res["workflow_id"], str(tmp_path / "pm.md"),
                             mode="naive")
    assert path.endswith("pm.md")
    assert (tmp_path / "pm.md").read_text(encoding="utf-8").startswith("# Incident post-mortem")
