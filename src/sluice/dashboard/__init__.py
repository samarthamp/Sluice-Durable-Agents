"""The read-only dashboard (Part 6): a FastAPI app over the journals, plus its page.

The dashboard reads the journal and never drives execution. The optional control
endpoints (``--allow-control``) launch scenarios in separate threads or processes and
are never consulted by the read path.
"""

from .app import DEFAULT_PANES, LIVE_SCENARIOS, STATIC_DIR, create_app

__all__ = ["DEFAULT_PANES", "LIVE_SCENARIOS", "STATIC_DIR", "create_app"]
