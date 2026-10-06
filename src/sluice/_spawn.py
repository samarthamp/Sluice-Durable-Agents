"""Launching sibling ``sluice`` processes.

A child must import the same package as its parent, however the parent got it: an
install, an editable install, ``PYTHONPATH=src``, or a ``sys.path`` entry added at
runtime (pytest's ``pythonpath`` option), which the environment does not carry.
"""

from __future__ import annotations

import os
import sys

# The directory that contains the ``sluice`` package: ``src/`` in a checkout.
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sluice_cmd(command: str, *args: str) -> list[str]:
    """argv for ``python -m sluice <command> <args...>`` on this interpreter."""
    return [sys.executable, "-m", "sluice", command, *args]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment, with the package root on ``PYTHONPATH``."""
    env = dict(os.environ)
    # An installed package is already importable; adding site-packages to PYTHONPATH
    # would only move it ahead of the standard library.
    if not os.path.basename(PACKAGE_ROOT).endswith("-packages"):
        current = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        if PACKAGE_ROOT not in current:
            env["PYTHONPATH"] = os.pathsep.join([PACKAGE_ROOT, *current])
    if extra:
        env.update(extra)
    return env
