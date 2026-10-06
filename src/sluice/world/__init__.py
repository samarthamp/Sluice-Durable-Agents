"""The effect layer behind the ``World`` protocol (3.6).

``InProcessWorld`` and the ground-truth ledger are stdlib only. The HTTP side lives in
``http_client`` (``HttpWorld``, ``HttpLedger``) and ``services`` (the FastAPI apps), and
pulls in its third-party dependencies only when used.
"""

from .faults import FAULT_PRESETS, NO_FAULTS, FaultConfig
from .inprocess import InProcessWorld
from .ledger import GroundTruthLedger

__all__ = ["FAULT_PRESETS", "NO_FAULTS", "FaultConfig", "GroundTruthLedger", "InProcessWorld"]
