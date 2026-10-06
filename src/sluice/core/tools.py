"""The eight tools of 1.4, their effect types, and the scripted agent trace."""

from __future__ import annotations

from .types import Alert, EffectType

# The alert the poison step is built around: payments-api, scripted to classify as P2,
# which routes to a rota with nobody on call.
DEMO_ALERT = Alert(
    "a-1001",
    "payments-api",
    "prometheus",
    {"msg": "error rate spike on checkout", "value": 0.42},
)

PURE =EffectType("pure", "observable")
IDEMPOTENT = EffectType("idempotent", "observable")
COMPENSATABLE = EffectType("compensatable", "observable")
COMPENSATABLE_VISIBLE = EffectType("compensatable", "observable", True)
IRREVERSIBLE = EffectType("irreversible", "unobservable")

EFFECT_TYPES: dict[str, EffectType] = {
    "fetch_alerts": PURE,
    "fetch_service_context": PURE,
    "classify": PURE,
    "write_dedupe_marker": IDEMPOTENT,
    "create_ticket": COMPENSATABLE,
    "post_to_channel": COMPENSATABLE_VISIBLE,
    "page_oncall": IRREVERSIBLE,
    "update_status_page": COMPENSATABLE_VISIBLE,
}

COMPENSATIONS: dict[str, str] = {
    "create_ticket": "close_ticket",
    "post_to_channel": "delete_channel_post",
    "update_status_page": "revert_status_page",
}

# Which service owns which tool.  Used by HttpWorld routing and by the topology strip.
SERVICE_FOR_TOOL: dict[str, str] = {
    "fetch_alerts": "ticket",
    "fetch_service_context": "ticket",
    "classify": "ticket",
    "write_dedupe_marker": "ticket",
    "create_ticket": "ticket",
    "post_to_channel": "channel",
    "update_status_page": "channel",
    "page_oncall": "pager",
}

TOOLS_FOR_SERVICE: dict[str, list[str]] = {}
for _tool, _svc in SERVICE_FOR_TOOL.items():
    TOOLS_FOR_SERVICE.setdefault(_svc, []).append(_tool)

ROTA_FOR_SEVERITY = {"P1": "rota-Y", "P2": "rota-X", "P3": "rota-Z"}

# Ranked alternatives for the divergence policy (2.5): highest-ranked unattempted.
ALTERNATIVES = ["P1", "P2", "P3"]

STEP_LABELS = [
    "fetch_alerts",
    "fetch_service_context",
    "classify",
    "write_dedupe_marker",
    "create_ticket",
    "post_to_channel",
    "page_oncall",
    "update_status_page",
]

PAGE_SEQ = STEP_LABELS.index("page_oncall")
CLASSIFY_SEQ = STEP_LABELS.index("classify")


def trace_for(alert, severity: str, supersede: dict | None = None) -> list[dict]:
    """Scripted decision trace (3.5)."""
    incident_id = f"inc-{alert.alert_id}"
    rota = ROTA_FOR_SEVERITY[severity]

    page_args: dict = {"rota": rota, "severity": severity, "incident": incident_id}
    if supersede:
        page_args["supersedes"] = supersede

    return [
        {"tool": "fetch_alerts", "args": {"source": alert.source}},
        {"tool": "fetch_service_context", "args": {"service": alert.service}},
        {
            "tool": "classify",
            "args": {"alert_id": alert.alert_id, "severity": severity},
            "decision": True,
            "alternatives": list(ALTERNATIVES),
        },
        {"tool": "write_dedupe_marker", "args": {"incident_id": incident_id}},
        {"tool": "create_ticket", "args": {"service": alert.service, "severity": severity}},
        {"tool": "post_to_channel", "args": {"incident": incident_id, "severity": severity}},
        {"tool": "page_oncall", "args": page_args},
        {
            "tool": "update_status_page",
            "args": {"incident": incident_id, "state": "investigating"},
        },
    ]


def supersede_annotation(residue_record) -> dict:
    """Render the supersede pointer carried by a page that follows residue (2.4)."""
    args = residue_record.args or {}
    return {
        "record_id": residue_record.record_id,
        "rota": args.get("rota"),
        "severity": args.get("severity"),
        "incident": args.get("incident"),
        "note": (
            f"supersedes page to {args.get('rota')} for incident "
            f"{args.get('incident')}, issued under classification "
            f"{args.get('severity')}"
        ),
    }
