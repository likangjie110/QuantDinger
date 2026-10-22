"""Factor mining has dedicated Celery capacity without changing other jobs."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def dispatch(monkeypatch):
    from app.tasks import agent_jobs as tasks
    from app.utils import agent_jobs

    database = Mock()

    @contextmanager
    def connect():
        yield database

    monkeypatch.setenv("CELERY_TASKS_ENABLED", "true")
    monkeypatch.setattr(agent_jobs, "get_db_connection", connect)
    monkeypatch.setattr(agent_jobs, "get_billing_service", Mock(return_value=Mock(
        consume_in_transaction=Mock(return_value={"charged": 30}),
    )))
    executor = Mock()
    monkeypatch.setattr(agent_jobs, "_get_executor", Mock(return_value=executor))
    publish = Mock(side_effect=lambda **_kwargs: database.commit.assert_called_once())
    delay = Mock(side_effect=lambda *_args: database.commit.assert_called_once())
    monkeypatch.setattr(tasks.execute_agent_job, "apply_async", publish)
    monkeypatch.setattr(tasks.execute_agent_job, "delay", delay)
    return agent_jobs, database, publish, delay, executor


@pytest.mark.parametrize("kind", ["factor_mining", "backtest", "strategy_evolution"])
def test_job_dispatch_preserves_task_payload_and_isolates_mining(dispatch, kind):
    agent_jobs, database, publish, delay, executor = dispatch
    receipt = agent_jobs.submit_job(
        user_id=9, agent_token_id=None, kind=kind,
        request_payload={"__user_id": 9}, runner=lambda _payload: {}, job_id="job-isolated",
    )

    assert receipt["job_id"] == "job-isolated"
    assert receipt["status"] == "queued"
    database.commit.assert_called_once()
    executor.submit.assert_not_called()
    if kind == "factor_mining":
        publish.assert_called_once_with(args=["job-isolated"], queue="mining")
        delay.assert_not_called()
    else:
        delay.assert_called_once_with("job-isolated")
        publish.assert_not_called()


def test_mining_dispatch_failure_keeps_durable_failure_semantics(dispatch, monkeypatch):
    agent_jobs, _database, publish, delay, executor = dispatch
    publish.side_effect = RuntimeError("broker unavailable")
    delay.side_effect = RuntimeError("broker unavailable")
    failure = Mock()
    monkeypatch.setattr(agent_jobs, "_set_failure", failure)
    monkeypatch.setattr(agent_jobs, "get_job_for_worker", lambda _job_id: {
        "job_id": "job-failed", "kind": "factor_mining", "status": "failed", "request": {},
    })

    receipt = agent_jobs.submit_job(
        user_id=9, agent_token_id=None, kind="factor_mining", request_payload={},
        runner=lambda _payload: {}, job_id="job-failed",
    )

    assert receipt["status"] == "failed"
    failure.assert_called_once_with("job-failed", "AGENT_JOB_DISPATCH_FAILED")
    executor.submit.assert_not_called()


def test_standalone_mining_keeps_explicit_local_executor(dispatch, monkeypatch):
    agent_jobs, _database, publish, delay, executor = dispatch
    monkeypatch.setenv("CELERY_TASKS_ENABLED", "false")

    receipt = agent_jobs.submit_job(
        user_id=9, agent_token_id=None, kind="factor_mining", request_payload={},
        runner=lambda _payload: {}, job_id="job-local",
    )

    assert receipt["status"] == "queued"
    executor.submit.assert_called_once()
    publish.assert_not_called()
    delay.assert_not_called()


def test_declared_mining_queue_uses_its_own_durable_binding():
    from app.celery_app import celery_app

    queues = {queue.name: queue for queue in celery_app.conf.task_queues or ()}
    assert {"mining", "jobs", "ai", "maintenance"} <= set(queues)
    mining = queues["mining"]
    assert mining.durable
    assert mining.exchange.name == "mining"
    assert mining.routing_key == "mining"
    routes = celery_app.conf.task_routes
    assert routes["quantdinger.tasks.agent_job"]["queue"] == "jobs"
    assert routes["quantdinger.tasks.fast_analysis"]["queue"] == "ai"
    assert routes["quantdinger.tasks.worker_heartbeat"]["queue"] == "maintenance"
    routed = celery_app.amqp.Router().route(
        {"queue": "mining"}, "quantdinger.tasks.agent_job", args=["job-mining"],
    )
    assert routed["queue"].name == "mining"


def _environment(service):
    environment = service["environment"]
    if isinstance(environment, list):
        return dict(value.split("=", 1) for value in environment)
    return environment


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.ghcr.yml"])
def test_compose_gives_mining_dedicated_capacity_and_local_readiness(filename):
    services = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))["services"]
    assert "celery-mining-worker" in services
    mining = services["celery-mining-worker"]
    general = services["celery-worker"]
    assert mining["command"][mining["command"].index("-Q") + 1] == "mining"
    assert general["command"][general["command"].index("-Q") + 1] == "jobs,ai,maintenance"
    assert "--concurrency=${CELERY_MINING_CONCURRENCY:-1}" in mining["command"]
    assert "--prefetch-multiplier=${CELERY_MINING_PREFETCH:-1}" in mining["command"]
    assert mining["deploy"]["replicas"] == "${CELERY_MINING_REPLICAS:-1}"
    assert "container_name" not in mining
    assert mining["mem_limit"] == "${CELERY_MINING_MEMORY_LIMIT:-2g}"
    assert mining["cpus"] == "${CELERY_MINING_CPU_LIMIT:-1.0}"
    environment = _environment(mining)
    assert environment["QD_PROCESS_ROLE"] == "celery"
    assert environment["CELERY_REDIS_HOST"] == "redis-jobs"
    assert environment["DB_APPLICATION_NAME"] == "quantdinger-celery-mining"
    assert mining["depends_on"]["migration"]["condition"] == "service_completed_successfully"
    assert mining["depends_on"]["redis-jobs"]["condition"] == "service_healthy"
    assert mining["healthcheck"]["test"][-2:] == ["--queues", "mining"]
    assert general["healthcheck"]["test"][-2:] == ["--queues", "jobs,ai,maintenance"]


def test_production_override_locks_mining_worker_with_its_own_resource_budget():
    services = yaml.safe_load((ROOT / "docker-compose.production.yml").read_text(encoding="utf-8"))["services"]
    assert "celery-mining-worker" in services
    mining = services["celery-mining-worker"]
    assert mining["read_only"] is True
    assert mining["user"] == "10001:10001"
    assert mining["cap_drop"] == ["ALL"]
    assert mining["mem_limit"] == "${CELERY_MINING_MEMORY_LIMIT:-2g}"
    assert mining["cpus"] == "${CELERY_MINING_CPU_LIMIT:-1.0}"
