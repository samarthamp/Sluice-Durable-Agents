"""Part 4: frozen interfaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Reversibility = Literal["pure", "idempotent", "compensatable", "irreversible"]
Observability = Literal["observable", "unobservable"]
ResultStatus = Literal["ok", "failed", "unknown"]
ProbeStatus = Literal["done", "not_done", "unknown"]
BranchStatus = Literal["active", "abandoned", "compensated", "abandoned_with_residue"]

RecordKind = Literal[
    "INTENT",
    "RESULT",
    "COMP_INTENT",
    "COMP_RESULT",
    "BRANCH_FORKED",
    "BRANCH_ABANDONED",
    "BRANCH_COMPENSATED",
    "BARRIER_BLOCKED",
    "BARRIER_RELEASED",
    "ESCALATED",
    "LATE_DELIVERY_SUPPRESSED",
]

DEAD_BRANCH_STATUSES = ("abandoned", "abandoned_with_residue")


@dataclass(frozen=True)
class EffectType:
    reversibility: Reversibility
    observability: Observability
    externally_visible: bool = False

    def to_dict(self) -> dict:
        return {
            "reversibility": self.reversibility,
            "observability": self.observability,
            "externally_visible": self.externally_visible,
        }

    @staticmethod
    def from_dict(d: dict | None) -> "EffectType | None":
        if d is None:
            return None
        return EffectType(
            d["reversibility"], d["observability"], d.get("externally_visible", False)
        )


@dataclass(frozen=True)
class ToolResult:
    status: ResultStatus
    value: Any = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "value": self.value, "error": self.error}

    @staticmethod
    def from_dict(d: dict | None) -> "ToolResult | None":
        if d is None:
            return None
        return ToolResult(d["status"], d.get("value"), d.get("error"))


@dataclass(frozen=True)
class JournalRecord:
    record_id: int
    ts: float
    workflow_id: str
    branch_id: str
    seq: int
    kind: RecordKind
    tool_name: str | None = None
    effect_type: EffectType | None = None
    args: dict | None = None
    key: str | None = None
    epoch: int | None = None
    result: ToolResult | None = None
    detail: dict | None = None

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "ts": self.ts,
            "workflow_id": self.workflow_id,
            "branch_id": self.branch_id,
            "seq": self.seq,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "effect_type": self.effect_type.to_dict() if self.effect_type else None,
            "args": self.args,
            "key": self.key,
            "epoch": self.epoch,
            "result": self.result.to_dict() if self.result else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Branch:
    branch_id: str
    parent_branch_id: str | None
    fork_point_record_id: int | None
    depth: int
    status: BranchStatus

    def to_dict(self) -> dict:
        return {
            "branch_id": self.branch_id,
            "parent_branch_id": self.parent_branch_id,
            "fork_point_record_id": self.fork_point_record_id,
            "depth": self.depth,
            "status": self.status,
        }


@dataclass(frozen=True)
class Alert:
    alert_id: str
    service: str
    source: str
    payload: dict

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "service": self.service,
            "source": self.source,
            "payload": self.payload,
        }

    @staticmethod
    def from_dict(d: dict) -> "Alert":
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return Alert(d["alert_id"], d["service"], d["source"], payload)


@dataclass(frozen=True)
class LeaseInfo:
    scope: str
    owner: str
    epoch: int
    expires: float


class Tool(Protocol):
    name: str
    effect_type: EffectType

    def execute(self, args: dict, key: str, epoch: int, timeout_s: float) -> ToolResult: ...
    def compensate(
        self, args: dict, result: ToolResult, key: str, epoch: int, timeout_s: float
    ) -> ToolResult: ...
    def probe(self, key: str, timeout_s: float) -> ProbeStatus: ...


class World(Protocol):
    def execute(
        self, tool_name: str, args: dict, key: str, epoch: int, timeout_s: float
    ) -> ToolResult: ...
    def compensate(
        self, tool_name: str, args: dict, key: str, epoch: int, timeout_s: float
    ) -> ToolResult: ...
    def probe(self, tool_name: str, key: str, timeout_s: float) -> ProbeStatus: ...


class AlertSource(Protocol):
    def consume(self) -> Iterator[Alert]: ...
    def ack(self, alert_id: str) -> None: ...


def effect_key(workflow_id: str, branch_id: str, seq: int) -> str:
    """key = hash(workflow_id, branch_id, seq), stable across epochs."""
    raw = f"{workflow_id}|{branch_id}|{seq}".encode()
    return f"{workflow_id}:{hashlib.sha256(raw).hexdigest()[:16]}"


def workflow_from_key(key: str | None) -> str:
    if not key or ":" not in key:
        return ""
    return key.split(":", 1)[0]


def workflow_id_for(alert_id: str) -> str:
    """Deterministic derivation, so at-least-once redelivery of the same alert
    lands on the same workflow and is absorbed by journal lookup (1.4, 3.2)."""
    return "wf-" + hashlib.sha256(alert_id.encode()).hexdigest()[:16]
