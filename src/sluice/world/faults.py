"""Fault injection: latency, jitter and failure modes, as middleware on every effect (3.3)."""

from __future__ import annotations

from ..core.tools import SERVICE_FOR_TOOL


class FaultConfig:
    """Latency, jitter and fault mode, as middleware on every effect (3.3)."""

    def __init__(self):
        self.latency_s = 0.0
        self.jitter_s = 0.0
        self.fail_tools: set[str] = set()
        self.timeout_tools: set[str] = set()
        self.late_delivery_tools: set[str] = set()
        # 8 seconds on stage, 40 in the sweep (3.3).
        self.late_delivery_delay_s = 8.0
        # Takes a service name ("ticket") or a tool name ("create_ticket").
        self.down_services: set[str] = set()
        self.fail_compensation_tools: set[str] = set()
        self.empty_rotas: set[str] = set()

    def is_down(self, tool_name: str) -> bool:
        return (
            tool_name in self.down_services
            or SERVICE_FOR_TOOL.get(tool_name, "") in self.down_services
        )

    def to_dict(self) -> dict:
        return {
            "latency_s": self.latency_s,
            "jitter_s": self.jitter_s,
            "fail_tools": sorted(self.fail_tools),
            "timeout_tools": sorted(self.timeout_tools),
            "late_delivery_tools": sorted(self.late_delivery_tools),
            "late_delivery_delay_s": self.late_delivery_delay_s,
            "down_services": sorted(self.down_services),
            "fail_compensation_tools": sorted(self.fail_compensation_tools),
            "empty_rotas": sorted(self.empty_rotas),
        }

    def update(self, d: dict) -> None:
        for name in (
            "fail_tools",
            "timeout_tools",
            "late_delivery_tools",
            "down_services",
            "fail_compensation_tools",
            "empty_rotas",
        ):
            if name in d and d[name] is not None:
                setattr(self, name, set(d[name]))
        for name in ("latency_s", "jitter_s", "late_delivery_delay_s"):
            if d.get(name) is not None:
                setattr(self, name, float(d[name]))


# Every fault switched off, as a full payload. Long-lived services must be sent the
# whole set, so that a previous run's faults are cleared rather than left to bleed
# into the next one. Treat these dicts as read-only.
NO_FAULTS: dict = FaultConfig().to_dict()

# Named fault configurations for the long-lived HTTP services.
FAULT_PRESETS: dict[str, dict] = {
    "clear": dict(NO_FAULTS),
    # The poison step: rota-X has nobody on call, so the P2 page fails.
    "poison": dict(NO_FAULTS, empty_rotas=["rota-X"]),
    # The zombie page: the pager times out, then the page lands late anyway.
    "zombie": dict(
        NO_FAULTS,
        timeout_tools=["page_oncall"],
        late_delivery_tools=["page_oncall"],
        late_delivery_delay_s=8.0,
    ),
}
