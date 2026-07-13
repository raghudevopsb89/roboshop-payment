"""Test bootstrap for the payment service.

This module runs before any test module is imported, so it is the right place to
neutralise anything that would otherwise reach out to real infrastructure at
import time.  ``main.py`` calls ``newrelic.agent.initialize()`` on import, so we
stub that out here before ``main`` is ever imported by a test.
"""
import os
import sys
from unittest.mock import MagicMock

# --- Environment: point external services at obviously-fake hosts so that even
# if a real network call slipped through it would fail fast rather than hit
# anything real. ---
os.environ.setdefault("USER_URL", "http://user.test")
os.environ.setdefault("CART_URL", "http://cart.test")
os.environ.setdefault("AMQP_HOST", "rabbitmq.test")

# --- Neutralise New Relic so importing main.py does not initialise the agent. ---
try:
    import newrelic.agent  # noqa: E402

    newrelic.agent.initialize = lambda *a, **k: None
except Exception:  # pragma: no cover - fall back to a full stub if not installed
    nr = MagicMock()
    sys.modules["newrelic"] = nr
    sys.modules["newrelic.agent"] = nr.agent
