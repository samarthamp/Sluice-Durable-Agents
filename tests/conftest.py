"""Shared fixtures.

The suite runs straight from a checkout: ``pyproject.toml`` puts ``src/`` on the path,
so no install is needed. Fixtures that touch the network live in
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import pytest

from sluice.core.journal import Journal
from sluice.scenarios import make_ctx
from sluice.world import FaultConfig, GroundTruthLedger, InProcessWorld


@pytest.fixture
def db_path(tmp_path) -> str:
    """A journal path in a fresh temporary directory."""
    return str(tmp_path / "journal.db")


@pytest.fixture
def journal(db_path):
    j = Journal(db_path)
    yield j
    j.close()


@pytest.fixture
def ledger() -> GroundTruthLedger:
    return GroundTruthLedger()


@pytest.fixture
def faults() -> FaultConfig:
    return FaultConfig()


@pytest.fixture
def world(ledger, faults):
    w = InProcessWorld(ledger, faults)
    yield w
    # Late deliveries are daemon timers; cancel any a test left armed.
    for timer in w.pending_late:
        timer.cancel()


@pytest.fixture
def ctx(db_path):
    """An in-process scenario context: fresh journal, ledger and world."""
    c = make_ctx(db_path)
    yield c
    c.close()
