"""InProcessWorld: direct calls to in-memory effect stubs plus the ground-truth ledger (3.6)."""

from __future__ import annotations

import random
import threading
import time

from ..core.tools import COMPENSATIONS, EFFECT_TYPES, TOOLS_FOR_SERVICE
from ..core.types import ProbeStatus, ToolResult, workflow_from_key
from .faults import FaultConfig
from .ledger import GroundTruthLedger


class InProcessWorld:
    """Direct calls to in-memory effect stubs plus the ground-truth ledger (3.6)."""

    name = "in-process"

    def __init__(self, ledger: GroundTruthLedger, faults: FaultConfig | None = None):
        self.ledger = ledger
        self.faults = faults or FaultConfig()
        # Effect state keyed by idempotency key.
        self.applied: dict[str, dict] = {}
        # Highest epoch seen PER WORKFLOW (3.4), not per key.
        self.epoch_seen: dict[str, int] = {}
        self.pending_late: list[threading.Timer] = []
        self.lock = threading.Lock()

    # ------------------------------------------------------------- middleware

    def _fence(self, key: str, epoch: int) -> bool:
        wf = workflow_from_key(key)
        with self.lock:
            seen = self.epoch_seen.get(wf, 0)
            if epoch < seen:
                return False
            self.epoch_seen[wf] = max(seen, epoch)
        return True

    def _delay(self):
        d = self._delay_seconds()
        if d:
            time.sleep(d)

    def _delay_seconds(self) -> float:
        d = self.faults.latency_s
        if self.faults.jitter_s:
            d += random.uniform(0, self.faults.jitter_s)
        return d

    # ---------------------------------------------------------------- effects

    def execute(
        self, tool_name: str, args: dict, key: str, epoch: int, timeout_s: float
    ) -> ToolResult:
        if not self._fence(key, epoch):
            return ToolResult(
                "failed",
                error=f"stale epoch {epoch} < {self.epoch_seen.get(workflow_from_key(key))}",
            )

        # Idempotency key check comes first, before faults and before latency.
        with self.lock:
            existing = self.applied.get(key)
        if existing is not None:
            return ToolResult("ok", dict(existing), None)

        delay = self._delay_seconds()

        # A refused connection comes back fast; it does not make you wait out the latency.
        if self.faults.is_down(tool_name):
            return ToolResult("failed", error=f"{tool_name} unavailable")

        if tool_name in self.faults.fail_tools:
            time.sleep(delay)
            return ToolResult("failed", error=f"{tool_name} rejected the request")

        if tool_name == "page_oncall" and args.get("rota") in self.faults.empty_rotas:
            time.sleep(delay)
            return ToolResult("failed", error=f"no responder on {args['rota']}")

        # Two routes to unknown: an injected timeout, or latency the caller won't wait out.
        timed_out = tool_name in self.faults.timeout_tools or (
            timeout_s > 0 and delay >= timeout_s
        )
        if timed_out:
            # Unknown, not failed: a timeout misread as a definite failure is exactly the
            # bug that produces a double page (4.1).
            time.sleep(min(delay, timeout_s) if timeout_s > 0 else delay)
            if tool_name in self.faults.late_delivery_tools:
                self._schedule_late(tool_name, args, key)
            return ToolResult("unknown", error=f"timeout after {timeout_s}s")

        time.sleep(delay)
        value = {"tool": tool_name, "args": dict(args), "key": key}
        with self.lock:
            self.applied[key] = value
        if EFFECT_TYPES[tool_name].reversibility != "pure":
            self.ledger.record(tool_name, key, args)
        return ToolResult("ok", dict(value), None)

    def _schedule_late(self, tool_name: str, args: dict, key: str) -> None:
        """The request timed out but lands anyway, later (3.3)."""

        def land():
            with self.lock:
                if key in self.applied:
                    return
                self.applied[key] = {
                    "tool": tool_name,
                    "args": dict(args),
                    "key": key,
                    "late": True,
                }
            self.ledger.record(tool_name, key, args)

        t = threading.Timer(self.faults.late_delivery_delay_s, land)
        t.daemon = True
        t.start()
        self.pending_late.append(t)

    def flush_late_deliveries(self) -> int:
        """Fire every pending late delivery now."""
        fired = 0
        for t in list(self.pending_late):
            if t.is_alive():
                t.cancel()
                t.function()
                fired += 1
        self.pending_late.clear()
        return fired

    def compensate(
        self, tool_name: str, args: dict, key: str, epoch: int, timeout_s: float
    ) -> ToolResult:
        comp = COMPENSATIONS.get(tool_name)
        if comp is None:
            return ToolResult("failed", error=f"{tool_name} has no inverse")
        if not self._fence(key, epoch):
            return ToolResult("failed", error=f"stale epoch {epoch}")
        self._delay()
        if self.faults.is_down(tool_name):
            return ToolResult("failed", error=f"{tool_name} unavailable for compensation")
        if tool_name in self.faults.fail_compensation_tools:
            return ToolResult("failed", error=f"{comp} rejected by {tool_name} service")
        with self.lock:
            self.applied.pop(key, None)
        self.ledger.record(comp, key, args, kind="compensation")
        return ToolResult("ok", {"compensated": key, "via": comp}, None)

    def probe(self, tool_name: str, key: str, timeout_s: float) -> ProbeStatus:
        """Probing replaces the commit ack for participants that can be queried."""
        et = EFFECT_TYPES[tool_name]
        if et.observability == "unobservable":
            return "unknown"
        if self.faults.is_down(tool_name):
            return "unknown"
        with self.lock:
            return "done" if key in self.applied else "not_done"

    # ------------------------------------------------------------ topology aid

    def health(self) -> dict:
        """Feeds the topology strip (6.4)."""
        services = {}
        for svc, tools in TOOLS_FOR_SERVICE.items():
            down = svc in self.faults.down_services or any(
                t in self.faults.down_services for t in tools
            )
            services[svc] = "down" if down else "up"
        return {
            "world": self.name,
            "services": services,
            "epoch_seen": dict(self.epoch_seen),
        }
