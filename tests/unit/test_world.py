"""The in-process effect layer: InProcessWorld, fault injection, the ground-truth ledger."""

from __future__ import annotations

import threading
import time

import pytest

from sluice.core.types import effect_key, workflow_id_for
from sluice.world import (
    FAULT_PRESETS,
    NO_FAULTS,
    FaultConfig,
    GroundTruthLedger,
)

WF = workflow_id_for("a-test")


def key(seq: int, branch: str = "br-1", wf: str = WF) -> str:
    return effect_key(wf, branch, seq)


# ------------------------------------------------------------------ execute


def test_a_clean_effect_commits_once_and_reaches_the_ledger(world, ledger):
    res = world.execute("create_ticket", {"service": "s"}, key(4), 1, 2.0)
    assert res.status == "ok"
    assert res.value["key"] == key(4)
    assert [(e["tool"], e["key"], e["workflow_id"]) for e in ledger.effects()] == [
        ("create_ticket", key(4), WF),
    ]


def test_pure_tools_never_reach_the_ledger(world, ledger):
    for seq, tool in enumerate(["fetch_alerts", "fetch_service_context", "classify"]):
        assert world.execute(tool, {}, key(seq), 1, 2.0).status == "ok"
    assert ledger.effects() == []


def test_the_idempotency_key_collapses_a_retry(world, ledger):
    first = world.execute("post_to_channel", {"incident": "i"}, key(5), 1, 2.0)
    again = world.execute("post_to_channel", {"incident": "i"}, key(5), 1, 2.0)
    assert first.status == again.status == "ok"
    assert len(ledger.effects()) == 1


def test_a_stale_epoch_is_fenced_per_workflow(world, ledger):
    assert world.execute("create_ticket", {}, key(4, "br-b"), 2, 2.0).status == "ok"
    stale = world.execute("page_oncall", {"rota": "rota-X"}, key(6, "br-a"), 1, 2.0)
    assert stale.status == "failed" and "stale epoch 1" in stale.error
    # The same epoch is not stale, and a different workflow has its own high-water mark.
    assert world.execute("post_to_channel", {}, key(5, "br-b"), 2, 2.0).status == "ok"
    other = effect_key(workflow_id_for("a-other"), "br-c", 6)
    assert world.execute("page_oncall", {"rota": "rota-Y"}, other, 1, 2.0).status == "ok"
    assert world.epoch_seen[WF] == 2
    assert [e["tool"] for e in ledger.effects()] == [
        "create_ticket", "post_to_channel", "page_oncall",
    ]


@pytest.mark.parametrize("down", ["ticket", "create_ticket"])
def test_a_down_service_fails_fast_by_service_or_tool_name(world, faults, ledger, down):
    faults.down_services = {down}
    faults.latency_s = 0.5
    t0 = time.monotonic()
    res = world.execute("create_ticket", {}, key(4), 1, 2.0)
    assert res.status == "failed" and "unavailable" in res.error
    # A refused connection does not make the caller wait out the latency.
    assert time.monotonic() - t0 < 0.4
    assert ledger.effects() == []


def test_fail_tools_reject_the_request(world, faults):
    faults.fail_tools = {"create_ticket"}
    res = world.execute("create_ticket", {}, key(4), 1, 2.0)
    assert res.status == "failed" and "rejected" in res.error


def test_an_empty_rota_fails_only_that_page(world, faults, ledger):
    faults.empty_rotas = {"rota-X"}
    bad = world.execute("page_oncall", {"rota": "rota-X"}, key(6), 1, 2.0)
    assert bad.status == "failed" and bad.error == "no responder on rota-X"
    good = world.execute("page_oncall", {"rota": "rota-Y"}, key(6, "br-2"), 1, 2.0)
    assert good.status == "ok"
    assert [e["args"]["rota"] for e in ledger.effects()] == ["rota-Y"]


def test_an_injected_timeout_is_unknown_not_failed(world, faults, ledger):
    faults.timeout_tools = {"page_oncall"}
    res = world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0)
    assert res.status == "unknown" and "timeout" in res.error
    assert ledger.effects() == [] and key(6) not in world.applied


def test_latency_the_caller_will_not_wait_out_is_unknown(world, faults):
    faults.latency_s = 0.05
    assert world.execute("create_ticket", {}, key(4), 1, 0.05).status == "unknown"


def test_latency_inside_the_deadline_still_commits(world, faults):
    faults.latency_s = 0.02
    t0 = time.monotonic()
    assert world.execute("create_ticket", {}, key(4), 1, 1.0).status == "ok"
    assert time.monotonic() - t0 >= 0.02


def test_a_zero_timeout_means_no_deadline(world, faults):
    """The HTTP services pass 0.0: slow must stay indistinguishable from crashed."""
    faults.latency_s = 0.02
    assert world.execute("create_ticket", {}, key(4), 1, 0.0).status == "ok"


# ------------------------------------------------------------- late delivery


def test_a_timed_out_effect_can_land_late_and_is_caught_on_the_key(world, faults, ledger):
    faults.timeout_tools = {"page_oncall"}
    faults.late_delivery_tools = {"page_oncall"}
    faults.late_delivery_delay_s = 60.0  # never on its own during the test

    assert world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0).status == "unknown"
    assert len(world.pending_late) == 1 and ledger.effects() == []

    assert world.flush_late_deliveries() == 1
    assert [e["tool"] for e in ledger.effects()] == ["page_oncall"]
    assert world.flush_late_deliveries() == 0

    # Re-driving the same key finds the late effect instead of paging again.
    faults.timeout_tools = set()
    again = world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0)
    assert again.status == "ok" and again.value.get("late") is True
    assert len(ledger.effects()) == 1


def test_a_late_delivery_never_overwrites_an_effect_that_already_landed(world, faults, ledger):
    faults.timeout_tools = {"page_oncall"}
    faults.late_delivery_tools = {"page_oncall"}
    faults.late_delivery_delay_s = 60.0
    world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0)
    faults.timeout_tools = set()
    world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0)  # lands normally
    world.flush_late_deliveries()
    assert len(ledger.effects()) == 1


def test_late_delivery_fires_on_its_own_after_the_delay(world, faults, ledger):
    faults.timeout_tools = {"page_oncall"}
    faults.late_delivery_tools = {"page_oncall"}
    faults.late_delivery_delay_s = 0.05
    world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0)
    deadline = time.monotonic() + 2.0
    while not ledger.effects() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(ledger.effects()) == 1


# -------------------------------------------------------------- compensation


def test_compensation_undoes_the_effect_and_is_recorded_under_the_same_key(world, ledger):
    world.execute("create_ticket", {"service": "s"}, key(4), 1, 2.0)
    res = world.compensate("create_ticket", {"service": "s"}, key(4), 1, 2.0)
    assert res.status == "ok" and res.value == {"compensated": key(4), "via": "close_ticket"}
    assert key(4) not in world.applied
    (comp,) = ledger.compensations()
    assert (comp["tool"], comp["key"], comp["kind"]) == ("close_ticket", key(4), "compensation")
    assert ledger.counts() == {}
    assert ledger.gross_counts() == {"create_ticket": 1}


def test_an_irreversible_effect_has_no_inverse(world):
    res = world.compensate("page_oncall", {}, key(6), 1, 2.0)
    assert res.status == "failed" and "no inverse" in res.error


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda f: setattr(f, "down_services", {"ticket"}), "unavailable for compensation"),
        (lambda f: setattr(f, "fail_compensation_tools", {"create_ticket"}), "rejected"),
    ],
)
def test_compensation_can_fail(world, faults, ledger, setup, message):
    world.execute("create_ticket", {}, key(4), 1, 2.0)
    setup(faults)
    res = world.compensate("create_ticket", {}, key(4), 1, 2.0)
    assert res.status == "failed" and message in res.error
    assert ledger.compensations() == []


def test_compensation_is_fenced_too(world):
    world.execute("create_ticket", {}, key(4), 3, 2.0)
    res = world.compensate("create_ticket", {}, key(4), 2, 2.0)
    assert res.status == "failed" and "stale epoch" in res.error


# --------------------------------------------------------------------- probe


def test_probe_honours_observability(world, faults):
    world.execute("post_to_channel", {}, key(5), 1, 2.0)
    assert world.probe("post_to_channel", key(5), 1.0) == "done"
    assert world.probe("post_to_channel", key(9), 1.0) == "not_done"
    # The pager cannot be asked, whatever actually happened.
    world.execute("page_oncall", {"rota": "rota-Y"}, key(6), 1, 2.0)
    assert world.probe("page_oncall", key(6), 1.0) == "unknown"
    faults.down_services = {"channel"}
    assert world.probe("post_to_channel", key(5), 1.0) == "unknown"


def test_health_reports_each_service(world, faults):
    world.execute("create_ticket", {}, key(4), 4, 2.0)
    faults.down_services = {"pager"}
    h = world.health()
    assert h["world"] == "in-process"
    assert h["services"] == {"ticket": "up", "channel": "up", "pager": "down"}
    assert h["epoch_seen"] == {WF: 4}


# -------------------------------------------------------------- fault config


def test_fault_config_defaults_to_no_faults():
    assert FaultConfig().to_dict() == NO_FAULTS
    assert NO_FAULTS["late_delivery_delay_s"] == 8.0


def test_fault_config_update_converts_and_ignores_missing():
    f = FaultConfig()
    f.update({"empty_rotas": ["rota-X"], "latency_s": "0.5", "jitter_s": None})
    assert f.empty_rotas == {"rota-X"} and f.latency_s == 0.5 and f.jitter_s == 0.0
    assert f.to_dict()["empty_rotas"] == ["rota-X"]


def test_is_down_accepts_a_service_or_a_tool_name():
    f = FaultConfig()
    f.down_services = {"channel"}
    assert f.is_down("post_to_channel") and f.is_down("update_status_page")
    assert not f.is_down("create_ticket")
    f.down_services = {"create_ticket"}
    assert f.is_down("create_ticket") and not f.is_down("fetch_alerts")


@pytest.mark.parametrize("name", sorted(FAULT_PRESETS))
def test_every_preset_is_a_complete_payload(name):
    """Long-lived services keep their faults, so a preset must overwrite all of them."""
    assert set(FAULT_PRESETS[name]) == set(NO_FAULTS)


def test_presets_configure_what_their_scenario_needs():
    f = FaultConfig()
    f.update(FAULT_PRESETS["poison"])
    assert f.empty_rotas == {"rota-X"} and not f.timeout_tools
    f.update(FAULT_PRESETS["zombie"])
    assert f.empty_rotas == set()
    assert f.timeout_tools == f.late_delivery_tools == {"page_oncall"}
    f.update(FAULT_PRESETS["clear"])
    assert f.to_dict() == NO_FAULTS


# ------------------------------------------------------------- the ledger


def test_ledger_counts_net_and_gross_per_workflow():
    led = GroundTruthLedger()
    a, b = workflow_id_for("a-1"), workflow_id_for("a-2")
    led.record("create_ticket", effect_key(a, "br", 4), {})
    led.record("create_ticket", effect_key(a, "br2", 4), {})
    led.record("close_ticket", effect_key(a, "br", 4), {}, kind="compensation")
    led.record("page_oncall", effect_key(b, "br", 6), {"rota": "rota-Y"})
    assert led.counts(workflow_id=a) == {"create_ticket": 1}
    assert led.gross_counts(workflow_id=a) == {"create_ticket": 2}
    assert led.scoreboard(workflow_id=b) == (0, 0, 1)
    assert led.scoreboard() == (1, 0, 1)
    assert [e["tool"] for e in led.effects(tool_name="page_oncall")] == ["page_oncall"]
    assert len(led.compensations(workflow_id=a)) == 1 and led.compensations(b) == []
    led.reset()
    assert led.entries == []


def test_ledger_is_safe_under_concurrent_writers():
    led = GroundTruthLedger()

    def write(t: int) -> None:
        for i in range(200):
            led.record("create_ticket", effect_key(WF, f"br-{t}", i), {})

    threads = [threading.Thread(target=write, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert led.counts() == {"create_ticket": 800}
