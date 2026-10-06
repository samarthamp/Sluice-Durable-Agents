"""Part 4 interfaces: serialisation round trips, key derivation, protocol conformance."""

from __future__ import annotations

import inspect
import json

import pytest

from sluice.core.types import (
    DEAD_BRANCH_STATUSES,
    Alert,
    Branch,
    EffectType,
    JournalRecord,
    ToolResult,
    World,
    effect_key,
    workflow_from_key,
    workflow_id_for,
)
from sluice.world import InProcessWorld

# ------------------------------------------------------------------ dataclasses


def test_effect_type_round_trips_through_a_dict():
    et = EffectType("compensatable", "observable", True)
    assert EffectType.from_dict(et.to_dict()) == et


def test_effect_type_defaults_externally_visible_to_false():
    assert EffectType.from_dict({"reversibility": "pure", "observability": "observable"}) == (
        EffectType("pure", "observable", False)
    )
    assert EffectType.from_dict(None) is None


@pytest.mark.parametrize("status", ["ok", "failed", "unknown"])
def test_tool_result_round_trips_every_status(status):
    r = ToolResult(status, {"k": 1}, "why" if status != "ok" else None)
    assert ToolResult.from_dict(r.to_dict()) == r


def test_tool_result_from_none_is_none():
    assert ToolResult.from_dict(None) is None


def test_journal_record_to_dict_nests_effect_type_and_result():
    rec = JournalRecord(
        record_id=7, ts=1.5, workflow_id="wf-x", branch_id="br-1", seq=4, kind="RESULT",
        tool_name="create_ticket", effect_type=EffectType("compensatable", "observable"),
        args={"service": "s"}, key="wf-x:abc", epoch=2, result=ToolResult("ok"),
        detail={"attempt": 1},
    )
    d = rec.to_dict()
    assert d["effect_type"] == {
        "reversibility": "compensatable", "observability": "observable",
        "externally_visible": False,
    }
    assert d["result"] == {"status": "ok", "value": None, "error": None}
    assert d["detail"] == {"attempt": 1}
    # Everything a dashboard or audit export needs must be JSON-serialisable.
    json.dumps(d)


def test_journal_record_to_dict_tolerates_missing_optionals():
    d = JournalRecord(1, 0.0, "wf", "br", 0, "ESCALATED").to_dict()
    assert d["effect_type"] is None and d["result"] is None


def test_branch_to_dict():
    b = Branch("br-1", None, None, 0, "active")
    assert b.to_dict() == {
        "branch_id": "br-1", "parent_branch_id": None, "fork_point_record_id": None,
        "depth": 0, "status": "active",
    }


def test_alert_round_trips_and_parses_a_json_string_payload():
    a = Alert("a-1", "svc", "prometheus", {"msg": "m", "value": 0.5})
    assert Alert.from_dict(a.to_dict()) == a
    # Redis stores the payload as a JSON string.
    as_stored = {**a.to_dict(), "payload": json.dumps(a.payload)}
    assert Alert.from_dict(as_stored) == a


def test_alert_with_no_payload_gets_an_empty_dict():
    assert Alert.from_dict({"alert_id": "a", "service": "s", "source": "x"}).payload == {}


def test_dead_branch_statuses_are_the_two_abandoned_states():
    assert set(DEAD_BRANCH_STATUSES) == {"abandoned", "abandoned_with_residue"}


# ---------------------------------------------------------------- key derivation


def test_effect_key_is_deterministic_and_carries_the_workflow():
    k = effect_key("wf-abc", "br-1", 6)
    assert k == effect_key("wf-abc", "br-1", 6)
    prefix, digest = k.split(":")
    assert prefix == "wf-abc"
    assert len(digest) == 16 and int(digest, 16) >= 0


@pytest.mark.parametrize(
    "other", [("wf-abd", "br-1", 6), ("wf-abc", "br-2", 6), ("wf-abc", "br-1", 7)]
)
def test_effect_key_changes_with_each_input(other):
    assert effect_key("wf-abc", "br-1", 6) != effect_key(*other)


@pytest.mark.parametrize(
    "key, expected",
    [("wf-1:deadbeef", "wf-1"), (None, ""), ("", ""), ("no-colon", ""), ("a:b:c", "a")],
)
def test_workflow_from_key(key, expected):
    assert workflow_from_key(key) == expected


def test_workflow_id_is_derived_from_the_alert_id():
    """At-least-once redelivery must land on the same workflow (3.2)."""
    wf = workflow_id_for("a-1001")
    assert wf == workflow_id_for("a-1001")
    assert wf.startswith("wf-") and len(wf) == len("wf-") + 16
    assert wf != workflow_id_for("a-1002")


def test_effect_key_round_trips_to_its_workflow():
    wf = workflow_id_for("a-1001")
    assert workflow_from_key(effect_key(wf, "br-x", 3)) == wf


# ------------------------------------------------------------- protocol contract


@pytest.mark.parametrize("method", ["execute", "compensate", "probe"])
def test_in_process_world_matches_the_world_protocol(method):
    """InProcessWorld and HttpWorld swap on a flag (3.6), so both must keep the
    exact signature the engine calls."""
    expected = list(inspect.signature(getattr(World, method)).parameters)
    actual = list(inspect.signature(getattr(InProcessWorld, method)).parameters)
    assert actual == expected


@pytest.mark.parametrize("method", ["execute", "compensate", "probe"])
def test_http_world_matches_the_world_protocol(method):
    from sluice.world.http_client import HttpWorld

    expected = list(inspect.signature(getattr(World, method)).parameters)
    actual = list(inspect.signature(getattr(HttpWorld, method)).parameters)
    assert actual == expected
