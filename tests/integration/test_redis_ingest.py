"""Redis Streams ingest against a real Redis (3.2): consumer groups, pending entries,
reclaiming a dead consumer's work, and at-least-once delivery absorbed downstream.

Skipped when Redis is not reachable; start it with ``docker compose up -d``.
"""

from __future__ import annotations

import time

import pytest

from sluice.core.engine import recover
from sluice.core.tools import DEMO_ALERT
from sluice.core.types import Alert, workflow_id_for
from sluice.ingest import DEFAULT_GROUP, DEFAULT_STREAM, alert_source, drain

pytestmark = pytest.mark.redis

A1 = Alert("a-1", "payments-api", "prometheus", {"msg": "m"})
A2 = Alert("a-2", "checkout-web", "datadog", {"msg": "n"})


@pytest.fixture
def source_factory(redis_url):
    from sluice.ingest import RedisAlertSource

    made = []

    def make(consumer: str = "c-a", **kw):
        src = RedisAlertSource(url=redis_url, stream="alerts:test", group="g",
                               consumer=consumer, block_ms=100, **kw)
        made.append(src)
        return src

    yield make
    for src in made:
        src.close()


def test_drain_delivers_each_alert_once_and_acks_it(source_factory):
    src = source_factory()
    src.publish(A1)
    src.publish(A2)
    out = drain(src, lambda a: a.alert_id)
    assert [o["result"] for o in out] == ["a-1", "a-2"]
    assert src.pending() == []
    assert list(src.consume()) == []


def test_unacked_entries_stay_pending_with_their_consumer(source_factory):
    src = source_factory(consumer="c-dead")
    src.publish(A1)
    assert [a.alert_id for a in src.consume()] == ["a-1"]
    (entry,) = src.pending()
    assert entry["consumer"] == "c-dead" and entry["deliveries"] == 1


def test_a_survivor_reclaims_what_a_dead_consumer_never_acked(source_factory):
    dead = source_factory(consumer="c-dead")
    dead.publish(A1)
    list(dead.consume())          # delivered to c-dead, which then "dies"

    survivor = source_factory(consumer="c-alive")
    assert survivor.claim_stale(min_idle_ms=60_000) == []  # not idle long enough yet
    time.sleep(0.2)
    claimed = survivor.claim_stale(min_idle_ms=100)
    assert [a.alert_id for a in claimed] == ["a-1"]
    assert survivor.pending()[0]["consumer"] == "c-alive"
    survivor.ack("a-1")
    assert survivor.pending() == []


def test_a_second_source_on_an_existing_group_reuses_it(source_factory):
    source_factory(consumer="c-a").publish(A1)
    other = source_factory(consumer="c-b")  # BUSYGROUP is expected, not an error
    assert [a.alert_id for a in other.consume()] == ["a-1"]


def test_depth_lag_and_stats(source_factory):
    src = source_factory(count=1)
    for alert in (A1, A2, A1):
        src.publish(alert)
    assert src.depth() == 3 and src.lag() == 3
    drain(src, lambda a: None, max_alerts=1)
    assert src.lag() == 2
    stats = src.stats()
    assert (stats["stream"], stats["group"], stats["consumer"]) == ("alerts:test", "g", "c-a")
    assert stats["depth"] == 3 and stats["pending"] == []


def test_the_factory_reports_the_real_stream(redis_url, capsys):
    src = alert_source(use_redis=True, consumer="c-factory")
    try:
        assert src.name == "redis"
        assert (f"[ingest] redis stream {DEFAULT_STREAM} group {DEFAULT_GROUP}"
                in capsys.readouterr().out)
    finally:
        src.close()


def test_redelivery_through_the_stream_collapses_into_one_workflow(source_factory, journal,
                                                                    world, ledger):
    src = source_factory()
    src.publish(DEMO_ALERT)
    src.redeliver(DEMO_ALERT)

    out = drain(src, lambda a: recover(journal, world, a, "P1", owner="orch-test"))
    assert len(out) == 2
    assert journal.workflows() == [workflow_id_for(DEMO_ALERT.alert_id)]
    assert ledger.scoreboard(workflow_id_for(DEMO_ALERT.alert_id)) == (1, 1, 1)
