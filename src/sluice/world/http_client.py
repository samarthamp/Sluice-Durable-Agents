"""HttpWorld: the same World Protocol over real HTTP (3.6)."""

from __future__ import annotations

import os

from ..core.tools import SERVICE_FOR_TOOL
from ..core.types import ProbeStatus, ToolResult, workflow_from_key

DEFAULT_PORTS = {"ledger": 8100, "ticket": 8101, "channel": 8102, "pager": 8103}


def default_urls(host: str = "127.0.0.1") -> dict[str, str]:
    urls = {}
    for name, port in DEFAULT_PORTS.items():
        env = os.environ.get(f"SLUICE_{name.upper()}_URL")
        urls[name] = env or f"http://{host}:{port}"
    return urls


class HttpWorld:
    name = "http"

    def __init__(self, urls: dict[str, str] | None = None, connect_timeout_s: float = 1.0):
        import httpx  # lazy: the in-process demo must not need it

        self.urls = urls or default_urls()
        self.connect_timeout_s = connect_timeout_s
        self._httpx = httpx
        self.client = httpx.Client()

    def close(self) -> None:
        self.client.close()

    def _url(self, tool_name: str, path: str) -> str:
        service = SERVICE_FOR_TOOL[tool_name]
        return f"{self.urls[service].rstrip('/')}{path}"

    def _post(self, tool_name: str, path: str, body: dict, timeout_s: float) -> ToolResult:
        httpx = self._httpx
        timeout = httpx.Timeout(timeout_s, connect=self.connect_timeout_s)
        try:
            resp = self.client.post(self._url(tool_name, path), json=body, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # The request never reached the service, so nothing committed.
            return ToolResult("failed", error=f"{SERVICE_FOR_TOOL[tool_name]} unreachable: {e}")
        except httpx.TimeoutException as e:
            # The request was sent and we never heard back.
            return ToolResult("unknown", error=f"timeout after {timeout_s}s: {e}")
        except Exception as e:
            return ToolResult("unknown", error=f"transport error: {e}")

        if resp.status_code == 409:
            return ToolResult("failed", error=resp.json().get("error", "fenced: stale epoch"))
        if resp.status_code >= 500:
            return ToolResult("unknown", error=f"HTTP {resp.status_code} from service")
        if resp.status_code >= 400:
            return ToolResult("failed", error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        return ToolResult.from_dict(resp.json()) or ToolResult("unknown", error="empty body")

    def execute(
        self, tool_name: str, args: dict, key: str, epoch: int, timeout_s: float
    ) -> ToolResult:
        return self._post(
            tool_name,
            "/execute",
            {
                "tool": tool_name,
                "args": args,
                "key": key,
                "epoch": epoch,
                "workflow_id": workflow_from_key(key),
                "timeout_s": timeout_s,
            },
            timeout_s,
        )

    def compensate(
        self, tool_name: str, args: dict, key: str, epoch: int, timeout_s: float
    ) -> ToolResult:
        return self._post(
            tool_name,
            "/compensate",
            {
                "tool": tool_name,
                "args": args,
                "key": key,
                "epoch": epoch,
                "workflow_id": workflow_from_key(key),
                "timeout_s": timeout_s,
            },
            timeout_s,
        )

    def probe(self, tool_name: str, key: str, timeout_s: float) -> ProbeStatus:
        httpx = self._httpx
        try:
            resp = self.client.get(
                self._url(tool_name, "/probe"),
                params={"tool": tool_name, "key": key},
                timeout=httpx.Timeout(timeout_s, connect=self.connect_timeout_s),
            )
            resp.raise_for_status()
            return resp.json()["status"]
        except Exception:
            return "unknown"

    # --------------------------------------------------------------- admin

    def get_faults(self, service: str, timeout_s: float = 2.0) -> dict:
        resp = self.client.get(
            f"{self.urls[service].rstrip('/')}/admin/faults", timeout=timeout_s
        )
        resp.raise_for_status()
        return resp.json()

    def set_faults(self, service: str, faults: dict, timeout_s: float = 2.0) -> dict:
        resp = self.client.post(
            f"{self.urls[service].rstrip('/')}/admin/faults", json=faults, timeout=timeout_s
        )
        resp.raise_for_status()
        return resp.json()

    def health(self) -> dict:
        services = {}
        for name, base in self.urls.items():
            if name == "ledger":
                continue
            try:
                r = self.client.get(f"{base.rstrip('/')}/health", timeout=0.5)
                services[name] = "up" if r.status_code == 200 else "down"
            except Exception:
                services[name] = "down"
        return {"world": self.name, "services": services, "urls": self.urls}

    def flush_late(self, timeout_s: float = 2.0) -> None:
        for name, base in self.urls.items():
            if name == "ledger":
                continue
            try:
                self.client.post(f"{base.rstrip('/')}/admin/flush_late", timeout=timeout_s)
            except Exception:
                pass

    def reset(self, timeout_s: float = 2.0) -> None:
        for name, base in self.urls.items():
            if name == "ledger":
                continue
            try:
                self.client.post(f"{base.rstrip('/')}/admin/reset", timeout=timeout_s)
            except Exception:
                pass


class HttpLedger:
    """Read side of the ground-truth ledger service."""

    def __init__(self, url: str | None = None, timeout_s: float = 5.0):
        import httpx

        self.url = (url or default_urls()["ledger"]).rstrip("/")
        self.timeout_s = timeout_s
        self.client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self.client.close()

    def _get(self, path: str, **params) -> list | dict:
        params = {k: v for k, v in params.items() if v is not None}
        resp = self.client.get(f"{self.url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def effects(self, tool_name: str | None = None, workflow_id: str | None = None) -> list[dict]:
        return self._get("/effects", tool=tool_name, workflow_id=workflow_id)

    def compensations(self, workflow_id: str | None = None) -> list[dict]:
        return self._get("/compensations", workflow_id=workflow_id)

    def counts(self, net: bool = True, workflow_id: str | None = None) -> dict:
        return self._get("/counts", net=str(bool(net)).lower(), workflow_id=workflow_id)

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
        self.client.post(f"{self.url}/reset")
