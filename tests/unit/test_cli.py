"""The ``sluice`` command: the dispatcher, and each subcommand that can run without
a live topology. Commands that need services or Redis are exercised end to end in
tests/integration/.
"""

from __future__ import annotations

import os
import signal
import subprocess

import pytest

from sluice import __version__
from sluice._spawn import child_env, sluice_cmd
from sluice.cli import COMMANDS, main
from sluice.cli import faults as faults_cli
from sluice.cli import orchestrator as orchestrator_cli
from sluice.cli import producer as producer_cli
from sluice.core.tools import DEMO_ALERT
from sluice.ingest import InProcessAlertSource
from sluice.world import FAULT_PRESETS, NO_FAULTS

NO_REDIS = "redis://127.0.0.1:1/0"


@pytest.fixture
def no_services(monkeypatch):
    """Point every service URL at a port nothing listens on."""
    for name in ("ledger", "ticket", "channel", "pager"):
        monkeypatch.setenv(f"SLUICE_{name.upper()}_URL", "http://127.0.0.1:1")


# ----------------------------------------------------------------- dispatcher


def test_no_command_prints_usage_and_fails(capsys):
    assert main([]) == 2
    out = capsys.readouterr().out
    assert out.startswith("usage: sluice <command>")
    for command in COMMANDS:
        assert f"  {command} " in out


def test_help_and_version(capsys):
    assert main(["--help"]) == 0
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"sluice {__version__}"


def test_unknown_command(capsys):
    assert main(["deploy"]) == 2
    assert "unknown command 'deploy'" in capsys.readouterr().err


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_every_command_has_its_own_help(command, capsys):
    with pytest.raises(SystemExit) as exit_:
        main([command, "--help"])
    assert exit_.value.code == 0
    assert f"usage: sluice {command}" in capsys.readouterr().out


def test_python_dash_m_works_without_an_install():
    proc = subprocess.run(sluice_cmd("--help"), env=child_env(), capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "usage: sluice <command>" in proc.stdout


# ----------------------------------------------------------------------- demo


def test_demo_runs_the_three_panes(tmp_path, capsys):
    assert main(["demo", "--db-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "tickets 1   posts 1   pages 0" in out      # pinned: livelocked
    assert "tickets 2   posts 2   pages 1" in out      # naive: duplicated
    assert "tickets 1   posts 1   pages 1" in out      # sluice
    assert "unexplained violations:    0" in out
    for mode in ("pinned", "naive", "sluice"):
        assert (tmp_path / f"demo-{mode}.db").exists()
        assert (tmp_path / f"demo-{mode}.db.verdict.json").exists()


def test_demo_narrates_and_writes_post_mortems(tmp_path, capsys):
    audit = tmp_path / "audit"
    code = main(["demo", "--db-dir", str(tmp_path), "--scenario", "residue",
                 "--modes", "sluice", "--narrate", "--verbose", "--audit-dir", str(audit)])
    assert code == 0
    out = capsys.readouterr().out
    assert "** FORK to P1 at depth 1" in out and "## GATE LIFTS" in out
    assert "residue:       page_oncall to rota-X (permanent, superseded by a later page)" in out
    post_mortem = (audit / "residue-sluice.md").read_text(encoding="utf-8")
    assert post_mortem.startswith("# Incident post-mortem")


def test_demo_rejects_an_unknown_mode(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["demo", "--modes", "pinned,optimistic"])
    assert exit_.value.code == 2


def test_demo_failover(tmp_path, capsys):
    assert main(["demo", "--failover", "--db-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "deposed leader fenced by the effect layer: True" in out
    assert "pages sent: 1 ['rota-Y']" in out


def test_demo_over_http_with_no_services_fails_cleanly(tmp_path, capsys, no_services):
    pytest.importorskip("httpx")
    assert main(["demo", "--world", "http", "--db-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "services down" in err and "sluice services" in err


# --------------------------------------------------------------- orchestrator


def test_orchestrator_handles_a_queued_alert_and_grades_it(tmp_path, monkeypatch, capsys):
    source = InProcessAlertSource([DEMO_ALERT])
    monkeypatch.setattr(orchestrator_cli, "alert_source", lambda **_: source)
    db = str(tmp_path / "shared.db")
    before = signal.getsignal(signal.SIGINT)

    code = main(["orchestrator", "--world", "inprocess", "--source", "inprocess",
                 "--once", "--db", db, "--narrate"])
    out = capsys.readouterr().out

    assert code == 0
    assert "barrier_released page_oncall" in out
    assert "a-1001: completed   rota=rota-Y" in out and "EEO PASS" in out
    assert "handled 1 alert(s)" in out
    assert source.acked == {"a-1001"}
    assert os.path.exists(db + ".verdict.json")
    # The process-wide handlers it installed are put back.
    assert signal.getsignal(signal.SIGINT) is before


def test_orchestrator_does_not_ack_what_it_could_not_lead(tmp_path, monkeypatch, capsys):
    from sluice.core.journal import Journal
    from sluice.core.types import workflow_id_for

    db = str(tmp_path / "shared.db")
    other = Journal(db)
    other.acquire_lease(workflow_id_for(DEMO_ALERT.alert_id), "orch-b", ttl=60)
    other.close()

    source = InProcessAlertSource([DEMO_ALERT])
    monkeypatch.setattr(orchestrator_cli, "alert_source", lambda **_: source)
    main(["orchestrator", "--world", "inprocess", "--source", "inprocess", "--once",
          "--db", db, "--owner", "orch-a"])
    assert "a-1001: not leader" in capsys.readouterr().out
    # Left pending, so the leader (or a survivor) can claim it.
    assert [p["message_id"] for p in source.pending()] == ["a-1001"]


def test_orchestrator_with_nothing_queued_exits_cleanly(tmp_path, capsys):
    code = main(["orchestrator", "--world", "inprocess", "--source", "inprocess", "--once",
                 "--db", str(tmp_path / "shared.db")])
    assert code == 0
    assert "handled 0 alert(s)" in capsys.readouterr().out


# ----------------------------------------------------------- producer, faults


def test_producer_needs_redis(monkeypatch, capsys):
    monkeypatch.setenv("REDIS_URL", NO_REDIS)
    assert main(["producer", "--count", "1"]) == 2
    assert "redis unavailable" in capsys.readouterr().err


def test_producer_alerts():
    assert producer_cli.make_alert(0, demo=True) == DEMO_ALERT
    alert = producer_cli.make_alert(3, demo=False)
    assert alert.alert_id == "a-2003" and alert.service in producer_cli.SERVICES


def test_faults_describe_only_what_is_switched_on():
    assert faults_cli.describe(NO_FAULTS) == "no faults"
    assert faults_cli.describe(FAULT_PRESETS["poison"]) == "empty_rotas=['rota-X']"
    zombie = faults_cli.describe(FAULT_PRESETS["zombie"])
    assert "timeout_tools=['page_oncall']" in zombie and "late_delivery_delay_s=8.0" in zombie


def test_faults_reports_unreachable_services(no_services, capsys):
    pytest.importorskip("httpx")
    assert main(["faults", "poison", "--service", "pager"]) == 1
    err = capsys.readouterr().err
    assert "pager    unreachable" in err and "sluice services" in err


# ----------------------------------------------------------- smoke, dashboard


def test_smoke_fails_loudly_when_the_services_are_down(no_services, capsys):
    pytest.importorskip("httpx")
    assert main(["smoke", "--http"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  services reachable" in out


def test_dashboard_builds_its_app_and_serves_it(tmp_path, monkeypatch, capsys):
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))

    code = main(["dashboard", "--db-dir", str(tmp_path), "--port", "8765",
                 "--allow-control", "--live-db", ""])
    assert code == 0
    assert served["port"] == 8765 and served["app"].title == "sluice-dashboard"
    out = capsys.readouterr().out
    assert "control    ON" in out and "sluice demo --db-dir" in out
