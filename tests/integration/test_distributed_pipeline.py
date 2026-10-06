"""The whole distributed path, as separate OS processes:

    sluice producer -> Redis stream -> sluice orchestrator -> HTTP services
                                                                   -> ledger (oracle)

Automates what scripts/verify.sh checks by hand (fresh run, replay, redelivery), plus
the leader failover the old docs called "not automatable": SIGKILL the leader in the
middle of compensation and let a standby finish the workflow from the shared journal.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from sluice._spawn import child_env, sluice_cmd
from sluice.cli import main as cli
from sluice.core.journal import Journal
from sluice.core.tools import DEMO_ALERT
from sluice.core.types import workflow_id_for

pytestmark = [pytest.mark.http, pytest.mark.redis, pytest.mark.slow]

WF = workflow_id_for(DEMO_ALERT.alert_id)
EXPECTED_COUNTS = {"create_ticket": 1, "post_to_channel": 1, "page_oncall": 1,
                   "update_status_page": 1, "write_dedupe_marker": 2}


@pytest.fixture
def pipeline(topology, redis_url, tmp_path):
    """A clean topology and stream, the poison fault set explicitly, a fresh journal."""
    from sluice.world.http_client import HttpLedger

    assert cli(["faults", "poison"]) == 0
    oracle = HttpLedger()
    yield {"db": str(tmp_path / "shared.db"), "oracle": oracle}
    oracle.close()


def run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    proc = subprocess.run(sluice_cmd(*args), env=child_env(), capture_output=True,
                          text=True, timeout=timeout)
    assert proc.returncode == 0, f"{args}\n{proc.stdout}\n{proc.stderr}"
    return proc


def orchestrate(db: str, *extra: str) -> str:
    return run("orchestrator", "--source", "redis", "--world", "http", "--once",
               "--narrate", "--db", db, *extra).stdout


def test_fresh_run_then_replay_then_redelivery(pipeline):
    db, oracle = pipeline["db"], pipeline["oracle"]

    # Fresh: the poison step, end to end across four processes and a stream.
    run("producer", "--demo", "--count", "1")
    out = orchestrate(db, "--owner", "orch-a")
    assert "[ingest] redis stream" in out, "fell back to the in-process queue"
    for line in ("barrier_blocked 2 uncompensated effects on abandoned branch",
                 "compensated post_to_channel", "compensated create_ticket",
                 "barrier_released page_oncall", "a-1001: completed   rota=rota-Y"):
        assert line in out
    assert "EEO PASS" in out
    assert oracle.counts(workflow_id=WF) == EXPECTED_COUNTS
    assert os.path.exists(db + ".verdict.json")

    # Replay: the same alert again finds every step already committed.
    run("producer", "--demo", "--count", "1")
    out = orchestrate(db, "--owner", "orch-a")
    assert out.count("step_replayed") == 8
    assert oracle.counts(workflow_id=WF) == EXPECTED_COUNTS

    # At-least-once: published twice more, still exactly one of everything.
    run("producer", "--demo", "--count", "1", "--redeliver")
    out = orchestrate(db, "--owner", "orch-a")
    assert "handled 2 alert(s)" in out
    assert oracle.counts(workflow_id=WF) == EXPECTED_COUNTS


def test_kill_9_the_leader_mid_compensation_and_the_standby_finishes(pipeline, topology):
    from sluice.world.http_client import HttpWorld

    db, oracle = pipeline["db"], pipeline["oracle"]
    world = HttpWorld()
    for service in ("ticket", "channel", "pager"):  # slow enough to kill mid-flight
        world.set_faults(service, {"latency_s": 0.25})
    world.close()

    run("producer", "--demo", "--count", "1")
    leader = subprocess.Popen(
        sluice_cmd("orchestrator", "--owner", "orch-a", "--source", "redis",
                       "--world", "http", "--narrate", "--lease-ttl", "2",
                       "--poll-s", "0.2", "--db", db),
        env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen = []
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = leader.stdout.readline()
            seen.append(line)
            if "barrier_blocked" in line or not line:
                break
        assert any("barrier_blocked" in s for s in seen), "".join(seen)
        leader.kill()  # SIGKILL (TerminateProcess on Windows): no chance to clean up
        leader.wait(timeout=10)
    finally:
        if leader.poll() is None:
            leader.kill()
        leader.stdout.close()

    # The workflow is stranded: its alert pending, its lease held by a dead process.
    journal = Journal(db, read_only=True)
    assert journal.lease_info(WF).owner == "orch-a"
    journal.close()
    time.sleep(2.5)  # let the dead leader's 2s lease expire

    out = orchestrate(db, "--owner", "orch-b", "--claim-idle-ms", "500")
    assert "reclaimed a-1001 from a stalled consumer" in out
    assert "a-1001: completed   rota=rota-Y" in out and "EEO PASS" in out

    journal = Journal(db, read_only=True)
    lease = journal.lease_info(WF)
    journal.close()
    assert lease.owner == "orch-b" and lease.epoch >= 2
    # Ground truth: one ticket, one post, one page, to the right rota.
    pages = oracle.effects(tool_name="page_oncall", workflow_id=WF)
    assert [p["args"]["rota"] for p in pages] == ["rota-Y"]
    assert oracle.scoreboard(workflow_id=WF) == (1, 1, 1)


def test_dashboard_run_live_drives_the_real_pipeline(pipeline, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sluice.dashboard import create_app

    live = str(tmp_path / "live.db")
    panes = {m: str(tmp_path / f"demo-{m}.db") for m in ("pinned", "naive", "sluice")}
    client = TestClient(create_app(panes, allow_control=True, live_db=live))

    resp = client.post("/api/run_live", params={"scenario": "poison"})
    assert resp.json() == {"launched": "poison", "mode": "sluice", "db": live}
    deadline = time.monotonic() + 120
    while client.get("/api/health").json()["running"]:
        assert time.monotonic() < deadline, "the live run never finished"
        time.sleep(0.2)

    pane = client.get("/api/state").json()["live"]
    assert pane["is_live"] and not pane["missing"]
    assert pane["scoreboard"]["pages"] == 1 and pane["barrier"]["state"] == "released"
    assert pane["verdict"]["pass"] is True
