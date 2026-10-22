"""A Celery container must prove its own queue subscription is ready."""

from __future__ import annotations

import socket
import sys
from unittest.mock import Mock

import pytest


@pytest.fixture
def readiness(monkeypatch):
    from app.celery_app import celery_app
    from app.commands import worker_health

    monkeypatch.setattr(socket, "gethostname", lambda: "mining-container")
    inspector = Mock()
    inspect = Mock(return_value=inspector)
    monkeypatch.setattr(celery_app.control, "inspect", inspect)
    database = Mock(side_effect=AssertionError("a shared heartbeat cannot prove local readiness"))
    monkeypatch.setattr(worker_health, "get_db_connection", database)
    monkeypatch.setattr(sys, "argv", ["worker_health", "celery", "--queues", "mining"])
    return worker_health, inspect, inspector, database


@pytest.mark.parametrize("queues", ["mining", "jobs,ai,maintenance"])
def test_readiness_accepts_only_the_local_worker_expected_queues(readiness, monkeypatch, queues):
    worker_health, inspect, inspector, database = readiness
    monkeypatch.setattr(sys, "argv", ["worker_health", "celery", "--queues", queues])
    inspector.active_queues.return_value = {
        "celery@mining-container": [{"name": name} for name in queues.split(",")],
    }

    worker_health.main()

    inspect.assert_called_once_with(destination=["celery@mining-container"], timeout=3.0)
    database.assert_not_called()


@pytest.mark.parametrize("response", [
    None,
    {},
    {"celery@another-container": [{"name": "mining"}]},
    {"celery@mining-container": []},
    {"celery@mining-container": [{"name": "jobs"}]},
    {"celery@mining-container": [{"name": "mining"}, {"name": "jobs"}]},
    {"celery@mining-container": [{"unexpected": "mining"}]},
])
def test_readiness_rejects_missing_wrong_or_shared_queue_consumers(readiness, response):
    worker_health, _inspect, inspector, database = readiness
    inspector.active_queues.return_value = response

    with pytest.raises(SystemExit) as stopped:
        worker_health.main()

    assert stopped.value.code == 1
    database.assert_not_called()


def test_readiness_fails_closed_on_broker_or_inspection_failure(readiness):
    worker_health, _inspect, inspector, database = readiness
    inspector.active_queues.side_effect = ConnectionError("broker unavailable")

    with pytest.raises(SystemExit) as stopped:
        worker_health.main()

    assert stopped.value.code == 1
    database.assert_not_called()
