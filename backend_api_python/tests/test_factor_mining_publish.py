"""The library, run and job become visible in one fenced transaction."""

import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.factor_mining import storage


def _database(monkeypatch, *, fail_candidates=False, refuse_execution=False):
    state = SimpleNamespace(statements=[], commits=0, rollbacks=0, guards=[])

    class Cursor:
        rowcount = 1
        lastrowid = 3

        def execute(self, sql, params=None):
            state.statements.append((sql, params))

        def executemany(self, sql, rows):
            if fail_candidates:
                raise RuntimeError("candidate write failed")
            state.statements.append((sql, list(rows)))

        def fetchone(self):
            return {"id": 3}

        def close(self):
            pass

    cursor = Cursor()

    def commit():
        state.commits += 1

    @contextmanager
    def connection():
        try:
            yield SimpleNamespace(cursor=lambda: cursor, commit=commit)
        except Exception:
            state.rollbacks += 1
            raise

    def assert_execution(cur, *, job_id, user_id, token):
        assert cur is cursor
        state.guards.append((job_id, user_id, token))
        if refuse_execution:
            raise RuntimeError("execution was replaced")

    monkeypatch.setattr(storage, "get_db_connection", connection)
    monkeypatch.setitem(sys.modules, "app.utils.factor_mining_jobs",
                        SimpleNamespace(assert_execution=assert_execution))
    return state


def _result():
    return {"runId": 3, "candidates": [{"expression": {"op": "close"},
            "fingerprint": "a" * 64, "status": "frozen", "metrics": {"rankIc": .1}}],
            "summary": {"trials": 8, "candidates": 1, "frozen": 1, "snapshotId": "b" * 64}}


def test_publish_run_commits_candidates_run_and_job_once(monkeypatch):
    state = _database(monkeypatch)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="owner")
    result = _result()
    repository.publish_run(3, user_id=4, result=result)
    assert state.guards == [("mine", 4, "owner")]
    assert state.commits == 1
    statements = [sql for sql, _ in state.statements]
    assert any("INSERT INTO qd_factor_candidates" in sql for sql in statements)
    assert any("UPDATE qd_factor_mining_runs" in sql for sql in statements)
    job = next((sql, params) for sql, params in state.statements
               if "UPDATE qd_agent_jobs" in sql)
    assert any(json.loads(value) == result for value in job[1]
               if isinstance(value, str) and value.startswith("{"))
    assert statements.index(next(sql for sql in statements if "qd_factor_candidates" in sql)) < \
        statements.index(job[0])


def test_replaced_execution_cannot_publish_any_rows(monkeypatch):
    state = _database(monkeypatch, refuse_execution=True)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="stale")
    with pytest.raises(RuntimeError, match="execution was replaced"):
        repository.publish_run(3, user_id=4, result=_result())
    assert state.statements == []
    assert state.commits == 0 and state.rollbacks == 1


def test_candidate_failure_cannot_commit_success_for_run_or_job(monkeypatch):
    state = _database(monkeypatch, fail_candidates=True)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="owner")
    with pytest.raises(RuntimeError, match="candidate write failed"):
        repository.publish_run(3, user_id=4, result=_result())
    assert state.commits == 0 and state.rollbacks == 1
    assert not any("UPDATE qd_agent_jobs" in sql for sql, _ in state.statements)


def test_every_bound_write_checks_current_execution(monkeypatch):
    state = _database(monkeypatch, refuse_execution=True)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="stale")
    with pytest.raises(RuntimeError, match="execution was replaced"):
        repository.save_trials(3, user_id=4, trials=[{"fingerprint": "a" * 64}])
    assert state.statements == [] and state.commits == 0


def test_bound_repository_refuses_another_user_before_opening_a_run(monkeypatch):
    state = _database(monkeypatch)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="owner")
    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.runNotFound"):
        repository.persist_run(user_id=5, config={}, source_id=3, source_name="", snapshot_id="",
                               manifest={}, job_id="mine")
    assert state.statements == [] and state.commits == 0


@pytest.mark.parametrize("status", ["running", "queued", "success", "invalid", ""])
def test_bound_repository_refuses_non_failure_terminal_states(monkeypatch, status):
    state = _database(monkeypatch)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="owner")
    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.invalidParameter"):
        repository.finish_run(3, user_id=4, status=status, result={})
    assert state.commits == 0


def test_bound_repository_cannot_open_a_successful_run(monkeypatch):
    state = _database(monkeypatch)
    repository = storage.FactorMiningRepository().for_execution(
        job_id="mine", user_id=4, token="owner")
    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.invalidParameter"):
        repository.persist_run(user_id=4, config={}, source_id=3, source_name="", snapshot_id="",
                               manifest={}, job_id="mine", status="success")
    assert state.statements == [] and state.commits == 0
