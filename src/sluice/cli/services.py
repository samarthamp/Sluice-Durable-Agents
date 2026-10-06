"""``sluice services``: the HTTP topology, three effect services plus the ledger (3.1).

Without ``--service`` it launches all four as separate OS processes and supervises them.
With ``--service NAME`` it runs that one service in the foreground.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time

from .._spawn import child_env, sluice_cmd
from ..world.http_client import DEFAULT_PORTS

SERVICES = ["ledger", "ticket", "channel", "pager"]


def serve_one(name: str, port: int, ledger_url: str, host: str) -> None:
    import uvicorn

    from ..world.services import build_app

    app = build_app(name, ledger_url=ledger_url)
    print(f"[{name}] listening on http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def serve_all(host: str, ledger_url: str) -> int:
    procs: list[tuple[str, subprocess.Popen]] = []
    env = child_env()

    try:
        for name in SERVICES:
            cmd = sluice_cmd(
                "services",
                "--service", name,
                "--port", str(DEFAULT_PORTS[name]),
                "--host", host,
                "--ledger-url", ledger_url,
            )
            p = subprocess.Popen(cmd, env=env)
            procs.append((name, p))
            print(f"  {name:<8} pid {p.pid}  http://{host}:{DEFAULT_PORTS[name]}", flush=True)
            # The ledger must be up before the effect services try to write to it.
            time.sleep(0.6 if name == "ledger" else 0.25)

        print("\n  all four up. ctrl-c to stop, or kill one pid to break a service.")
        print("  then:  sluice demo --world http\n", flush=True)

        while True:
            time.sleep(1.0)
            for name, p in procs:
                if p.poll() is not None:
                    print(f"  [{name}] exited with {p.returncode}", flush=True)
    except KeyboardInterrupt:
        print("\n  stopping...", flush=True)
    finally:
        for _name, p in procs:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except Exception:
                    p.kill()
        for _name, p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
    return 0


def main(argv=None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Sluice effect services")
    ap.add_argument("--service", choices=SERVICES, default=None,
                    help="run a single service in the foreground")
    ap.add_argument("--port", type=int, default=None,
                    help="with --service: listen here (default: 8100-8103 by service)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    ap.add_argument("--ledger-url", default=None,
                    help="where effect services record ground truth"
                         " (default: http://HOST:8100)")
    args = ap.parse_args(argv)

    ledger_url = args.ledger_url or f"http://{args.host}:{DEFAULT_PORTS['ledger']}"

    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as e:
        print(f"the HTTP topology needs the optional deps: {e}", file=sys.stderr)
        print('  pip install -e ".[http]"', file=sys.stderr)
        return 2

    if args.service:
        serve_one(args.service, args.port or DEFAULT_PORTS[args.service], ledger_url, args.host)
        return 0
    return serve_all(args.host, ledger_url)


if __name__ == "__main__":
    sys.exit(main())
