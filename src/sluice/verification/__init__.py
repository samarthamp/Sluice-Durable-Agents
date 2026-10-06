"""Correctness evidence: the EEO checker, the crash sweep and the overhead benchmark.

The checker grades a workflow against the ground-truth ledger, never against what the
orchestrator believed (2.8).
"""

from .bench import benchmark, format_benchmark
from .checker import (
    check_eeo,
    escalation_reasons,
    evidence_table,
    explain,
    format_escalations,
    outcome_counts,
    read_verdict,
    write_verdict,
)
from .sweep import FAULT_MODES, sweep

__all__ = [
    "FAULT_MODES",
    "benchmark",
    "check_eeo",
    "escalation_reasons",
    "evidence_table",
    "explain",
    "format_benchmark",
    "format_escalations",
    "outcome_counts",
    "read_verdict",
    "sweep",
    "write_verdict",
]
