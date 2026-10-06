"""The effect services and the ledger service, through their HTTP API (in-process).

Starlette's TestClient drives each FastAPI app directly, so request validation, status
codes and the fencing response are covered without sockets. Real sockets, real
timeouts and the services talking to the ledger are in test_http_topology.py.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sluice.core.types import effect_key, workflow_id_for  # noqa: E402
from sluice.world.services import (  # noqa: E402
    LedgerClient,
    build_app,
    create_effect_service,
    create_ledger_service,
)

pytestmark = pytest.mark.http

WF = workflow_id_for("a-api")
NOWHERE = "http://127.0.0.1:1"  # a ledger that is down: effects buffer instead


def call(tool, seq=4, epoch=1, branch="br-1", **extra):
    return {"tool": tool, "args": extra.pop("args", {}), "key": effect_key(WF, branch, seq),
            "epoch": epoch, "workflow_id": WF, **extra}


@pytest.fixture
def ticket():
    return TestClient(create_effect_service("ticket", NOWHERE))


@pytest.fixture
def pager():
    return TestClient(create_effect_service("pager", NOWHERE))


@pytest.fixture
def ledger_api():
    return TestClient(create_ledger_service())


# ------------------------------------------------------------ effect service


def test_execute_commits_once_per_key(ticket):
    first = ticket.post("/execute", json=call("create_ticket"))
    again = ticket.post("/execute", json=call("create_ticket"))
    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == again.json()["status"] == "ok"
    health = ticket.get("/health").json()
    assert health["committed_keys"] == 1
    # The ledger is down, so the effect waits in the service's buffer, not lost.
    assert health["ledger_buffer"] == 1


def test_a_stale_epoch_gets_409_from_the_service(ticket):
    assert ticket.post("/execute", json=call("create_ticket", epoch=5)).status_code == 200
    stale = ticket.post("/execute", json=call("create_ticket", seq=5, epoch=2))
    assert stale.status_code == 409
    assert stale.json()["error"] == f"fenced: stale epoch 2 < 5 for {WF}"
    assert ticket.get("/health").json()["epoch_seen"] == {WF: 5}


def test_a_tool_the_service_does_not_own_is_404(ticket):
    resp = ticket.post("/execute", json=call("page_oncall", seq=6))
    assert resp.status_code == 404 and "not served by ticket" in resp.json()["error"]
    assert ticket.get("/probe", params={"tool": "page_oncall", "key": "k"}).status_code == 404


def test_a_malformed_call_is_rejected_by_validation(ticket):
    assert ticket.post("/execute", json={"tool": "create_ticket"}).status_code == 422


def test_compensate_and_probe(ticket):
    body = call("create_ticket")
    ticket.post("/execute", json=body)
    probe = {"tool": "create_ticket", "key": body["key"]}
    assert ticket.get("/probe", params=probe).json() == {"status": "done"}

    undo = ticket.post("/compensate", json=body).json()
    assert undo["status"] == "ok" and undo["value"]["via"] == "close_ticket"
    assert ticket.get("/probe", params=probe).json() == {"status": "not_done"}


def test_the_pager_cannot_be_probed(pager):
    body = call("page_oncall", seq=6, args={"rota": "rota-Y"})
    assert pager.post("/execute", json=body).json()["status"] == "ok"
    assert pager.get("/probe", params={"tool": "page_oncall", "key": body["key"]}).json() == {
        "status": "unknown"}


def test_faults_are_set_read_and_applied(pager):
    assert pager.get("/admin/faults").json()["empty_rotas"] == []
    pager.post("/admin/faults", json={"empty_rotas": ["rota-X"]})
    assert pager.get("/admin/faults").json()["empty_rotas"] == ["rota-X"]
    res = pager.post("/execute", json=call("page_oncall", seq=6, args={"rota": "rota-X"}))
    assert res.json() == {"status": "failed", "value": None, "error": "no responder on rota-X"}


def test_the_service_does_not_enforce_the_callers_deadline(pager):
    """Slow must stay indistinguishable from crashed: only the caller decides 'unknown'."""
    pager.post("/admin/faults", json={"latency_s": 0.2})
    t0 = time.monotonic()
    res = pager.post("/execute", json=call("page_oncall", seq=6, timeout_s=0.01,
                                           args={"rota": "rota-Y"}))
    assert res.json()["status"] == "ok"
    assert time.monotonic() - t0 >= 0.2


def test_reset_clears_keys_and_epochs_but_not_faults(ticket):
    ticket.post("/execute", json=call("create_ticket", epoch=7))
    ticket.post("/admin/faults", json={"fail_tools": ["post_to_channel"]})
    assert ticket.post("/admin/reset").json() == {"reset": "ticket"}
    health = ticket.get("/health").json()
    assert health["committed_keys"] == 0 and health["epoch_seen"] == {}
    # Faults survive a reset -- which is why `sluice faults` exists.
    assert health["faults"]["fail_tools"] == ["post_to_channel"]
    assert ticket.post("/execute", json=call("create_ticket", epoch=1)).status_code == 200


def test_flush_late_fires_pending_late_deliveries(pager):
    pager.post("/admin/faults", json={"timeout_tools": ["page_oncall"],
                                      "late_delivery_tools": ["page_oncall"],
                                      "late_delivery_delay_s": 60})
    res = pager.post("/execute", json=call("page_oncall", seq=6, args={"rota": "rota-Y"}))
    assert res.json()["status"] == "unknown"
    assert pager.post("/admin/flush_late").json() == {"fired": 1}
    assert pager.get("/health").json()["committed_keys"] == 1


def test_health_identifies_the_process():
    import os

    health = TestClient(build_app("channel", NOWHERE)).get("/health").json()
    assert health["service"] == "channel" and health["pid"] == os.getpid()
    assert health["tools"] == ["post_to_channel", "update_status_page"]


# ------------------------------------------------------------- ledger service


def entry(tool, seq, kind="effect", ts=None, workflow_id=WF):
    return {"ts": ts or time.time(), "tool": tool, "key": effect_key(workflow_id, "br", seq),
            "workflow_id": workflow_id, "args": {}, "kind": kind}


def test_ledger_records_batches_and_deduplicates_resent_entries(ledger_api):
    batch = [entry("create_ticket", 4, ts=1.0), entry("post_to_channel", 5, ts=2.0)]
    assert ledger_api.post("/record", json={"entries": batch}).json() == {"total": 2}
    # A client that never saw the acknowledgement re-sends its whole buffer.
    assert ledger_api.post("/record", json={"entries": batch}).json() == {"total": 2}
    # A single bare entry is accepted too.
    assert ledger_api.post("/record", json=entry("page_oncall", 6, ts=3.0)).json() == {
        "total": 3}


def test_ledger_queries(ledger_api):
    other = workflow_id_for("a-other")
    ledger_api.post("/record", json={"entries": [
        entry("create_ticket", 4, ts=1.0),
        entry("close_ticket", 4, kind="compensation", ts=2.0),
        entry("create_ticket", 14, ts=3.0),
        entry("page_oncall", 6, ts=4.0, workflow_id=other),
    ]})
    assert len(ledger_api.get("/effects", params={"tool": "create_ticket"}).json()) == 2
    assert len(ledger_api.get("/effects", params={"workflow_id": other}).json()) == 1
    assert len(ledger_api.get("/compensations").json()) == 1
    assert ledger_api.get("/counts", params={"workflow_id": WF}).json() == {"create_ticket": 1}
    assert ledger_api.get("/counts", params={"net": "false", "workflow_id": WF}).json() == {
        "create_ticket": 2}
    assert ledger_api.get("/health").json()["entries"] == 4
    ledger_api.post("/reset")
    assert ledger_api.get("/health").json()["entries"] == 0


def test_ledger_client_buffers_until_the_ledger_is_back(ledger_api):
    client = LedgerClient(NOWHERE)
    client.record("create_ticket", effect_key(WF, "br", 4), {})
    assert client.depth() == 1  # ledger unreachable: kept, not dropped

    client.client = ledger_api  # the ledger comes back
    client.record("post_to_channel", effect_key(WF, "br", 5), {})
    assert client.depth() == 0
    assert ledger_api.get("/health").json()["entries"] == 2
