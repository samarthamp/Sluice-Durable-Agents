"""The incident-triage workflow definition: tools, effect types, the scripted trace."""

from __future__ import annotations

from sluice.core.tools import (
    ALTERNATIVES,
    CLASSIFY_SEQ,
    COMPENSATIONS,
    DEMO_ALERT,
    EFFECT_TYPES,
    PAGE_SEQ,
    ROTA_FOR_SEVERITY,
    SERVICE_FOR_TOOL,
    STEP_LABELS,
    TOOLS_FOR_SERVICE,
    supersede_annotation,
    trace_for,
)
from sluice.core.types import JournalRecord


def test_every_step_has_an_effect_type_and_an_owning_service():
    assert len(STEP_LABELS) == 8
    for tool in STEP_LABELS:
        assert tool in EFFECT_TYPES
        assert SERVICE_FOR_TOOL[tool] in ("ticket", "channel", "pager")


def test_compensations_cover_exactly_the_compensatable_tools():
    compensatable = {t for t, et in EFFECT_TYPES.items() if et.reversibility == "compensatable"}
    assert set(COMPENSATIONS) == compensatable


def test_the_page_is_the_only_irreversible_unobservable_effect():
    hard = [
        t for t, et in EFFECT_TYPES.items()
        if et.reversibility == "irreversible" and et.observability == "unobservable"
    ]
    assert hard == ["page_oncall"]
    assert SERVICE_FOR_TOOL["page_oncall"] == "pager"


def test_step_indices():
    assert STEP_LABELS[CLASSIFY_SEQ] == "classify"
    assert STEP_LABELS[PAGE_SEQ] == "page_oncall"
    assert CLASSIFY_SEQ < PAGE_SEQ


def test_tools_for_service_is_the_inverse_of_service_for_tool():
    flattened = {t: svc for svc, tools in TOOLS_FOR_SERVICE.items() for t in tools}
    assert flattened == SERVICE_FOR_TOOL


def test_every_alternative_routes_to_a_rota():
    assert set(ALTERNATIVES) <= set(ROTA_FOR_SEVERITY)
    assert ROTA_FOR_SEVERITY["P1"] != ROTA_FOR_SEVERITY["P2"]


def test_trace_follows_the_step_labels_and_routes_by_severity():
    for severity, rota in ROTA_FOR_SEVERITY.items():
        steps = trace_for(DEMO_ALERT, severity)
        assert [s["tool"] for s in steps] == STEP_LABELS
        assert steps[PAGE_SEQ]["args"]["rota"] == rota
        assert steps[CLASSIFY_SEQ]["args"]["severity"] == severity
        assert "supersedes" not in steps[PAGE_SEQ]["args"]


def test_trace_marks_the_decision_and_copies_its_alternatives():
    steps = trace_for(DEMO_ALERT, "P2")
    decision = steps[CLASSIFY_SEQ]
    assert decision["decision"] is True
    assert decision["alternatives"] == ALTERNATIVES
    # Mutating one trace must not leak into the module-level ranking.
    decision["alternatives"].reverse()
    assert ALTERNATIVES == ["P1", "P2", "P3"]


def test_trace_carries_a_supersede_pointer_only_when_given():
    pointer = {"rota": "rota-X", "note": "n"}
    steps = trace_for(DEMO_ALERT, "P1", supersede=pointer)
    assert steps[PAGE_SEQ]["args"]["supersedes"] == pointer


def test_incident_id_ties_the_steps_together():
    steps = trace_for(DEMO_ALERT, "P1")
    incident = f"inc-{DEMO_ALERT.alert_id}"
    assert steps[3]["args"]["incident_id"] == incident
    assert steps[PAGE_SEQ]["args"]["incident"] == incident


def test_supersede_annotation_names_the_residue_it_replaces():
    residue = JournalRecord(
        record_id=42, ts=0.0, workflow_id="wf", branch_id="br-1", seq=PAGE_SEQ,
        kind="RESULT", tool_name="page_oncall",
        args={"rota": "rota-X", "severity": "P2", "incident": "inc-a-1001"},
    )
    note = supersede_annotation(residue)
    assert note["record_id"] == 42
    assert note["rota"] == "rota-X"
    assert "supersedes page to rota-X" in note["note"]
    assert "classification P2" in note["note"]


def test_demo_alert_is_the_poison_step_alert():
    assert DEMO_ALERT.alert_id == "a-1001"
    assert DEMO_ALERT.service == "payments-api"
