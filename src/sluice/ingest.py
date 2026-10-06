"""AlertSource: Redis Streams with an in-process fallback (3.2)."""

from __future__ import annotations

import json
import os
import queue
import time
from collections.abc import Iterator

from .core.types import Alert

DEFAULT_STREAM = "alerts:incoming"
DEFAULT_GROUP = "orchestrators"


class InProcessAlertSource:
    """A thread-safe queue standing in for the stream.  Same Protocol."""

    name = "in-process"

    def __init__(self, alerts: list[Alert] | None = None, block_s: float = 0.0):
        self.q: "queue.Queue[Alert]" = queue.Queue()
        self.block_s = block_s
        self.delivered: list[str] = []
        self.acked: set[str] = set()
        for a in alerts or []:
            self.publish(a)

    def publish(self, alert: Alert) -> None:
        self.q.put(alert)

    def redeliver(self, alert: Alert) -> None:
        """At-least-once: hand the same alert out a second time."""
        self.q.put(alert)

    def consume(self) -> Iterator[Alert]:
        while True:
            try:
                alert = self.q.get(timeout=self.block_s) if self.block_s else self.q.get_nowait()
            except queue.Empty:
                return
            self.delivered.append(alert.alert_id)
            yield alert

    def ack(self, alert_id: str) -> None:
        self.acked.add(alert_id)

    def pending(self) -> list[dict]:
        """Delivered but never acked -- the recovery signal after a node dies."""
        return [
            {"message_id": a, "consumer": "in-process", "idle_ms": 0, "deliveries": 1}
            for a in self.delivered
            if a not in self.acked
        ]

    def claim_stale(self, min_idle_ms: int = 5000, count: int = 10) -> list[Alert]:
        return []

    def lag(self) -> int:
        return self.q.qsize()

    def depth(self) -> int:
        return self.q.qsize()

    def stats(self) -> dict:
        return {
            "stream": "in-process",
            "group": "-",
            "consumer": "-",
            "depth": self.depth(),
            "lag": self.lag(),
            "pending": self.pending(),
        }

    def close(self) -> None:
        pass


class RedisAlertSource:
    """Redis Streams with a consumer group."""

    name = "redis"

    def __init__(
        self,
        url: str | None = None,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_GROUP,
        consumer: str = "orch-a",
        block_ms: int = 1000,
        count: int = 10,
    ):
        import redis  # imported lazily so the core demo has no third-party dependency

        self.url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.block_ms = block_ms
        self.count = count
        self.r = redis.Redis.from_url(self.url, decode_responses=True)
        self.r.ping()
        self._msg_ids: dict[str, str] = {}
        try:
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP: the group already exists
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, alert: Alert) -> str:
        return self.r.xadd(self.stream, {"alert": json.dumps(alert.to_dict())})

    def redeliver(self, alert: Alert) -> str:
        return self.publish(alert)

    def consume(self) -> Iterator[Alert]:
        while True:
            resp = self.r.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=self.count,
                block=self.block_ms,
            )
            if not resp:
                return
            for _stream, messages in resp:
                for msg_id, fields in messages:
                    alert = Alert.from_dict(json.loads(fields["alert"]))
                    self._msg_ids[alert.alert_id] = msg_id
                    yield alert

    def ack(self, alert_id: str) -> None:
        msg_id = self._msg_ids.pop(alert_id, None)
        if msg_id:
            self.r.xack(self.stream, self.group, msg_id)

    def pending(self) -> list[dict]:
        """Entries delivered to some consumer and never acknowledged."""
        try:
            entries = self.r.xpending_range(
                self.stream, self.group, min="-", max="+", count=100
            )
        except Exception:
            return []
        return [
            {
                "message_id": e["message_id"],
                "consumer": e["consumer"],
                "idle_ms": int(e["time_since_delivered"]),
                "deliveries": int(e["times_delivered"]),
            }
            for e in entries
        ]

    def claim_stale(self, min_idle_ms: int = 5000, count: int = 10) -> list[Alert]:
        """Take over entries a dead consumer never acknowledged."""
        try:
            _cursor, messages, *_ = self.r.xautoclaim(
                self.stream, self.group, self.consumer,
                min_idle_time=min_idle_ms, count=count,
            )
        except Exception:
            return []
        out = []
        for msg_id, fields in messages or []:
            if not fields:
                continue
            alert = Alert.from_dict(json.loads(fields["alert"]))
            self._msg_ids[alert.alert_id] = msg_id
            out.append(alert)
        return out

    def lag(self) -> int:
        """Undelivered entries behind this group's cursor -- the backpressure metric."""
        try:
            for g in self.r.xinfo_groups(self.stream):
                if g["name"] == self.group:
                    lag = g.get("lag")
                    if lag is not None:
                        return int(lag)
                    return int(self.r.xlen(self.stream)) - int(g.get("entries-read") or 0)
        except Exception:
            pass
        return 0

    def depth(self) -> int:
        try:
            return int(self.r.xlen(self.stream))
        except Exception:
            return 0

    def stats(self) -> dict:
        return {
            "stream": self.stream,
            "group": self.group,
            "consumer": self.consumer,
            "depth": self.depth(),
            "lag": self.lag(),
            "pending": self.pending(),
        }

    def close(self) -> None:
        try:
            self.r.close()
        except Exception:
            pass


def alert_source(
    use_redis: bool | None = None,
    alerts: list[Alert] | None = None,
    consumer: str = "orch-a",
    verbose: bool = True,
):
    """Factory with the fallback wired in."""
    if use_redis is None:
        use_redis = os.environ.get("SLUICE_REDIS", "").lower() in ("1", "true", "yes")

    if use_redis:
        try:
            src = RedisAlertSource(consumer=consumer)
            for a in alerts or []:
                src.publish(a)
            if verbose:
                print(f"[ingest] redis stream {src.stream} group {src.group}")
            return src
        except Exception as e:
            if verbose:
                print(f"[ingest] redis unavailable ({e}); falling back to in-process queue")

    if verbose:
        print("[ingest] in-process queue")
    return InProcessAlertSource(alerts)


def drain(source, handler, ack: bool = True, max_alerts: int | None = None) -> list:
    """Consume alerts and hand each to ``handler``.  Acks only after the handler
    returns, so an alert whose orchestrator died stays pending and is redelivered."""
    out = []
    n = 0
    for alert in source.consume():
        started = time.time()
        result = handler(alert)
        out.append({"alert_id": alert.alert_id, "result": result, "elapsed": time.time() - started})
        if ack:
            source.ack(alert.alert_id)
        n += 1
        if max_alerts is not None and n >= max_alerts:
            break
    return out
