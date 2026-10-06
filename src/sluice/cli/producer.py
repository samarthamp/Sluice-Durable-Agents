"""``sluice producer``: synthetic alerts into the Redis stream (3.1, top of the topology)."""

from __future__ import annotations

import argparse
import random
import sys
import time

from ..core.tools import DEMO_ALERT
from ..core.types import Alert
from ..ingest import DEFAULT_GROUP, DEFAULT_STREAM, RedisAlertSource

SERVICES = ["payments-api", "checkout-web", "ledger-svc", "auth-gateway", "search-idx"]
SOURCES = ["prometheus", "datadog", "cloudwatch", "sentry"]
MESSAGES = [
    "error rate spike on checkout",
    "p99 latency above SLO",
    "connection pool exhausted",
    "5xx rate climbing",
    "queue depth growing without drain",
]


def make_alert(i: int, demo: bool) -> Alert:
    if demo:
        # The one the three-pane demo is built around.
        return DEMO_ALERT
    return Alert(
        f"a-{2000 + i}",
        random.choice(SERVICES),
        random.choice(SOURCES),
        {"msg": random.choice(MESSAGES), "value": round(random.uniform(0.1, 0.9), 2)},
    )


def main(argv=None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Sluice alert producer")
    ap.add_argument("--count", type=int, default=10,
                    help="how many alerts to publish (default: %(default)s)")
    ap.add_argument("--rate", type=float, default=2.0,
                    help="alerts per second (default: %(default)s)")
    ap.add_argument("--stream", default=DEFAULT_STREAM,
                    help="Redis stream key (default: %(default)s)")
    ap.add_argument("--group", default=DEFAULT_GROUP,
                    help="consumer group to create if missing (default: %(default)s)")
    ap.add_argument("--demo", action="store_true",
                    help="publish the poison-step alert instead of random ones")
    ap.add_argument("--redeliver", action="store_true",
                    help="publish each alert twice, to exercise at-least-once")
    ap.add_argument("--stats", action="store_true", help="print stream stats and exit")
    args = ap.parse_args(argv)

    try:
        src = RedisAlertSource(stream=args.stream, group=args.group, consumer="producer")
    except Exception as e:
        print(f"redis unavailable: {e}", file=sys.stderr)
        print("start it with:  docker compose up -d", file=sys.stderr)
        return 2

    if args.stats:
        stats = src.stats()
        print(f"  stream {stats['stream']}  depth {stats['depth']}  lag {stats['lag']}")
        for p in stats["pending"]:
            print(f"    pending {p['message_id']} consumer={p['consumer']}"
                  f" idle={p['idle_ms']}ms deliveries={p['deliveries']}")
        if not stats["pending"]:
            print("    no pending entries")
        return 0

    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    published = 0
    try:
        for i in range(args.count):
            alert = make_alert(i, args.demo)
            src.publish(alert)
            published += 1
            line = f"  -> {alert.alert_id}  {alert.service:<14} {alert.payload['msg']}"
            if args.redeliver:
                src.redeliver(alert)
                published += 1
                line += "   (published twice)"
            print(line, flush=True)
            if interval:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass

    stats = src.stats()
    print(f"\n  published {published}; stream depth {stats['depth']}, lag {stats['lag']}")
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
