"""The journal: a SQLite-WAL branch tree with leases and fencing epochs (2.1, 3.4)."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from sluice.core.journal import Journal, LeaseUnavailable
from sluice.core.tools import EFFECT_TYPES
from sluice.core.types import ToolResult, effect_key

WF = "wf-test"


def _result(j, branch_id, seq, tool, status="ok", wf=WF, **kw):
    return j.append(
        wf, branch_id, seq, "RESULT", tool_name=tool, effect_type=EFFECT_TYPES[tool],
        args=kw.pop("args", {}), key=kw.pop("key", effect_key(wf, branch_id, seq)),
        epoch=1, result=ToolResult(status, error=None if status == "ok" else "x"), **kw,
    )


def _comp(j, branch_id, seq, tool, status="ok", attempt=1, wf=WF):
    return j.append(
        wf, branch_id, seq, "COMP_RESULT", tool_name=tool, effect_type=EFFECT_TYPES[tool],
        key=effect_key(wf, branch_id, seq), epoch=1,
        result=ToolResult(status), detail={"attempt": attempt},
    )


# ------------------------------------------------------------------- records


def test_append_round_trips_every_field(journal):
    rid = journal.append(
        WF, "br-1", 4, "RESULT", tool_name="create_ticket",
        effect_type=EFFECT_TYPES["create_ticket"], args={"service": "payments-api"},
        key="wf-test:k", epoch=3, result=ToolResult("ok", {"id": 9}), detail={"note": "n"},
    )
    (rec,) = journal.records(WF)
    assert rec.record_id == rid
    assert (rec.workflow_id, rec.branch_id, rec.seq, rec.kind) == (WF, "br-1", 4, "RESULT")
    assert rec.tool_name == "create_ticket"
    assert rec.effect_type == EFFECT_TYPES["create_ticket"]
    assert rec.args == {"service": "payments-api"}
    assert (rec.key, rec.epoch) == ("wf-test:k", 3)
    assert rec.result == ToolResult("ok", {"id": 9})
    assert rec.detail == {"note": "n"}
    assert rec.ts > 0


def test_record_ids_increase_and_records_are_per_workflow(journal):
    a = journal.append("wf-a", "br", 0, "INTENT")
    b = journal.append("wf-b", "br", 0, "INTENT")
    c = journal.append("wf-a", "br", 1, "INTENT")
    assert a < b < c
    assert [r.record_id for r in journal.records("wf-a")] == [a, c]
    assert journal.workflows() == ["wf-a", "wf-b"]
    assert journal.latest_workflow() == "wf-a"


def test_an_empty_journal_has_no_latest_workflow(journal):
    assert journal.latest_workflow() is None
    assert journal.workflows() == []


def test_records_survive_a_reopen(db_path):
    """Durability is the whole point: a restarted process must see the journal."""
    j = Journal(db_path)
    j.append(WF, "br-1", 0, "INTENT", tool_name="fetch_alerts")
    j.close()
    again = Journal(db_path)
    assert [r.tool_name for r in again.records(WF)] == ["fetch_alerts"]
    again.close()


def test_journal_runs_in_wal_mode(journal):
    assert journal.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_synchronous_level_is_validated(db_path):
    with pytest.raises(ValueError):
        Journal(db_path, synchronous="SOMETIMES")
    j = Journal(db_path, synchronous="normal")
    assert j.synchronous == "NORMAL"
    j.close()


def test_read_only_journal_can_read_but_never_write(db_path):
    rw = Journal(db_path)
    rw.append(WF, "br-1", 0, "INTENT")
    ro = Journal(db_path, read_only=True)
    assert len(ro.records(WF)) == 1
    with pytest.raises(sqlite3.OperationalError):
        ro.append(WF, "br-1", 1, "INTENT")
    ro.close()
    rw.close()


# ------------------------------------------------------------------ branches


def test_branch_tree(journal):
    root = journal.create_branch(WF)
    child = journal.create_branch(WF, root.branch_id, 17, 1)
    assert root.branch_id.startswith("br-") and root.depth == 0 and root.status == "active"
    assert (child.parent_branch_id, child.fork_point_record_id, child.depth) == (
        root.branch_id, 17, 1,
    )
    assert [b.branch_id for b in journal.branches(WF)] == [root.branch_id, child.branch_id]
    assert journal.branch(child.branch_id) == child
    assert journal.branch("br-missing") is None


def test_active_branch_is_the_newest_active_one(journal):
    first = journal.create_branch(WF)
    second = journal.create_branch(WF, first.branch_id, None, 1)
    assert journal.active_branch(WF).branch_id == second.branch_id
    journal.set_branch_status(second.branch_id, "abandoned")
    assert journal.active_branch(WF).branch_id == first.branch_id
    journal.set_branch_status(first.branch_id, "compensated")
    assert journal.active_branch(WF) is None


def test_dead_branches_are_the_abandoned_ones(journal):
    a = journal.create_branch(WF)
    b = journal.create_branch(WF)
    c = journal.create_branch(WF)
    journal.set_branch_status(a.branch_id, "abandoned")
    journal.set_branch_status(b.branch_id, "abandoned_with_residue")
    journal.set_branch_status(c.branch_id, "compensated")
    assert journal.dead_branches(WF) == {a.branch_id, b.branch_id}


# --------------------------------------------------------------- replay aids


def test_next_seq_counts_only_intent_and_result(journal):
    assert journal.next_seq("br-1") == 0
    journal.append(WF, "br-1", 0, "INTENT")
    journal.append(WF, "br-1", 0, "RESULT", result=ToolResult("ok"))
    journal.append(WF, "br-1", 9, "ESCALATED")
    assert journal.next_seq("br-1") == 1


def test_completed_results_are_the_ok_ones_only(journal):
    _result(journal, "br-1", 0, "fetch_alerts")
    _result(journal, "br-1", 1, "fetch_service_context", status="unknown")
    _result(journal, "br-1", 2, "classify")
    # A later failure on the same step un-completes it.
    _result(journal, "br-1", 2, "classify", status="failed")
    assert set(journal.completed_results("br-1")) == {0}


def test_last_result_is_the_newest_for_that_step(journal):
    _result(journal, "br-1", 6, "page_oncall", status="unknown")
    newest = _result(journal, "br-1", 6, "page_oncall", status="ok")
    assert journal.last_result("br-1", 6).record_id == newest
    assert journal.last_result("br-1", 5) is None


# --------------------------------------------------------- barrier predicates


def _abandoned_with_ticket_and_post(journal):
    b = journal.create_branch(WF)
    _result(journal, b.branch_id, 4, "create_ticket")
    _result(journal, b.branch_id, 5, "post_to_channel")
    journal.set_branch_status(b.branch_id, "abandoned")
    return b


def test_uncompensated_lists_compensatable_effects_on_dead_branches(journal):
    b = _abandoned_with_ticket_and_post(journal)
    assert [r.tool_name for r in journal.uncompensated(WF)] == [
        "create_ticket", "post_to_channel",
    ]
    _comp(journal, b.branch_id, 5, "post_to_channel")
    assert [r.tool_name for r in journal.uncompensated(WF)] == ["create_ticket"]


def test_a_failed_compensation_does_not_count(journal):
    b = _abandoned_with_ticket_and_post(journal)
    _comp(journal, b.branch_id, 4, "create_ticket", status="failed")
    assert len(journal.uncompensated(WF)) == 2


def test_uncompensated_ignores_live_branches_failures_and_other_effect_types(journal):
    live = journal.create_branch(WF)
    _result(journal, live.branch_id, 4, "create_ticket")
    dead = journal.create_branch(WF)
    _result(journal, dead.branch_id, 3, "write_dedupe_marker")       # idempotent
    _result(journal, dead.branch_id, 4, "create_ticket", status="failed")
    _result(journal, dead.branch_id, 6, "page_oncall")               # irreversible
    journal.set_branch_status(dead.branch_id, "abandoned")
    assert journal.uncompensated(WF) == []


def test_uncompensated_reports_a_key_once(journal):
    b = journal.create_branch(WF)
    _result(journal, b.branch_id, 4, "create_ticket")
    _result(journal, b.branch_id, 4, "create_ticket")  # replayed result, same key
    journal.set_branch_status(b.branch_id, "abandoned")
    assert len(journal.uncompensated(WF)) == 1


def test_residue_is_irreversible_effects_stranded_on_dead_branches(journal):
    b = journal.create_branch(WF)
    _result(journal, b.branch_id, 6, "page_oncall", args={"rota": "rota-X"})
    assert journal.residue(WF) == []  # still live
    journal.set_branch_status(b.branch_id, "abandoned")
    assert [r.args["rota"] for r in journal.residue(WF)] == ["rota-X"]


def test_an_unknown_page_on_a_dead_branch_is_residue_but_a_failed_one_is_not(journal):
    unknown = journal.create_branch(WF)
    _result(journal, unknown.branch_id, 6, "page_oncall", status="unknown")
    failed = journal.create_branch(WF)
    _result(journal, failed.branch_id, 6, "page_oncall", status="failed")
    for b in (unknown, failed):
        journal.set_branch_status(b.branch_id, "abandoned")
    assert [r.branch_id for r in journal.residue(WF)] == [unknown.branch_id]


def test_compensations_lists_successful_ones(journal):
    b = _abandoned_with_ticket_and_post(journal)
    _comp(journal, b.branch_id, 5, "post_to_channel", status="failed")
    _comp(journal, b.branch_id, 5, "post_to_channel", attempt=2)
    assert [(r.tool_name, r.detail["attempt"]) for r in journal.compensations(WF)] == [
        ("post_to_channel", 2),
    ]


# ------------------------------------------------------------------- leases


def test_first_lease_is_epoch_one_and_renewal_increments(journal):
    assert journal.acquire_lease(WF, "orch-a", ttl=30) == 1
    assert journal.acquire_lease(WF, "orch-a", ttl=30) == 2
    info = journal.lease_info(WF)
    assert (info.owner, info.epoch) == ("orch-a", 2)
    assert info.expires > 0


def test_another_owner_is_refused_while_the_lease_is_live(journal):
    journal.acquire_lease(WF, "orch-a", ttl=30)
    with pytest.raises(LeaseUnavailable, match="held by orch-a"):
        journal.acquire_lease(WF, "orch-b", ttl=30)


def test_takeover_after_expiry_increments_the_epoch(journal):
    journal.acquire_lease(WF, "orch-a", ttl=30)
    journal.expire_lease(WF)
    assert journal.acquire_lease(WF, "orch-b", ttl=30) == 2
    assert journal.lease_info(WF).owner == "orch-b"


def test_leases_are_scoped_per_workflow(journal):
    journal.acquire_lease("wf-1", "orch-a", ttl=30)
    assert journal.acquire_lease("wf-2", "orch-b", ttl=30) == 1
    assert journal.lease_info("wf-3") is None


def test_lease_contention_across_two_connections(db_path):
    """Two journal handles stand in for two orchestrator processes on one file."""
    a, b = Journal(db_path), Journal(db_path)
    assert a.acquire_lease(WF, "orch-a", ttl=30) == 1
    with pytest.raises(LeaseUnavailable):
        b.acquire_lease(WF, "orch-b", ttl=30)
    a.expire_lease(WF)
    assert b.acquire_lease(WF, "orch-b", ttl=30) == 2
    a.close()
    b.close()


def test_concurrent_threads_never_share_an_epoch(journal):
    granted: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def contend(i: int) -> None:
        start.wait()
        try:
            epoch = journal.acquire_lease(WF, f"orch-{i}", ttl=0.0)
        except LeaseUnavailable:
            return
        with lock:
            granted.append(epoch)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted, "somebody must be able to take an expired lease"
    assert len(granted) == len(set(granted)), f"duplicate epoch issued: {sorted(granted)}"
    # Epochs are the fencing token, so they must also be contiguous and increasing.
    assert sorted(granted) == list(range(min(granted), min(granted) + len(granted)))


def test_one_journal_is_safe_to_share_between_threads(journal):
    """Regression: threads sharing one connection used to read each other's rows.

    Without the journal's lock this fails within a few hundred operations with
    ``IndexError`` or ``sqlite3.InterfaceError``, and the benchmark's worker threads
    died silently while it went on reporting a throughput.
    """
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def hammer(t: int) -> None:
        start.wait()
        branch = f"br-{t}"
        try:
            for seq in range(60):
                _result(journal, branch, seq, "create_ticket")
                assert journal.next_seq(branch) == seq + 1
                assert journal.last_result(branch, seq).tool_name == "create_ticket"
                assert all(len(r.key) > 0 for r in journal.records(WF)[-5:])
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(journal.records(WF)) == 6 * 60
