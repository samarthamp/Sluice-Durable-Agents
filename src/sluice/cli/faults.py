"""``sluice faults``: show or set fault injection on the running effect services.

    sluice faults                      show what each service is injecting now
    sluice faults poison               rota-X has nobody on call (the poison step)
    sluice faults zombie               the pager times out, then the page lands late
    sluice faults clear                switch every fault off
    sluice faults poison --service pager

The services are long-lived and keep their faults until told otherwise, and
``/admin/reset`` clears idempotency keys and epochs but not faults. Set them
explicitly before a distributed run rather than relying on whatever ran last.
"""

from __future__ import annotations

import argparse
import sys

from ..world.faults import FAULT_PRESETS, NO_FAULTS

EFFECT_SERVICES = ("ticket", "channel", "pager")


def describe(faults: dict) -> str:
    """Only what differs from 'no faults', so the active injection is easy to read."""
    active = [
        f"{name}={value}"
        for name, value in sorted(faults.items())
        if name != "late_delivery_delay_s" and value != NO_FAULTS.get(name)
    ]
    if faults.get("late_delivery_tools"):
        active.append(f"late_delivery_delay_s={faults.get('late_delivery_delay_s')}")
    return ", ".join(active) if active else "no faults"


def main(argv=None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog=prog, description="Show or set fault injection on the running effect services"
    )
    ap.add_argument("preset", nargs="?", choices=sorted(FAULT_PRESETS),
                    help="apply this preset; omit it to show the current faults")
    ap.add_argument("--service", action="append", choices=EFFECT_SERVICES,
                    help="only this service (repeatable); default: all three")
    args = ap.parse_args(argv)

    try:
        from ..world.http_client import HttpWorld

        world = HttpWorld()
    except ImportError as e:
        print(f'needs the HTTP extras ({e}):  pip install -e ".[http]"', file=sys.stderr)
        return 2

    unreachable = 0
    try:
        for service in args.service or EFFECT_SERVICES:
            try:
                if args.preset:
                    faults = world.set_faults(service, FAULT_PRESETS[args.preset])
                else:
                    faults = world.get_faults(service)
            except Exception as e:
                print(f"  {service:<8} unreachable: {e}", file=sys.stderr)
                unreachable += 1
                continue
            print(f"  {service:<8} {describe(faults)}")
    finally:
        world.close()

    if unreachable:
        print("  start the services with:  sluice services", file=sys.stderr)
    return 1 if unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
