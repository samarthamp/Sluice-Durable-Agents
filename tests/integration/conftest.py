"""Fixtures for tests that cross real boundaries: OS processes, sockets, Redis.

* ``http_services`` (session): the four services as separate processes on free ports,
  launched exactly as production launches them (``python -m sluice services
  --service NAME``). It never touches the default ports 8100-8103, so a topology you
  have running for a demo is left alone.
* ``topology`` (per test): the same processes, reset to a clean state, with
  ``SLUICE_*_URL`` pointing at them so every client in the code finds them.
* ``redis_url`` (per test): a Redis database reserved for tests -- DB 15 on the server
  ``REDIS_URL`` names, or ``SLUICE_TEST_REDIS_URL`` -- flushed before and after.
  Skipped, with the reason, when Redis is not reachable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from urllib.parse import urlparse, urlunparse

import pytest

from sluice._spawn import child_env, sluice_cmd

SERVICES = ("ledger", "ticket", "channel", "pager")


def redis_test_url() -> str:
    """DB 15 on the configured server, so tests never touch the demo stream on DB 0."""
    explicit = os.environ.get("SLUICE_TEST_REDIS_URL")
    if explicit:
        return explicit
    server = urlparse(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    return urlunparse(server._replace(path="/15"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(urls: dict[str, str]) -> bool:
    import httpx

    try:
        return all(httpx.get(f"{u}/health", timeout=0.5).status_code == 200
                   for u in urls.values())
    except Exception:
        return False


@pytest.fixture(scope="session")
def http_services(tmp_path_factory):
    for module in ("fastapi", "uvicorn", "httpx"):
        pytest.importorskip(module, reason='needs the HTTP extras: pip install -e ".[http]"')

    ports = {name: free_port() for name in SERVICES}
    urls = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
    logs = tmp_path_factory.mktemp("services")
    procs: list[tuple[str, subprocess.Popen, object]] = []
    env = child_env()

    for name in SERVICES:
        log = open(logs / f"{name}.log", "w")  # noqa: SIM115 -- closed at teardown
        cmd = sluice_cmd("services", "--service", name, "--port", str(ports[name]),
                             "--ledger-url", urls["ledger"])
        procs.append((name, subprocess.Popen(cmd, env=env, stdout=log,
                                             stderr=subprocess.STDOUT), log))

    deadline = time.monotonic() + 30
    while not _healthy(urls):
        dead = [(n, p.returncode) for n, p, _ in procs if p.poll() is not None]
        if dead or time.monotonic() > deadline:
            for _n, p, _log in procs:
                p.kill()
            detail = "\n".join(
                f"--- {n}\n" + (logs / f"{n}.log").read_text(encoding="utf-8")
                for n in SERVICES
            )
            pytest.fail(f"services did not come up (exited: {dead})\n{detail}")
        time.sleep(0.2)

    yield {"urls": urls, "pids": {n: p.pid for n, p, _ in procs}, "logs": logs}

    for _n, p, _log in procs:
        p.terminate()
    for _n, p, log in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()


@pytest.fixture
def topology(http_services, monkeypatch):
    """The live services, reset: no idempotency keys, no epochs, no faults, empty ledger."""
    import httpx

    from sluice.world.faults import NO_FAULTS

    urls = http_services["urls"]
    for name, url in urls.items():
        monkeypatch.setenv(f"SLUICE_{name.upper()}_URL", url)
        if name == "ledger":
            httpx.post(f"{url}/reset", timeout=5).raise_for_status()
        else:
            httpx.post(f"{url}/admin/reset", timeout=5).raise_for_status()
            httpx.post(f"{url}/admin/faults", json=NO_FAULTS, timeout=5).raise_for_status()
    return http_services


@pytest.fixture
def redis_url(monkeypatch):
    redis = pytest.importorskip("redis", reason='needs the Redis extra: pip install -e ".[redis]"')

    url = redis_test_url()
    db = (urlparse(url).path or "/0").lstrip("/") or "0"
    if db == "0":
        pytest.skip("refusing to flush Redis DB 0; point SLUICE_TEST_REDIS_URL elsewhere")
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=1)
        client.ping()
    except Exception as e:
        pytest.skip(f"Redis not reachable at {url} ({e}); start it with: docker compose up -d")

    client.flushdb()
    monkeypatch.setenv("REDIS_URL", url)
    yield url
    client.flushdb()
    client.close()
