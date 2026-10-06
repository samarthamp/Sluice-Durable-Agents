"""Sluice: divergence-safe durable execution for agent decisioning.

Layers, bottom to top (each imports only from the layers below it):

    core           interfaces, journal, workflow definition, orchestrator (stdlib only)
    world          effect layer: in-process world, fault injection, ledger, HTTP side
    ingest         alert sources: Redis Streams with an in-process fallback
    observability  journal -> dashboard state, incident post-mortem
    verification   EEO checker, crash sweep, overhead benchmark
    scenarios      the runnable scenarios and the failover demo
    dashboard      the read-only web dashboard
    cli            the ``sluice`` command
"""

from .core.engine import (
    BARRIER_DEADLINE_S,
    MAX_COMP_ATTEMPTS,
    MAX_FORK_DEPTH,
    CrashPolicy,
    Escalation,
    Orchestrator,
    ProcessCrash,
    recover,
)
from .core.journal import Journal, LeaseUnavailable
from .core.tools import (
    COMPENSATIONS,
    DEMO_ALERT,
    EFFECT_TYPES,
    ROTA_FOR_SEVERITY,
    STEP_LABELS,
    trace_for,
)
from .core.types import (
    Alert,
    Branch,
    EffectType,
    JournalRecord,
    ToolResult,
    effect_key,
    workflow_from_key,
    workflow_id_for,
)
from .ingest import InProcessAlertSource, RedisAlertSource, alert_source, drain
from .observability.audit import post_mortem, write_post_mortem
from .observability.view import derive_state, render_terminal
from .scenarios import ALERT, SCENARIOS, Ctx, make_ctx, run_scenario
from .verification.bench import benchmark, format_benchmark
from .verification.checker import (
    check_eeo,
    escalation_reasons,
    evidence_table,
    explain,
    format_escalations,
    outcome_counts,
)
from .world import FAULT_PRESETS, FaultConfig, GroundTruthLedger, InProcessWorld

__version__ = "1.0.0"

__all__ = [
    "ALERT",
    "BARRIER_DEADLINE_S",
    "COMPENSATIONS",
    "DEMO_ALERT",
    "EFFECT_TYPES",
    "FAULT_PRESETS",
    "MAX_COMP_ATTEMPTS",
    "MAX_FORK_DEPTH",
    "ROTA_FOR_SEVERITY",
    "SCENARIOS",
    "STEP_LABELS",
    "Alert",
    "Branch",
    "CrashPolicy",
    "Ctx",
    "EffectType",
    "Escalation",
    "FaultConfig",
    "GroundTruthLedger",
    "InProcessAlertSource",
    "InProcessWorld",
    "Journal",
    "JournalRecord",
    "LeaseUnavailable",
    "Orchestrator",
    "ProcessCrash",
    "RedisAlertSource",
    "ToolResult",
    "alert_source",
    "benchmark",
    "check_eeo",
    "derive_state",
    "drain",
    "effect_key",
    "escalation_reasons",
    "evidence_table",
    "explain",
    "format_benchmark",
    "format_escalations",
    "make_ctx",
    "outcome_counts",
    "post_mortem",
    "recover",
    "render_terminal",
    "run_scenario",
    "trace_for",
    "workflow_from_key",
    "workflow_id_for",
    "write_post_mortem",
]
