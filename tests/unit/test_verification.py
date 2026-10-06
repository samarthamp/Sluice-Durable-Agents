"""The crash sweep (2.8) and the overhead benchmark, at test-sized scale.

The full sweep (165 runs) is ``sluice demo --sweep``; here a slice of it runs on
every test pass, so a change that breaks recovery at some boundary fails fast.
"""

from __future__ import annotations

import os

import pytest

from sluice.verification.bench import _percentile, benchmark, format_benchmark
from sluice.verification.sweep import (
    FAULT_MODES,
    by_fault_mode,
    failures,
    format_by_fault_mode,
    format_failures,
    sweep,
)

# ------------------------------------------------------------------- sweep


def test_fault_modes_cover_the_plan_plus_a_healing_partition():
    assert set(FAULT_MODES) == {"crash", "timeout", "partition", "partition-transient",
                                "late-delivery"}


@pytest.fixture(scope="module")
def small_sweep(tmp_path_factory):
    """Every fault mode, at the two boundaries around the irreversible step."""
    return sweep(steps=[5, 6], phases=("after_intent", "after_effect"),
                 barrier_deadline_s=0.2, workdir=str(tmp_path_factory.mktemp("sweep")))


def test_a_sweep_slice_has_no_unexplained_violations(small_sweep):
    # 1 clean run + 2 steps x 2 phases, for each of the 5 fault modes.
    assert len(small_sweep["results"]) == 5 * 5
    assert small_sweep["crash_points"] == 5
    assert failures(small_sweep) == []
    assert format_failures(small_sweep) == "no unexplained violations"
    assert "unexplained violations:    0" in small_sweep["table"]


def test_each_fault_mode_ends_the_way_the_design_says(small_sweep):
    modes = by_fault_mode(small_sweep)
    # Crash recovery is what durable execution is for: it never needs a human.
    assert modes["crash"]["escalation_rate"] == 0.0
    assert modes["partition-transient"]["escalation_rate"] == 0.0
    # Permanent outages and unknowable pages escalate by construction.
    for mode in ("timeout", "partition", "late-delivery"):
        assert modes[mode]["escalation_rate"] == 100.0
    assert modes["timeout"]["ba"] == modes["timeout"]["runs"]
    assert all(m["unexplained"] == 0 for m in modes.values())


def test_fault_mode_table_columns_line_up(small_sweep):
    lines = format_by_fault_mode(small_sweep).splitlines()[1:]
    run_columns = {line.index(line.split()[1]) + len(line.split()[1]) for line in lines}
    assert len(run_columns) == 1, lines


def test_a_caller_owned_workdir_is_kept(tmp_path):
    report = sweep(steps=[], fault_modes=["crash"], workdir=str(tmp_path))
    assert len(report["results"]) == 1
    assert os.listdir(tmp_path)  # the journal is still there to inspect


def test_failures_are_reported_with_their_crash_point():
    report = {"results": [
        {"fault_mode": "partition-transient", "crash_point": "seq4:after_effect",
         "result_outcome": "completed",
         "unexplained": [{"clause": "no_loss", "message": "lost"}]},
        {"fault_mode": "crash", "crash_point": "none", "unexplained": []},
    ]}
    text = format_failures(report)
    assert text.splitlines()[0] == "1 run(s) with unexplained violations:"
    assert "seq4:after_effect" in text and "no_loss: lost" in text


# ---------------------------------------------------------------- benchmark


@pytest.mark.parametrize(
    "values, p, expected",
    [([], 50, 0.0), ([5.0], 99, 5.0), ([1.0, 2.0, 3.0], 50, 2.0), ([1.0, 2.0, 3.0], 100, 3.0)],
)
def test_percentile(values, p, expected):
    assert _percentile(values, p) == expected


def test_benchmark_reports_every_configuration(tmp_path):
    r = benchmark(n=4, warmup=1, concurrency=2, effect_latency_ms=1.0, workdir=str(tmp_path))
    assert set(r["rows"]) == {"bare", "memory", "normal", "full"}
    for row in r["rows"].values():
        assert row["n"] == 4 and row["mean_ms"] > 0
    assert r["rows"]["full"]["records_per_workflow"] == 16  # INTENT + RESULT x 8 steps
    assert set(r["latency_rows"]) == {"bare_lat", "full_lat"}
    # Regression: every worker thread must finish and contribute its samples.
    assert r["concurrent"]["threads"] == 2
    assert r["concurrent"]["n"] == 2 * 10
    text = format_benchmark(r)
    for label in ("bare tool calls", "+ journal (:memory:)", "synchronous=NORMAL",
                  "synchronous=FULL", "AGAINST A REALISTIC EFFECT LAYER",
                  "NOT a distributed scale run"):
        assert label in text


def test_benchmark_without_latency_or_concurrency_rows(tmp_path):
    r = benchmark(n=2, warmup=0, concurrency=0, effect_latency_ms=0, workdir=str(tmp_path))
    assert r["latency_rows"] is None and r["concurrent"] is None
    assert "AGAINST A REALISTIC" not in format_benchmark(r)
