"""The ground-truth ledger: what actually happened to the world (2.8).

The ledger sits outside the system and is written only by the effect layer. The EEO
checker reads it; the orchestrator never does. That separation is the point: nothing
grades itself.
"""

from __future__ import annotations

import threading
import time

from ..core.types import workflow_from_key


class GroundTruthLedger:
    def __init__(self):
        self.entries: list[dict] = []
        self.lock = threading.Lock()

    def record(self, tool_name: str, key: str, args: dict, kind: str = "effect") -> None:
        with self.lock:
            self.entries.append(
                {
                    "ts": time.time(),
                    "tool": tool_name,
                    "key": key,
                    "workflow_id": workflow_from_key(key),
                    "args": dict(args),
                    "kind": kind,
                }
            )

    def _rows(self, workflow_id: str | None) -> list[dict]:
        with self.lock:
            rows = list(self.entries)
        if workflow_id is None:
            return rows
        return [e for e in rows if e["workflow_id"] == workflow_id]

    def effects(self, tool_name: str | None = None, workflow_id: str | None = None) -> list[dict]:
        return [
            e
            for e in self._rows(workflow_id)
            if e["kind"] == "effect" and (tool_name is None or e["tool"] == tool_name)
        ]

    def compensations(self, workflow_id: str | None = None) -> list[dict]:
        return [e for e in self._rows(workflow_id) if e["kind"] == "compensation"]

    def counts(self, net: bool = True, workflow_id: str | None = None) -> dict:
        """Net counts subtract compensated effects."""
        rows = self._rows(workflow_id)
        comped = {e["key"] for e in rows if e["kind"] == "compensation"}
        out: dict[str, int] = {}
        for e in rows:
            if e["kind"] != "effect":
                continue
            if net and e["key"] in comped:
                continue
            out[e["tool"]] = out.get(e["tool"], 0) + 1
        return out

    def gross_counts(self, workflow_id: str | None = None) -> dict:
        return self.counts(net=False, workflow_id=workflow_id)

    def scoreboard(self, workflow_id: str | None = None) -> tuple[int, int, int]:
        c = self.counts(workflow_id=workflow_id)
        return (
            c.get("create_ticket", 0),
            c.get("post_to_channel", 0),
            c.get("page_oncall", 0),
        )

    def reset(self) -> None:
        with self.lock:
            self.entries.clear()
