"""AlertSource: the in-process queue, drain(), and the fallback when Redis is absent.

The Redis-backed source is exercised against a real server in
tests/integration/test_redis_ingest.py.
"""

from __future__ import annotations

import pytest

from sluice.core.types import Alert
from sluice.ingest import InProcessAlertSource, alert_source, drain

A1 = Alert("a-1", "payments-api", "prometheus", {})
A2 = Alert("a-2", "checkout-web", "datadog", {})

# Port 1 is never a Redis server, so connecting is refused immediately.
NO_REDIS = "redis://127.0.0.1:1/0"


def test_in_process_source_delivers_in_order_and_stops_when_empty():
    src = InProcessAlertSource([A1, A2])
    assert src.depth() == src.lag() == 2
    assert [a.alert_id for a in src.consume()] == ["a-1", "a-2"]
    assert list(src.consume()) == []


def test_redelivery_hands_the_same_alert_out_again():
    src = InProcessAlertSource([A1])
    src.redeliver(A1)
    assert [a.alert_id for a in src.consume()] == ["a-1", "a-1"]


def test_pending_is_delivered_but_not_acked():
    src = InProcessAlertSource([A1, A2])
    list(src.consume())
    src.ack("a-1")
    assert [p["message_id"] for p in src.pending()] == ["a-2"]
    stats = src.stats()
    assert stats["stream"] == "in-process" and len(stats["pending"]) == 1
    assert src.claim_stale() == []


def test_drain_acks_only_after_the_handler_returns():
    src = InProcessAlertSource([A1, A2])
    seen = []
    out = drain(src, lambda a: seen.append(a.alert_id) or {"ok": a.alert_id})
    assert seen == ["a-1", "a-2"]
    assert [o["result"] for o in out] == [{"ok": "a-1"}, {"ok": "a-2"}]
    assert src.acked == {"a-1", "a-2"} and src.pending() == []


def test_an_alert_whose_handler_dies_stays_pending():
    """The recovery signal after a node dies: delivered, never acknowledged."""
    src = InProcessAlertSource([A1])

    def crash(_alert):
        raise RuntimeError("orchestrator died")

    with pytest.raises(RuntimeError):
        drain(src, crash)
    assert [p["message_id"] for p in src.pending()] == ["a-1"]


def test_drain_respects_max_alerts_and_ack_false():
    src = InProcessAlertSource([A1, A2])
    out = drain(src, lambda a: None, ack=False, max_alerts=1)
    assert len(out) == 1 and src.acked == set()
    assert src.depth() == 1


def test_factory_returns_the_in_process_queue_when_asked(capsys):
    src = alert_source(use_redis=False, alerts=[A1])
    assert isinstance(src, InProcessAlertSource) and src.depth() == 1
    assert "[ingest] in-process queue" in capsys.readouterr().out


def test_factory_falls_back_loudly_when_redis_is_unreachable(monkeypatch, capsys):
    monkeypatch.setenv("REDIS_URL", NO_REDIS)
    src = alert_source(use_redis=True, alerts=[A1])
    assert isinstance(src, InProcessAlertSource)
    out = capsys.readouterr().out
    # This line is the one to watch for: a fallback run proves nothing about the stream.
    assert "[ingest] redis unavailable" in out and "falling back to in-process queue" in out
    assert [a.alert_id for a in src.consume()] == ["a-1"]


def test_factory_reads_the_redis_switch_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("REDIS_URL", NO_REDIS)
    monkeypatch.setenv("SLUICE_REDIS", "1")
    assert isinstance(alert_source(), InProcessAlertSource)
    assert "redis unavailable" in capsys.readouterr().out

    monkeypatch.setenv("SLUICE_REDIS", "0")
    alert_source(verbose=False)
    assert capsys.readouterr().out == ""
