"""The HTTP topology for real: four service processes, real sockets, real timeouts.

Uses the session's service processes on free ports (see conftest.py), reset before
every test. These are the properties pytest could not reach before the refactor
(the old suite was in-process only).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from sluice._spawn import child_env, sluice_cmd
from sluice.cli import main as cli
from sluice.core.types import effect_key, workflow_id_for
from sluice.scenarios import make_ctx, scenario_poison

pytestmark = pytest.mark.http

WF = workflow_id_for("a-topology")


def key(seq: int, branch: str = "br-1") -> str:
    return effect_key(WF, branch, seq)


@pytest.fixture
def world(topology):
    from sluice.world.http_client import HttpWorld

    w = HttpWorld()
    yield w
    w.close()


@pytest.fixture
def oracle(topology):
    from sluice.world.http_client import HttpLedger

    led = HttpLedger()
    yield led
    led.close()


def wait_for(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- processes


def test_four_services_are_four_separate_processes(topology):
    import httpx

    pids = {name: httpx.get(f"{url}/health", timeout=2).json()["pid"]
            for name, url in topology["urls"].items()}
    assert len(set(pids.values())) == 4
    assert os.getpid() not in pids.values()
    assert pids == topology["pids"]


# ------------------------------------------------------ effects over sockets


def test_an_effect_crosses_a_socket_and_reaches_the_out_of_process_ledger(world, oracle):
    assert world.execute("create_ticket", {"service": "s"}, key(4), 1, 2.0).status == "ok"
    assert world.execute("create_ticket", {"service": "s"}, key(4), 1, 2.0).status == "ok"
    rows = oracle.effects(tool_name="create_ticket", workflow_id=WF)
    assert len(rows) == 1 and rows[0]["key"] == key(4)

    assert world.probe("create_ticket", key(4), 2.0) == "done"
    assert world.compensate("create_ticket", {}, key(4), 1, 2.0).status == "ok"
    assert [c["tool"] for c in oracle.compensations(workflow_id=WF)] == ["close_ticket"]
    assert oracle.counts(workflow_id=WF) == {}
    assert oracle.gross_counts(workflow_id=WF) == {"create_ticket": 1}


def test_the_service_fences_a_stale_epoch(world):
    world.execute("post_to_channel", {}, key(5), 4, 2.0)
    stale = world.execute("post_to_channel", {}, key(5, "br-2"), 3, 2.0)
    assert stale.status == "failed" and "fenced: stale epoch 3 < 4" in stale.error


def test_a_real_read_timeout_is_unknown_and_the_page_still_lands(world, oracle):
    world.set_faults("pager", {"latency_s": 1.0})
    t0 = time.monotonic()
    res = world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 0.3)
    assert res.status == "unknown" and "timeout" in res.error
    assert time.monotonic() - t0 < 0.9
    # Slow and crashed were indistinguishable to the caller; the page landed anyway.
    assert wait_for(lambda: oracle.effects("page_oncall", WF))
    assert world.probe("page_oncall", key(6), 2.0) == "unknown"


def test_a_refused_connection_is_failed_not_unknown(topology):
    from sluice.world.http_client import HttpWorld

    dead = HttpWorld(urls={**topology["urls"], "ticket": "http://127.0.0.1:9"})
    res = dead.execute("create_ticket", {}, key(4), 1, 1.0)
    dead.close()
    assert res.status == "failed" and "unreachable" in res.error


def test_health_reports_the_effect_services(world):
    h = world.health()
    assert h["world"] == "http"
    assert h["services"] == {"ticket": "up", "channel": "up", "pager": "up"}


# ------------------------------------------------- one World, two worlds


@pytest.mark.parametrize("mode", ["pinned", "naive", "sluice"])
def test_http_world_reproduces_the_in_process_result(topology, tmp_path, mode):
    """3.6: InProcessWorld and HttpWorld swap on a flag with identical semantics."""
    outcomes = {}
    for kind in ("inprocess", "http"):
        ctx = make_ctx(str(tmp_path / f"{kind}.db"), world_kind=kind)
        try:
            res = scenario_poison(ctx, mode, {})
            board = res["state"]["scoreboard"]
            outcomes[kind] = (res["result"]["outcome"],
                              (board["tickets"], board["posts"], board["pages"]),
                              res["verdict"]["pass"],
                              ctx.ledger.scoreboard(workflow_id=res["workflow_id"]))
        finally:
            ctx.close()
    assert outcomes["http"] == outcomes["inprocess"]


# ------------------------------------------------------ commands against it


def test_faults_command_sets_and_shows_presets(topology, world, capsys):
    assert cli(["faults", "poison"]) == 0
    assert world.get_faults("pager")["empty_rotas"] == ["rota-X"]
    assert cli(["faults"]) == 0
    assert capsys.readouterr().out.count("empty_rotas=['rota-X']") == 2 * 3
    assert cli(["faults", "clear", "--service", "pager"]) == 0
    assert world.get_faults("pager")["empty_rotas"] == []
    assert world.get_faults("ticket")["empty_rotas"] == ["rota-X"]


def test_demo_over_http(topology, tmp_path, capsys):
    assert cli(["demo", "--world", "http", "--db-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "tickets 1   posts 1   pages 1" in out and "unexplained violations:    0" in out


def test_smoke_passes_and_leaves_no_fault_behind(topology, world):
    proc = subprocess.run(sluice_cmd("smoke", "--http"), env=child_env(),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " 0 failed, 0 skipped" in proc.stdout
    # Regression: smoke used to leave rota-X poisoned for whatever ran next.
    for service in ("ticket", "channel", "pager"):
        assert world.get_faults(service)["empty_rotas"] == []


# ------------------------------------------------------------- supervisor


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.skipif(sys.platform != "linux",
                    reason="POSIX signals and /proc; on Windows SIGTERM is TerminateProcess")
def test_the_supervisor_stops_its_children_on_sigterm():
    """Regression: `kill <pid>` used to orphan the four service processes."""
    import httpx

    from sluice.world.http_client import DEFAULT_PORTS

    if any(_port_busy(p) for p in DEFAULT_PORTS.values()):
        pytest.skip("ports 8100-8103 are in use (a running topology?); not touching it")

    sup = subprocess.Popen(sluice_cmd("services"), env=child_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True)
    try:
        def all_up():
            try:
                return all(httpx.get(f"http://127.0.0.1:{p}/health", timeout=0.5)
                           .status_code == 200 for p in DEFAULT_PORTS.values())
            except Exception:
                return False

        assert wait_for(all_up, timeout_s=30), "supervisor never brought the services up"
        children = [httpx.get(f"http://127.0.0.1:{p}/health", timeout=2).json()["pid"]
                    for p in DEFAULT_PORTS.values()]

        sup.send_signal(signal.SIGTERM)
        assert sup.wait(timeout=15) == 0

        def gone(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            # Exited but not yet reaped by its (now dead) parent counts as gone too.
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().split()[2] == "Z"

        assert wait_for(lambda: all(gone(pid) for pid in children), timeout_s=10)
        assert not any(_port_busy(p) for p in DEFAULT_PORTS.values())
    finally:
        if sup.poll() is None:
            os.killpg(sup.pid, signal.SIGKILL)
