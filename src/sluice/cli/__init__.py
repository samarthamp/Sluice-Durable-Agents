"""The ``sluice`` command: one entry point, one subcommand per process role.

    sluice demo          three panes, crash sweep, failover, overhead benchmark
    sluice services      ledger + ticket/channel/pager effect services (HTTP)
    sluice orchestrator  one orchestrator process (run two for a leader race)
    sluice producer      synthetic alerts into the Redis stream
    sluice dashboard     the read-only dashboard
    sluice smoke         layer-by-layer check of a running topology
    sluice faults        show or set fault injection on the running services

Each subcommand lives in its own module with a ``main(argv, prog)`` and is imported
only when it runs, so ``sluice demo`` never imports the HTTP stack.
"""

from __future__ import annotations

import importlib
import sys

# command -> (module, one-line summary)
COMMANDS: dict[str, tuple[str, str]] = {
    "demo": ("sluice.cli.demo",
             "three-pane scenarios, crash sweep, failover, overhead benchmark"),
    "services": ("sluice.cli.services",
                 "start the ledger and the ticket/channel/pager effect services"),
    "orchestrator": ("sluice.cli.orchestrator",
                     "run one orchestrator process against the shared journal"),
    "producer": ("sluice.cli.producer",
                 "publish synthetic alerts to the Redis stream"),
    "dashboard": ("sluice.cli.dashboard",
                  "serve the read-only dashboard"),
    "smoke": ("sluice.cli.smoke",
              "layer-by-layer check of a running topology"),
    "faults": ("sluice.cli.faults",
               "show or set fault injection on the running services"),
}


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        "usage: sluice <command> [options]",
        "",
        "Divergence-safe durable execution for agent decisioning.",
        "",
        "commands:",
    ]
    lines += [f"  {name:<{width}}  {summary}" for name, (_mod, summary) in COMMANDS.items()]
    lines += ["", "Run 'sluice <command> --help' for that command's options."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if args else 2
    if args[0] in ("-V", "--version"):
        from .. import __version__

        print(f"sluice {__version__}")
        return 0

    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        print(f"sluice: unknown command {command!r}\n", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 2

    module = importlib.import_module(COMMANDS[command][0])
    return module.main(rest, prog=f"sluice {command}")
