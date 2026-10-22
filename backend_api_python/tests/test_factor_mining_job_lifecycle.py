"""Durable mining ownership tests, isolated from Flask, providers and real I/O."""

from __future__ import annotations

import copy
import importlib.util
import json
import socket
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Database:
    """A transactional SQL double; locks serialize competing in-process callers."""

    def __init__(self):
        self.now = datetime(2026, 10, 19, 12)
        self.jobs = {}
        self.runs = []
        self.sql = []
        self.lock = threading.RLock()
        self.commits = 0

    def job(self, job_id="job-1", **changes):
        row = dict(job_id=job_id, user_id=9, kind="factor_mining", status="queued",
                   request={"__userId": 999, "__jobId": "untrusted"}, execution_token="",
                   attempts=0, lease_expires_at=None, heartbeat_at=None,
                   created_at=self.now - timedelta(minutes=10), started_at=None,
                   finished_at=None, result=None, progress={})
        row.update(changes)
        self.jobs[job_id] = row
        return row

    def run(self, token="old", **changes):
        row = dict(id=len(self.runs) + 1, user_id=9, job_id="job-1", status="running",
                   execution_token=token, error_message="", proposals=["recorded"])
        row.update(changes)
        self.runs.append(row)
        return row

    @contextmanager
    def connect(self):
        connection = Connection(self)
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.release()


class Connection:
    def __init__(self, database):
        self.database = database
        self.locked = False
        self.snapshot = None

    def acquire(self):
        if not self.locked:
            self.database.lock.acquire()
            self.locked = True
            self.snapshot = copy.deepcopy((self.database.jobs, self.database.runs))

    def release(self):
        if self.locked:
            self.locked = False
            self.database.lock.release()

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.database.commits += 1
        self.release()

    def rollback(self):
        if self.snapshot is not None:
            jobs, runs = self.snapshot
            for key in list(self.database.jobs):
                if key not in jobs:
                    del self.database.jobs[key]
            for key, row in jobs.items():
                self.database.jobs.setdefault(key, {}).clear()
                self.database.jobs[key].update(row)
            for index, row in enumerate(runs):
                self.database.runs[index].clear()
                self.database.runs[index].update(row)
            del self.database.runs[len(runs):]
        self.release()


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.database = connection.database
        self.rowcount = 0
        self.rows = []

    def execute(self, query, parameters=()):
        sql = " ".join(query.lower().split())
        self.database.sql.append((sql, parameters))
        self.rowcount, self.rows = 0, []
        jobs = self.database.jobs
        if "pg_advisory_xact_lock" in sql:
            self.connection.acquire()
        elif sql.startswith("select") and "as db_now" in sql:
            self.rows = [{"db_now": self.database.now}]
        elif sql.startswith("select") and "count(*)" in sql:
            count = sum(row["user_id"] == parameters[0] and row["kind"] == "factor_mining"
                        and row["status"] in {"queued", "running"} for row in jobs.values())
            self.rows = [{"count": count, "active": count > 0}]
        elif sql.startswith("select") and "from qd_agent_jobs" in sql:
            if "for update" in sql:
                self.connection.acquire()
            if "where job_id =" in sql:
                row = jobs.get(parameters[0])
                if row and "and user_id = %s" in sql and row["user_id"] != parameters[1]:
                    row = None
                self.rows = [copy.deepcopy(row)] if row else []
            else:
                self.rows = [copy.deepcopy(row) for row in jobs.values()
                             if row["kind"] == "factor_mining"
                             and (len(parameters) == 2 or row["user_id"] == parameters[1])
                             and ((row["status"] == "queued"
                                   and row["created_at"] < self.database.now - timedelta(seconds=parameters[0]))
                                  or (row["status"] == "running" and
                                      (row["lease_expires_at"] is None
                                       or row["lease_expires_at"] <= self.database.now)))][:parameters[-1]]
        elif sql.startswith("insert into qd_agent_jobs"):
            job_id, user_id, _agent, kind, payload, _idem, created_at = parameters
            self.database.job(job_id, user_id=user_id, kind=kind, request=json.loads(payload),
                              created_at=created_at)
            self.rowcount = 1
        elif sql.startswith("update qd_factor_mining_runs"):
            status, error, job_id, user_id = parameters
            for row in self.database.runs:
                if row["job_id"] == job_id and row["user_id"] == user_id and row["status"] == "running":
                    row.update(status=status, error_message=error)
                    self.rowcount += 1
        elif sql.startswith("update qd_agent_jobs set status = 'running'"):
            token, seconds, job_id = parameters
            row = jobs[job_id]
            row.update(status="running", execution_token=token, attempts=row["attempts"] + 1,
                       lease_expires_at=self.database.now + timedelta(seconds=seconds),
                       heartbeat_at=self.database.now, started_at=self.database.now,
                       finished_at=None, result=None, error=None, progress={"phase": "running"})
            self.rowcount = 1
            self.rows = [copy.deepcopy(row)]
        elif sql.startswith("update qd_agent_jobs set lease_expires_at"):
            seconds, job_id, user_id, token = parameters
            row = jobs[job_id]
            if (row["status"] == "running" and row["execution_token"] == token
                    and row["user_id"] == user_id and row["lease_expires_at"] > self.database.now):
                row.update(lease_expires_at=self.database.now + timedelta(seconds=seconds),
                           heartbeat_at=self.database.now)
                self.rowcount = 1
        elif sql.startswith("update qd_agent_jobs set progress"):
            row = jobs[parameters[1]]
            row["progress"] = json.loads(parameters[0])
            self.rowcount = 1
        elif sql.startswith("update qd_agent_jobs set status = %s"):
            if len(parameters) == 2:
                status, job_id = parameters
                row = jobs[job_id]
                if "kind <> 'factor_mining'" not in sql or row["kind"] != "factor_mining":
                    row["status"] = status
                    self.rowcount = 1
            elif "started_at = %s" in sql:
                status, started, job_id = parameters
                row = jobs[job_id]
                if row["status"] == "queued" and row["kind"] != "factor_mining":
                    row.update(status=status, started_at=started)
                    self.rowcount = 1
            elif "execution_token = %s" in sql:
                status, result, error, progress, job_id, token = parameters
                row = jobs[job_id]
                if row["execution_token"] == token:
                    row.update(status=status, result=json.loads(result), error=error,
                               progress=json.loads(progress), finished_at=self.database.now,
                               lease_expires_at=None)
                    self.rowcount = 1
            else:
                status, result, error, payload, progress, job_id = parameters
                row = jobs[job_id]
                row.update(status=status, result=json.loads(result), error=error,
                           request=json.loads(payload), progress=json.loads(progress),
                           finished_at=self.database.now)
                self.rowcount = 1
        else:
            raise AssertionError(f"Unmodelled SQL: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Task:
    def __init__(self, function, options):
        self.options = options
        self.run = partial(function, self) if options.get("bind") else function
        self.apply_async = Mock()
        self.delay = Mock()
        self.retry = Mock(side_effect=RuntimeError("retry scheduled"))


@pytest.fixture
def env(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External I/O or Flask initialization is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for name, relative in [("app", "app"), ("app.utils", "app/utils"),
                           ("app.tasks", "app/tasks"), ("app.services", "app/services")]:
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT / relative)]
        monkeypatch.setitem(sys.modules, name, package)
    sys.modules["app"].create_app = forbidden
    database = Database()
    db = types.ModuleType("app.utils.db")
    db.get_db_connection = database.connect
    logger = types.ModuleType("app.utils.logger")
    logger.get_logger = lambda _name: Mock()
    billing = types.ModuleType("app.services.billing_service")
    billing.BillingError = RuntimeError
    billing.get_billing_service = forbidden
    celery = types.ModuleType("app.celery_app")
    celery.celery_app = types.SimpleNamespace(task=lambda **opts: lambda f: Task(f, opts))
    for module in [db, logger, billing, celery]:
        monkeypatch.setitem(sys.modules, module.__name__, module)

    def load(name):
        path = ROOT / (name.replace(".", "/") + ".py")
        assert path.exists(), f"Missing lifecycle implementation: {path.name}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        parent, _, attribute = name.rpartition(".")
        setattr(sys.modules[parent], attribute, module)
        return module

    jobs = load("app.utils.agent_jobs")
    if (ROOT / "app/utils/factor_mining_jobs.py").exists():
        load("app.utils.factor_mining_jobs")
    tasks = load("app.tasks.agent_jobs")
    monkeypatch.setenv("CELERY_TASKS_ENABLED", "true")
    return types.SimpleNamespace(database=database, jobs=jobs, tasks=tasks, load=load)


def test_submit_serializes_the_cap_with_insertion(env):
    def submit(index):
        try:
            return env.jobs.submit_job(user_id=9, agent_token_id=None, kind="factor_mining",
                                       request_payload={}, runner=lambda _p: {}, job_id=f"job-{index}")
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert len(env.database.jobs) == 1
    assert results.count("factorMining.concurrentLimit") == 1
    statements = [sql for sql, _parameters in env.database.sql]
    assert "pg_advisory_xact_lock" in statements[0]
    assert "count(*)" in statements[1]
    assert statements[2].startswith("insert")
    env.tasks.execute_agent_job.apply_async.assert_called_once()


def test_cancellation_closes_all_owned_running_runs(env):
    env.database.job(status="running", execution_token="owner")
    mine = env.database.run()
    foreign = env.database.run(user_id=10)
    succeeded = env.database.run(status="success")
    env.jobs.cancel_job("job-1", user_id=9)
    assert mine["status"] == "cancelled"
    assert foreign["status"] == "running"
    assert succeeded["status"] == "success"
    assert mine["proposals"] == ["recorded"]
    assert env.database.commits == 2  # one terminal transaction and one progress snapshot


def test_claim_is_exclusive_and_returns_authoritative_payload(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    first = lifecycle.claim_job("job-1")
    assert first["user_id"] == 9
    assert first["request"]["__userId"] == 999
    assert first["token"] and first["attempts"] == 1
    with pytest.raises(lifecycle.JobLeaseBusy) as error:
        lifecycle.claim_job("job-1")
    assert error.value.retry_after > 0
    assert env.database.jobs["job-1"]["execution_token"] == first["token"]


def test_expired_claim_fences_old_attempt_and_preserves_audit(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job(status="running", execution_token="old", attempts=1,
                     lease_expires_at=env.database.now - timedelta(seconds=1))
    old = env.database.run()
    claim = lifecycle.claim_job("job-1")
    assert claim["token"] != "old" and claim["attempts"] == 2
    assert old["status"] == "failed" and old["proposals"] == ["recorded"]
    assert env.database.commits == 1
    with pytest.raises(lifecycle.JobExecutionLost):
        with env.database.connect() as db:
            lifecycle.assert_execution(db.cursor(), job_id="job-1", user_id=9, token="old")


@pytest.mark.parametrize("change,exception", [
    ({"status": "cancelled"}, "JobCancelledError"),
    ({"execution_token": "new-owner"}, "JobExecutionLost"),
    ({"user_id": 10}, "JobExecutionLost"),
    ({"expired": True}, "JobExecutionLost"),
])
def test_assert_execution_rejects_cancellation_foreign_owner_and_expiry(env, change, exception):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    expired = change.pop("expired", False)
    env.database.job(status="running", execution_token="owner",
                     lease_expires_at=env.database.now + timedelta(seconds=-1 if expired else 90))
    env.database.jobs["job-1"].update(change)
    exception_type = getattr(env.jobs if exception == "JobCancelledError" else lifecycle, exception)
    with pytest.raises(exception_type):
        with env.database.connect() as db:
            lifecycle.assert_execution(db.cursor(), job_id="job-1", user_id=9, token="owner")
    assert "for update" in env.database.sql[0][0]


def test_renewal_extends_only_a_live_matching_lease(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    claim = lifecycle.claim_job("job-1")
    initial = env.database.jobs["job-1"]["lease_expires_at"]
    env.database.now += timedelta(seconds=10)
    assert lifecycle.renew_lease("job-1", user_id=9, token=claim["token"])
    assert env.database.jobs["job-1"]["lease_expires_at"] > initial
    with pytest.raises(lifecycle.JobExecutionLost):
        lifecycle.renew_lease("job-1", user_id=9, token="old")
    env.database.now += timedelta(hours=1)
    with pytest.raises(lifecycle.JobExecutionLost):
        lifecycle.renew_lease("job-1", user_id=9, token=claim["token"])


def test_finish_never_closes_a_new_owner_or_an_expired_same_token(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    claim = lifecycle.claim_job("job-1")
    run = env.database.run(token=claim["token"])
    assert not lifecycle.finish_job("job-1", user_id=9, token="old", error="late")
    assert run["status"] == "running"
    env.database.now += timedelta(hours=1)
    assert not lifecycle.finish_job("job-1", user_id=9, token=claim["token"], error="late")
    assert run["status"] == "running"
    replacement = lifecycle.claim_job("job-1")
    current = env.database.run(token=replacement["token"])
    assert lifecycle.finish_job("job-1", user_id=9, token=replacement["token"], error="engine failed")
    assert current["status"] == "failed"
    assert env.database.jobs["job-1"]["status"] == "failed"


def test_old_progress_is_rejected_before_memory_or_disk_write(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    lifecycle.claim_job("job-1")
    with pytest.raises(lifecycle.JobExecutionLost):
        lifecycle.publish_progress("job-1", user_id=9, token="old", event={"phase": "search"})
    assert env.database.jobs["job-1"]["progress"] == {"phase": "running"}
    assert not env.jobs._progress_buffers


def test_run_uses_authoritative_identity_and_leaves_published_success_intact(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    observed = []

    def runner(payload, on_progress):
        observed.append(payload)
        on_progress({"phase": "search"})
        env.database.jobs["job-1"].update(status="succeeded", result={"runId": 7})
        return {"runId": 7}

    lifecycle.run_job("job-1", runner)
    assert observed[0]["__userId"] == 9 and observed[0]["__jobId"] == "job-1"
    assert observed[0]["__executionToken"] == env.database.jobs["job-1"]["execution_token"]
    assert env.database.jobs["job-1"]["result"] == {"runId": 7}
    assert not any(sql.startswith("update qd_agent_jobs set status = %s") for sql, _ in env.database.sql)


def test_runner_return_cannot_publish_success_outside_storage_transaction(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    with pytest.raises(RuntimeError, match="FACTOR_MINING_RESULT_NOT_PUBLISHED"):
        lifecycle.run_job("job-1", lambda _payload: {"runId": 7})
    assert env.database.jobs["job-1"]["status"] == "failed"


def test_celery_busy_delivery_uses_controlled_retry_on_mining_queue(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    lifecycle.claim_job("job-1")
    execute = Mock()
    monkeypatch.setattr(env.tasks, "_execute", execute)
    with pytest.raises(RuntimeError, match="retry scheduled"):
        env.tasks.execute_agent_job.run("job-1")
    execute.assert_not_called()
    options = env.tasks.execute_agent_job.retry.call_args.kwargs
    assert options["countdown"] > 0 and options["queue"] == "mining"
    assert env.tasks.execute_agent_job.options["acks_late"]
    assert env.tasks.execute_agent_job.options["reject_on_worker_lost"]
    assert env.tasks.execute_agent_job.options["max_retries"] is None


def test_recovery_redispatches_only_lost_queued_or_expired_mining(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job("queued")
    env.database.job("expired", status="running", lease_expires_at=env.database.now - timedelta(seconds=1))
    env.database.job("live", status="running", lease_expires_at=env.database.now + timedelta(seconds=90))
    env.database.job("done", status="succeeded")
    env.database.job("other", kind="backtest")
    assert lifecycle.recover_jobs(limit=2) == 2
    assert [call.kwargs["args"][0] for call in env.tasks.execute_agent_job.apply_async.call_args_list] == ["queued", "expired"]
    assert all(call.kwargs["queue"] == "mining" for call in env.tasks.execute_agent_job.apply_async.call_args_list)


def test_maintenance_keeps_billing_expiry_and_recovers_mining(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    monkeypatch.setattr(env.jobs, "expire_billed_jobs", lambda: 2)
    recover = Mock(return_value=3)
    monkeypatch.setattr(lifecycle, "recover_jobs", recover)
    assert env.tasks.expire_agent_jobs.run() == 5
    recover.assert_called_once()


def test_generic_status_writers_cannot_bypass_mining_fencing(env):
    env.database.job(status="running", execution_token="owner")
    assert not env.jobs._set_status("job-1", "failed")
    assert not env.jobs._set_result("job-1", {"runId": 8})
    assert not env.jobs._set_failure("job-1", "unscoped failure")
    assert env.database.jobs["job-1"]["status"] == "running"


@pytest.mark.parametrize("kind", ["backtest", "strategy_evolution"])
def test_other_job_kinds_keep_their_existing_celery_execution(env, monkeypatch, kind):
    env.database.job(kind=kind)
    execute = Mock(return_value={"runId": 12})
    monkeypatch.setattr(env.tasks, "_execute", execute)
    env.tasks.execute_agent_job.run("job-1")
    assert env.database.jobs["job-1"]["status"] == "succeeded"
    assert execute.call_args.args[0] == kind
    assert "__executionToken" not in execute.call_args.args[1]


def test_local_executor_uses_the_same_fenced_runner(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    executor = Mock()
    monkeypatch.setenv("CELERY_TASKS_ENABLED", "false")
    monkeypatch.setattr(env.jobs, "_get_executor", lambda: executor)
    execute = Mock()
    monkeypatch.setattr(lifecycle, "run_job", execute)
    runner = lambda _payload: {}
    env.jobs.submit_job(user_id=9, agent_token_id=None, kind="factor_mining",
                        request_payload={}, runner=runner, job_id="local")
    executor.submit.call_args.args[0]()
    execute.assert_called_once_with("local", runner)


def test_success_wins_a_late_cancel_without_touching_audit(env):
    env.database.job(status="succeeded", result={"runId": 8})
    run = env.database.run(status="success")
    job = env.jobs.cancel_job("job-1", user_id=9)
    assert job["status"] == "succeeded" and job["result"] == {"runId": 8}
    assert run["status"] == "success"


def test_double_claim_has_one_owner_under_concurrent_delivery(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()

    def claim(_index):
        try:
            return lifecycle.claim_job("job-1")
        except lifecycle.JobLeaseBusy:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert results.count("busy") == 1
    assert env.database.jobs["job-1"]["attempts"] == 1


def test_expiry_is_checked_after_waiting_for_the_job_lock(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job(status="running", execution_token="owner",
                     lease_expires_at=env.database.now + timedelta(seconds=1))
    original = lifecycle._locked_job

    def locked(cur, job_id):
        row = original(cur, job_id)
        env.database.now += timedelta(seconds=2)
        return row

    monkeypatch.setattr(lifecycle, "_locked_job", locked)
    with pytest.raises(lifecycle.JobExecutionLost):
        with env.database.connect() as db:
            lifecycle.assert_execution(db.cursor(), job_id="job-1", user_id=9, token="owner")


def test_heartbeat_runs_without_candidate_progress_and_closes_with_the_attempt(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    claim = lifecycle.claim_job("job-1")
    renewed = threading.Event()
    original = lifecycle.renew_lease

    def renew(*args, **kwargs):
        env.database.now += timedelta(seconds=10)
        result = original(*args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(lifecycle, "renew_lease", renew)
    lease = lifecycle.ExecutionLease(job_id="job-1", user_id=9, token=claim["token"])
    lease.interval = 0.01
    before = env.database.jobs["job-1"]["lease_expires_at"]
    lease.start()
    try:
        assert renewed.wait(1)
        lease.check()
        assert env.database.jobs["job-1"]["lease_expires_at"] > before
    finally:
        lease.close()
    assert not lease._thread.is_alive()


def test_worker_failure_closes_job_and_attempt_together(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()

    def runner(payload):
        env.database.run(token=payload["__executionToken"])
        raise ValueError("search failed")

    with pytest.raises(ValueError, match="search failed"):
        lifecycle.run_job("job-1", runner)
    assert env.database.jobs["job-1"]["status"] == "failed"
    assert env.database.runs[0]["status"] == "failed"


def test_cancellation_after_runner_returns_does_not_become_success(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()

    def runner(payload):
        env.database.run(token=payload["__executionToken"])
        env.jobs.cancel_job("job-1", user_id=9)
        return {"runId": 8}

    lifecycle.run_job("job-1", runner)
    assert env.database.jobs["job-1"]["status"] == "cancelled"
    assert env.database.runs[0]["status"] == "cancelled"


def test_outer_runner_never_replaces_an_atomic_terminal_snapshot(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    terminal = {"phase": "succeeded", "runId": 8, "publication": "atomic"}

    def runner(_payload):
        env.database.jobs["job-1"].update(status="succeeded", result={"runId": 8},
                                          progress=copy.deepcopy(terminal))
        return {"runId": 8}

    lifecycle.run_job("job-1", runner)
    assert env.database.jobs["job-1"]["progress"] == terminal


def test_late_old_failure_cannot_erase_new_owner_terminal_progress(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    terminal = {"phase": "failed", "error": "new owner failure"}

    def runner(_payload):
        env.database.jobs["job-1"].update(status="failed", execution_token="new-owner",
                                          progress=copy.deepcopy(terminal))
        raise ValueError("old owner failure")

    with pytest.raises(ValueError, match="old owner failure"):
        lifecycle.run_job("job-1", runner)
    assert env.database.jobs["job-1"]["progress"] == terminal


@pytest.mark.parametrize("operation", ["claim", "cancel", "finish"])
def test_job_update_failure_rolls_back_the_same_transactions_run_changes(env, monkeypatch, operation):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    expired = operation == "claim"
    env.database.job(status="running", execution_token="owner",
                     lease_expires_at=env.database.now + timedelta(seconds=-1 if expired else 90))
    run = env.database.run(token="owner")
    original = Cursor.execute

    def fail_job_update(self, query, parameters=()):
        if " ".join(query.lower().split()).startswith("update qd_agent_jobs set status"):
            raise RuntimeError("job write failed")
        return original(self, query, parameters)

    monkeypatch.setattr(Cursor, "execute", fail_job_update)
    with pytest.raises(RuntimeError, match="job write failed"):
        if operation == "claim":
            lifecycle.claim_job("job-1")
        elif operation == "cancel":
            env.jobs.cancel_job("job-1", user_id=9)
        else:
            lifecycle.finish_job("job-1", user_id=9, token="owner", error="failed")
    assert run["status"] == "running"
    assert env.database.jobs["job-1"]["execution_token"] == "owner"


def test_terminal_and_foreign_job_never_claim_or_cancel(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job(status="succeeded")
    assert lifecycle.claim_job("job-1") is None
    env.database.job("foreign", status="running", user_id=10)
    run = env.database.run(job_id="foreign", user_id=10)
    assert env.jobs.cancel_job("foreign", user_id=9) is None
    assert run["status"] == "running"


def test_recovery_preserves_eligible_rows_when_broker_is_unavailable(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    row = env.database.job()
    env.tasks.execute_agent_job.apply_async.side_effect = RuntimeError("broker unavailable")
    assert lifecycle.recover_jobs() == 0
    assert row["status"] == "queued"


def test_storage_interface_accepts_positional_arguments_and_job_execution_lost_name(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    claim = lifecycle.claim_job("job-1")
    with env.database.connect() as db:
        row = lifecycle.assert_execution(db.cursor(), "job-1", 9, claim["token"])
        assert row["execution_token"] == claim["token"]
    with pytest.raises(lifecycle.JobExecutionLost):
        with env.database.connect() as db:
            lifecycle.assert_execution(db.cursor(), "job-1", 9, "old")


def test_recovery_filters_the_requesting_tenant_before_its_limit(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job("foreign", user_id=10)
    env.database.job("owned", user_id=9)
    assert lifecycle.recover_jobs(user_id=9, limit=1) == 1
    env.tasks.execute_agent_job.apply_async.assert_called_once_with(args=["owned"], queue="mining")
    assert "and user_id = %s" in env.database.sql[0][0]


def test_recovery_without_celery_resubmits_the_real_mining_runner_locally(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job("orphan")
    monkeypatch.setenv("CELERY_TASKS_ENABLED", "false")
    executor = Mock()
    monkeypatch.setattr(env.jobs, "_get_executor", lambda: executor)
    service = types.ModuleType("app.services.factor_mining.service")
    service.run_mining_job = Mock()
    monkeypatch.setitem(sys.modules, service.__name__, service)
    assert lifecycle.recover_jobs(user_id=9) == 1
    executor.submit.assert_called_once_with(lifecycle.run_job, "orphan", service.run_mining_job)
    env.tasks.execute_agent_job.apply_async.assert_not_called()


def test_exhausted_expired_attempt_closes_job_and_runs_in_one_transaction(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    row = env.database.job(status="running", execution_token="last", attempts=3,
                           lease_expires_at=env.database.now - timedelta(seconds=1))
    run = env.database.run(token="last")
    assert lifecycle.claim_job("job-1") is None
    assert row["status"] == run["status"] == "failed"
    assert row["attempts"] == 3 and row["execution_token"] == "last"
    assert env.database.commits == 1
    assert run["proposals"] == ["recorded"]


def test_last_allowed_attempt_keeps_a_live_lease(env):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    row = env.database.job(status="running", execution_token="last", attempts=3,
                           lease_expires_at=env.database.now + timedelta(seconds=90))
    run = env.database.run(token="last")
    with pytest.raises(lifecycle.JobLeaseBusy):
        lifecycle.claim_job("job-1")
    assert row["status"] == run["status"] == "running"


@pytest.mark.parametrize("setting", ["garbage", "", "0", "-1", "99999999", "inf"])
def test_invalid_attempt_limit_settings_keep_the_finite_default(env, monkeypatch, setting):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    monkeypatch.setenv("FACTOR_MINING_JOB_MAX_ATTEMPTS", setting)
    row = env.database.job(attempts=2)
    claim = lifecycle.claim_job("job-1")
    assert claim["attempts"] == 3
    row["lease_expires_at"] = env.database.now - timedelta(seconds=1)
    assert lifecycle.claim_job("job-1") is None
    assert row["status"] == "failed"


def test_configured_attempt_limit_is_enforced_before_starting_more_work(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    monkeypatch.setenv("FACTOR_MINING_JOB_MAX_ATTEMPTS", "1")
    row = env.database.job(attempts=1)
    assert lifecycle.claim_job("job-1") is None
    assert row["status"] == "failed"


def test_local_recovery_does_not_import_celery_task_module(env, monkeypatch):
    lifecycle = env.load("app.utils.factor_mining_jobs")
    env.database.job()
    monkeypatch.setenv("CELERY_TASKS_ENABLED", "false")
    monkeypatch.setitem(sys.modules, "app.tasks.agent_jobs", None)
    service = types.ModuleType("app.services.factor_mining.service")
    service.run_mining_job = Mock()
    monkeypatch.setitem(sys.modules, service.__name__, service)
    executor = Mock()
    monkeypatch.setattr(env.jobs, "_get_executor", lambda: executor)
    assert lifecycle.recover_jobs(user_id=9) == 1
    executor.submit.assert_called_once()
