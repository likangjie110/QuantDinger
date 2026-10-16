"""Shared pytest fixtures."""
import asyncio
import os
import sys

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Minimal env so config classes don't blow up
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-32-bytes-minimum")
os.environ.setdefault("ADMIN_USER", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testpass123")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("SKIP_STARTUP_HOOKS", "1")

# Some optional trading dependencies still call asyncio.get_event_loop() at
# import time. Python 3.13 warns when no current loop exists, so provide one for
# the test session before importing the app.
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import pytest
import requests
from app import create_app


class NetworkReached(BaseException):
    """A test tried to open a connection. Deliberately not an ``Exception``.

    Every venue assembler in ``app.data_sources.derivatives`` wraps its request in
    ``except Exception`` and answers an empty series instead, so a guard that raised an ordinary
    exception would be swallowed into a column of NaN and the test would pass with no data at
    all -- the silent shape it exists to prevent. ``tests/test_derivatives_source._Unrecorded``
    is the same device for the same reason.
    """


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """No test opens a connection: a request fails here, before it leaves the process.

    Unit tests stub what they read, so a test that reaches a venue has stubbed the wrong name or
    is exercising a path nobody meant to run -- both cheaper to find as a failure here than as a
    rate-limit error and a column of nothing. A test with its own ``requests`` double patches
    over this one and keeps working. Live checks live under ``tests/integration`` and are run by
    hand; ``requests`` is the client this repository's data layer uses.
    """
    def blocked(*_args, **_kwargs):
        raise NetworkReached("a unit test tried to open a connection")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "request", blocked)


@pytest.fixture(scope="session")
def app():
    """Create application for testing."""
    application = create_app("testing")
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
