"""The dashboard's HTTP API: the read path, the audit export and the control endpoints.

The governing rule (6.6): the dashboard reads the journal and never drives execution.
The control endpoints exist only with --allow-control, and launch the same scenario
code a terminal would.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sluice.core.journal import Journal  # noqa: E402
from sluice.dashboard import DEFAULT_PANES, LIVE_SCENARIOS, create_app  # noqa: E402
from sluice.scenarios import MODES, SCENARIOS, run_scenario  # noqa: E402

pytestmark = pytest.mark.http


@pytest.fixture
def panes(tmp_path) -> dict[str, str]:
    return {mode: str(tmp_path / f"demo-{mode}.db") for mode in MODES}


@pytest.fixture
def filled_panes(panes):
    for mode, db in panes.items():
        run_scenario("poison", mode, db)["ctx"].close()
    return panes


def wait_until_idle(client, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while client.get("/api/health").json()["running"]:
        assert time.monotonic() < deadline, "control worker never finished"
        time.sleep(0.05)


# ------------------------------------------------------------------ read path


def test_index_serves_the_page(panes):
    resp = TestClient(create_app(panes)).get("/")
    assert resp.status_code == 200 and "<title>Sluice</title>" in resp.text


def test_state_derives_every_pane_from_its_journal(filled_panes):
    state = TestClient(create_app(filled_panes)).get("/api/state").json()
    assert state["order"] == list(MODES) and "live" not in state

    pinned, naive, sluice = (state["panes"][m] for m in MODES)
    assert (pinned["scoreboard"]["pages"], pinned["running"]) == (0, True)
    assert naive["scoreboard"]["tickets"] == 2
    assert sluice["barrier"]["state"] == "released"
    assert [p["rota"] for p in sluice["phones"]] == ["rota-Y"]
    # The verdict comes from the sidecar the grading process wrote; this one cannot grade.
    assert sluice["verdict"]["pass"] is True and pinned["verdict"]["pass"] is False


def test_a_pane_without_a_journal_says_so(panes):
    state = TestClient(create_app(panes)).get("/api/state").json()
    assert all(p["missing"] and p["empty"] for p in state["panes"].values())


def _record_count(db: str) -> int:
    reader = Journal(db, read_only=True)
    try:
        return len(reader.records(reader.latest_workflow()))
    finally:
        reader.close()


def test_reading_never_writes_to_a_journal(filled_panes):
    db = filled_panes["sluice"]
    before, stat = _record_count(db), os.stat(db)
    client = TestClient(create_app(filled_panes))
    for _ in range(3):
        client.get("/api/state")
        client.get("/api/audit", params={"mode": "sluice"})
    assert _record_count(db) == before
    assert os.stat(db).st_mtime_ns == stat.st_mtime_ns


def test_audit_returns_the_post_mortem(filled_panes):
    client = TestClient(create_app(filled_panes))
    text = client.get("/api/audit", params={"mode": "sluice"}).text
    assert text.startswith("# Incident post-mortem") and "## Compensation" in text
    assert client.get("/api/audit", params={"mode": "optimistic"}).status_code == 404


def test_health_advertises_scenarios_from_the_registry(panes):
    health = TestClient(create_app(panes, live_db="")).get("/api/health").json()
    assert health["control_enabled"] is False
    assert health["scenarios"] == sorted(SCENARIOS)
    assert health["live_scenarios"] == sorted(LIVE_SCENARIOS) == ["poison", "redelivery",
                                                                   "zombie"]
    assert health["running"] == {}


def test_the_live_pane_appears_once_its_journal_exists(panes, tmp_path):
    live = str(tmp_path / "shared.db")
    client = TestClient(create_app(panes, live_db=live))
    assert client.get("/api/state").json()["live"]["missing"] is True
    run_scenario("poison", "sluice", live)["ctx"].close()
    live_state = client.get("/api/state").json()["live"]
    assert live_state["is_live"] is True and live_state["scoreboard"]["pages"] == 1


def test_default_panes_are_the_three_demo_journals():
    assert DEFAULT_PANES == {m: f"demo-{m}.db" for m in MODES}


# --------------------------------------------------------------- control path


def test_control_endpoints_do_not_exist_without_the_flag(panes):
    client = TestClient(create_app(panes))
    assert client.post("/api/run", params={"scenario": "poison"}).status_code == 404
    assert client.post("/api/run_live").status_code == 404


def test_run_drives_all_three_panes(panes):
    client = TestClient(create_app(panes, allow_control=True))
    assert client.post("/api/run", params={"scenario": "residue"}).json() == {
        "launched": "residue", "panes": list(MODES)}
    wait_until_idle(client)
    state = client.get("/api/state").json()["panes"]
    assert state["sluice"]["scoreboard"]["pages"] == 2
    assert state["naive"]["verdict"]["pass"] is False


def test_run_rejects_an_unknown_scenario(panes):
    client = TestClient(create_app(panes, allow_control=True))
    assert client.post("/api/run", params={"scenario": "chaos"}).status_code == 400


def test_run_live_refuses_what_the_distributed_path_cannot_express(panes, tmp_path):
    no_live = TestClient(create_app(panes, allow_control=True))
    assert no_live.post("/api/run_live").status_code == 400

    client = TestClient(create_app(panes, allow_control=True,
                                   live_db=str(tmp_path / "shared.db")))
    resp = client.post("/api/run_live", params={"scenario": "residue"})
    assert resp.status_code == 400
    assert "cannot run over the distributed path" in resp.json()["detail"]
    assert client.post("/api/run_live", params={"mode": "optimistic"}).status_code == 400
