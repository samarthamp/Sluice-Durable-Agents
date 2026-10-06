"""Every scenario under every recovery strategy: 7 x 3 = 21 panes.

The expected table is the one in docs/design.md, section 5, as recorded from the
pre-refactor baseline. Each pane is also cross-checked: the scoreboard the journal
derives (what the system believes) must equal the ground-truth ledger's net counts
(what actually happened).
"""

from __future__ import annotations

import pytest

from sluice.scenarios import SCENARIOS

# scenario -> mode -> (outcome, (tickets, posts, pages), EEO pass)
EXPECTED = {
    "poison": {
        "pinned": ("livelocked", (1, 1, 0), False),
        "naive": ("completed", (2, 2, 1), False),
        "sluice": ("completed", (1, 1, 1), True),
    },
    "residue": {
        "pinned": ("completed", (1, 1, 1), True),     # passes EEO, pages the wrong rota
        "naive": ("completed", (2, 2, 2), False),     # the unexplained double buzz
        "sluice": ("completed", (1, 1, 2), True),  # second page supersedes the first
    },
    "compfail": {
        "pinned": ("livelocked", (1, 1, 0), False),
        "naive": ("completed", (2, 2, 1), False),
        "sluice": ("escalated", (2, 1, 0), True),  # refused to act blind, said so
    },
    "compretry": {
        "pinned": ("livelocked", (1, 1, 0), False),
        "naive": ("completed", (2, 2, 1), False),
        "sluice": ("completed", (1, 1, 1), True),
    },
    "zombie": {
        "pinned": ("escalated", (1, 1, 1), True),
        "naive": ("escalated", (1, 1, 1), True),
        "sluice": ("escalated", (1, 1, 1), True),
    },
    "crash": {
        "pinned": ("livelocked", (1, 1, 0), False),
        "naive": ("completed", (2, 2, 1), False),
        "sluice": ("completed", (1, 1, 1), True),
    },
    "redelivery": {
        "pinned": ("completed", (1, 1, 1), True),
        "naive": ("completed", (2, 2, 2), False),
        "sluice": ("completed", (1, 1, 1), True),
    },
}

# Where the world and the journal legitimately disagree. Naive re-run does not replay:
# after a crash between the effect and its RESULT record it abandons that branch and
# starts over, so the world holds a ticket the journal never learned about. (A known
# limitation of EEO clause 3: see docs/architecture.md.)
LEDGER_DIFFERS = {("crash", "naive"): (3, 2, 1)}

# What `sluice demo --scenario all --flush-late` passes to every pane.
DEMO_OPTS = {"step_pause_s": 0.0, "late_delivery_delay_s": 8.0, "flush": True,
             "crash_seq": 4, "crash_phase": "after_effect"}

CASES = [(s, m) for s in EXPECTED for m in ("pinned", "naive", "sluice")]


def test_the_matrix_covers_every_registered_scenario():
    assert set(EXPECTED) == set(SCENARIOS)


@pytest.mark.parametrize("scenario, mode", CASES, ids=[f"{s}-{m}" for s, m in CASES])
def test_scenario_outcome(ctx, scenario, mode):
    fn, _description = SCENARIOS[scenario]
    res = fn(ctx, mode, dict(DEMO_OPTS))
    outcome, counts, passes = EXPECTED[scenario][mode]

    board = res["state"]["scoreboard"]
    assert res["result"]["outcome"] == outcome
    assert (board["tickets"], board["posts"], board["pages"]) == counts
    assert res["verdict"]["pass"] is passes, res["verdict"]["unexplained"]
    # What the journal believes is what the world shows -- except where noted.
    world = LEDGER_DIFFERS.get((scenario, mode), counts)
    assert ctx.ledger.scoreboard(workflow_id=res["workflow_id"]) == world
    # Every verdict is also left beside the journal for the read-only dashboard.
    assert res["verdict"]["workflow_id"] == res["workflow_id"]


def test_sluice_never_loses_or_duplicates_where_the_baselines_do(ctx, tmp_path):
    """The headline: across every scenario, only sluice passes everywhere."""
    from sluice.scenarios import make_ctx

    failing = {"pinned": [], "naive": [], "sluice": []}
    for scenario, mode in CASES:
        c = make_ctx(str(tmp_path / f"{scenario}-{mode}.db"))
        try:
            fn, _ = SCENARIOS[scenario]
            if not fn(c, mode, dict(DEMO_OPTS))["verdict"]["pass"]:
                failing[mode].append(scenario)
        finally:
            c.close()
    assert failing["sluice"] == []
    assert sorted(failing["pinned"]) == ["compfail", "compretry", "crash", "poison"]
    assert sorted(failing["naive"]) == ["compfail", "compretry", "crash", "poison",
                                        "redelivery", "residue"]
