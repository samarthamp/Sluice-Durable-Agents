"""The effect services and the ground-truth ledger service (3.1).

Do not add `from __future__ import annotations` here. Call is a local of
create_effect_service(), so PEP 563 leaves FastAPI unable to resolve it and every
/execute falls back to a query param and 422s.
"""

import os
import threading
import time

from ..core.tools import TOOLS_FOR_SERVICE
from ..core.types import workflow_from_key
from .faults import FaultConfig
from .inprocess import InProcessWorld
from .ledger import GroundTruthLedger


class LedgerClient:
    """Write side of the ground-truth ledger, over HTTP."""

    def __init__(self, url: str, timeout_s: float = 2.0):
        import httpx

        self.url = url.rstrip("/")
        self.client = httpx.Client(timeout=timeout_s)
        self.buffer: list[dict] = []
        self.lock = threading.Lock()

    def record(self, tool_name: str, key: str, args: dict, kind: str = "effect") -> None:
        entry = {
            "ts": time.time(),
            "tool": tool_name,
            "key": key,
            "workflow_id": workflow_from_key(key),
            "args": dict(args),
            "kind": kind,
        }
        with self.lock:
            self.buffer.append(entry)
            pending = list(self.buffer)
        try:
            resp = self.client.post(f"{self.url}/record", json={"entries": pending})
            resp.raise_for_status()
        except Exception:
            return
        with self.lock:
            del self.buffer[: len(pending)]

    def depth(self) -> int:
        with self.lock:
            return len(self.buffer)


def create_effect_service(service_name: str, ledger_url: str):
    """One effect service.  ``service_name`` is one of ticket / channel / pager."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    tools = set(TOOLS_FOR_SERVICE[service_name])
    faults = FaultConfig()
    ledger = LedgerClient(ledger_url)
    world = InProcessWorld(ledger, faults)
    epoch_seen: dict[str, int] = {}
    lock = threading.Lock()

    app = FastAPI(title=f"sluice-{service_name}")

    class Call(BaseModel):
        tool: str
        args: dict = {}
        key: str
        epoch: int = 1
        workflow_id: str | None = None
        timeout_s: float = 2.0

    def _fence(body: Call):
        """Each service records the highest epoch seen per workflow and rejects
        anything lower.  A deposed leader that wakes from a pause is refused here,
        rather than trusted to stand down on its own (3.4)."""
        wf = body.workflow_id or workflow_from_key(body.key)
        with lock:
            seen = epoch_seen.get(wf, 0)
            if body.epoch < seen:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": f"fenced: stale epoch {body.epoch} < {seen} for {wf}",
                        "epoch_seen": seen,
                    },
                )
            epoch_seen[wf] = max(seen, body.epoch)
        return None

    def _guard(body: Call):
        if body.tool not in tools:
            return JSONResponse(
                status_code=404,
                content={"error": f"{body.tool} is not served by {service_name}"},
            )
        return _fence(body)

    # The service must not enforce the caller's read timeout; 0.0 means no deadline.
    NO_SELF_DEADLINE = 0.0

    @app.post("/execute")
    def execute(body: Call):
        bad = _guard(body)
        if bad is not None:
            return bad
        res = world.execute(body.tool, body.args, body.key, body.epoch, NO_SELF_DEADLINE)
        return res.to_dict()

    @app.post("/compensate")
    def compensate(body: Call):
        bad = _guard(body)
        if bad is not None:
            return bad
        res = world.compensate(body.tool, body.args, body.key, body.epoch, NO_SELF_DEADLINE)
        return res.to_dict()

    @app.get("/probe")
    def probe(tool: str, key: str):
        if tool not in tools:
            return JSONResponse(
                status_code=404, content={"error": f"{tool} is not served by {service_name}"}
            )
        return {"status": world.probe(tool, key, 2.0)}

    @app.get("/health")
    def health():
        return {
            "service": service_name,
            # pid proves these are genuinely separate OS processes rather than threads.
            "pid": os.getpid(),
            "tools": sorted(tools),
            "faults": faults.to_dict(),
            "epoch_seen": dict(epoch_seen),
            "committed_keys": len(world.applied),
            "ledger_buffer": ledger.depth(),
        }

    @app.get("/admin/faults")
    def get_faults():
        return faults.to_dict()

    @app.post("/admin/faults")
    def set_faults(body: dict):
        faults.update(body)
        return faults.to_dict()

    @app.post("/admin/flush_late")
    def flush_late():
        return {"fired": world.flush_late_deliveries()}

    @app.post("/admin/reset")
    def reset():
        world.applied.clear()
        world.epoch_seen.clear()
        with lock:
            epoch_seen.clear()
        return {"reset": service_name}

    return app


def create_ledger_service():
    """The oracle.  Write-only from the services, read-only to the checker."""
    from fastapi import FastAPI

    ledger = GroundTruthLedger()
    app = FastAPI(title="sluice-ledger")

    @app.post("/record")
    def record(body: dict):
        entries = body.get("entries") or [body]
        with ledger.lock:
            known = {(e["ts"], e["key"], e["kind"]) for e in ledger.entries}
            for e in entries:
                ident = (e["ts"], e["key"], e["kind"])
                if ident in known:
                    continue
                known.add(ident)
                ledger.entries.append(
                    {
                        "ts": e["ts"],
                        "tool": e["tool"],
                        "key": e["key"],
                        "workflow_id": e.get("workflow_id") or workflow_from_key(e["key"]),
                        "args": e.get("args") or {},
                        "kind": e.get("kind", "effect"),
                    }
                )
        return {"total": len(ledger.entries)}

    @app.get("/effects")
    def effects(tool: str | None = None, workflow_id: str | None = None):
        return ledger.effects(tool_name=tool, workflow_id=workflow_id)

    @app.get("/compensations")
    def compensations(workflow_id: str | None = None):
        return ledger.compensations(workflow_id=workflow_id)

    @app.get("/counts")
    def counts(net: str = "true", workflow_id: str | None = None):
        return ledger.counts(net=net.lower() == "true", workflow_id=workflow_id)

    @app.get("/health")
    def health():
        return {"service": "ledger", "pid": os.getpid(), "entries": len(ledger.entries)}

    @app.post("/reset")
    def reset():
        ledger.reset()
        return {"reset": "ledger"}

    return app


def build_app(name: str, ledger_url: str = "http://127.0.0.1:8100"):
    return create_ledger_service() if name == "ledger" else create_effect_service(name, ledger_url)
