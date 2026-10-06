"""Runnable scenarios: the poison step, its harder variants, and the failover demo (5.5).

Each scenario runs one recovery strategy (``pinned``, ``naive`` or ``sluice``) against
a fresh journal and effect layer, then grades the run against the ground-truth ledger.
"""

from .catalog import (
    ALERT,
    MODES,
    SCENARIOS,
    Ctx,
    make_ctx,
    run_scenario,
    scenario_compfail,
    scenario_compretry,
    scenario_crash,
    scenario_poison,
    scenario_redelivery,
    scenario_residue,
    scenario_zombie,
)
from .failover import format_failover, run_failover

__all__ = [
    "ALERT",
    "MODES",
    "SCENARIOS",
    "Ctx",
    "format_failover",
    "make_ctx",
    "run_failover",
    "run_scenario",
    "scenario_compfail",
    "scenario_compretry",
    "scenario_crash",
    "scenario_poison",
    "scenario_redelivery",
    "scenario_residue",
    "scenario_zombie",
]
