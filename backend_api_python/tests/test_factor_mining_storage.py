"""Factor mining persistence: row hydration, the write contracts, and user scoping.

Nothing here connects to a database. The hydration helpers are static, so every stored row shape
is exercised directly. The contracts a database would otherwise be needed to prove -- that the
trial ledger keeps rejected candidates, that candidates converge on their expression hash, that
``asdict(MiningConfig)``'s native datetimes are serialized -- are checked against a recording
connection instead: what matters is which statements and bound values reach PostgreSQL, not what
PostgreSQL then does with them. Scoping is asserted the same way, on the statement itself.
"""

import json
from contextlib import contextmanager
from datetime import datetime

import pytest

from app.services.factor_mining import storage
from app.services.factor_mining.storage import FactorMiningRepository

# What the ownership precheck's ``SELECT 1`` returns when the run belongs to the caller.
_OWNED_RUN = {"?column?": 1}


class _RecordingCursor:
    def __init__(self, rows=()):
        self.statements = []
        self.lastrowid = 9  # what PostgresCursor exposes after an INSERT .. RETURNING id
        self.rows = list(rows)

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def executemany(self, sql, params_list):
        self.statements.append((sql, [tuple(row) for row in params_list]))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return []

    def close(self):
        pass


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _record(monkeypatch, *, rows=()) -> _RecordingCursor:
    """Point the repository at a recording connection and return its cursor.

    ``rows`` is what each ``fetchone`` returns in turn, so a test that writes passes one
    ``_OWNED_RUN`` per write (the ownership check the repository runs first) and a test that must
    find nothing passes ``[None]``.
    """
    cursor = _RecordingCursor(rows=rows)
    connection = _RecordingConnection(cursor)

    @contextmanager
    def _connect():
        yield connection

    monkeypatch.setattr(storage, "get_db_connection", _connect)
    return cursor


def test_hydrate_candidate_parses_expression_and_metrics():
    row = {
        "id": 3,
        "expression_json": json.dumps({"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}),
        "expression_hash": "c" * 64,
        "origin": "template",
        "template_id": "momentum",
        "status": "frozen",
        "reject_reason": "",
        "market": "USStock",
        "timeframe": "1d",
        "metrics_json": json.dumps({"rankIc": 0.05, "icir": 1.1}),
        "out_of_sample_json": json.dumps({"rankIc": 0.03}),
    }

    summary = FactorMiningRepository._hydrate_candidate(dict(row), include_metrics=False)
    detail = FactorMiningRepository._hydrate_candidate(dict(row), include_metrics=True)

    assert summary["expressionHash"] == "c" * 64
    assert "metrics" not in summary
    assert detail["expression"]["op"] == "ts_pct_change"
    assert detail["metrics"]["rankIc"] == 0.05
    assert detail["outOfSample"]["rankIc"] == 0.03


def test_hydrate_candidate_tolerates_broken_json():
    row = {"id": 4, "expression_json": "{not json", "expression_hash": "d" * 64,
           "origin": "template", "template_id": "", "status": "rejected",
           "reject_reason": "factorMining.reject.weakIc", "metrics_json": "[]",
           "out_of_sample_json": "null", "market": "", "timeframe": ""}

    detail = FactorMiningRepository._hydrate_candidate(dict(row))

    assert detail["expression"] is None
    assert detail["metrics"] == {}
    assert detail["outOfSample"] == {}


def test_hydrate_run_summary_omits_result_payload():
    row = {"id": 9, "config_json": json.dumps({"market": "USStock"}), "status": "success",
           "trials_count": 250, "candidates_count": 12, "frozen_count": 3,
           "manifest_json": json.dumps({"snapshotId": "e" * 64}),
           "result_json": json.dumps({"summary": {"trials": 250}})}

    summary = FactorMiningRepository._hydrate_run(dict(row), include_result=False)
    detail = FactorMiningRepository._hydrate_run(dict(row), include_result=True)

    assert summary["trialsCount"] == 250
    assert "result" not in summary
    assert detail["result"]["summary"]["trials"] == 250


def test_hydrate_run_keeps_an_unreadable_result_out_of_the_summary():
    row = {"id": 10, "config_json": "{not json", "status": "failed", "trials_count": None,
           "candidates_count": 0, "frozen_count": 0, "manifest_json": "", "error_message": "boom"}

    summary = FactorMiningRepository._hydrate_run(dict(row))

    assert summary["config"] == {}
    assert summary["manifest"] == {}
    assert summary["trialsCount"] == 0
    assert summary["errorMessage"] == "boom"


def test_save_trials_counts_failures(monkeypatch):
    """The ledger's count is the deflated-Sharpe denominator, so nothing may be filtered out."""
    cursor = _record(monkeypatch, rows=[_OWNED_RUN])
    trials = [
        {"fingerprint": "a" * 64, "stage": "L2", "verdict": "reject",
         "reason": "factorMining.reject.weakIc", "metrics": {"rankIc": 0.001}},
        {"fingerprint": "b" * 64, "stage": "L4", "verdict": "pass", "reason": "",
         "metrics": {"rankIc": 0.05}},
        {"fingerprint": "c" * 64, "stage": "L1", "verdict": "reject",
         "reason": "factorMining.reject.invalid", "metrics": {}},
    ]

    written = FactorMiningRepository().save_trials(9, user_id=4, trials=trials)

    sql, rows = cursor.statements[-1]
    assert written == 3
    assert len(rows) == len(trials)
    assert sql.count("?") == len(rows[0])  # every placeholder is bound
    assert any("factorMining.reject.weakIc" in row for row in rows)
    assert any("factorMining.reject.invalid" in row for row in rows)


def test_save_trials_takes_the_owner_from_the_run_not_the_caller(monkeypatch):
    """A trial's ``user_id`` comes from the run it belongs to, inside the insert itself."""
    cursor = _record(monkeypatch, rows=[_OWNED_RUN])

    FactorMiningRepository().save_trials(
        9, user_id=4,
        trials=[{"fingerprint": "a" * 64, "stage": "L1", "verdict": "reject",
                 "reason": "factorMining.reject.invalid", "metrics": {}}],
    )

    check, insert = cursor.statements
    sql, rows = insert
    assert "SELECT ?, run.user_id, ?, ?, ?, ?, ?" in sql
    assert "WHERE run.id = ? AND run.user_id = ?" in sql
    assert rows[0][-2:] == (9, 4)
    assert "user_id" in check[0]  # the check that ran first was the ownership one


def test_save_trials_refuses_a_run_that_is_not_the_callers(monkeypatch):
    """A foreign run raises before anything is written -- never a silent zero-row ledger."""
    cursor = _record(monkeypatch, rows=[None])
    trials = [{"fingerprint": "a" * 64, "stage": "L2", "verdict": "reject",
               "reason": "factorMining.reject.weakIc", "metrics": {}}]

    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.runNotFound"):
        FactorMiningRepository().save_trials(9, user_id=4, trials=trials)

    assert len(cursor.statements) == 1  # the ownership check, and no INSERT after it


def test_save_trials_and_upsert_candidates_skip_the_database_when_nothing_was_evaluated(monkeypatch):
    def _forbidden():
        raise AssertionError("an empty batch must not open a connection")

    monkeypatch.setattr(storage, "get_db_connection", _forbidden)
    repository = FactorMiningRepository()

    assert repository.save_trials(9, user_id=4, trials=[]) == 0
    assert repository.upsert_candidates(user_id=4, run_id=9, candidates=[]) == 0


def test_upsert_candidates_converges_on_the_expression_hash(monkeypatch):
    cursor = _record(monkeypatch, rows=[_OWNED_RUN, _OWNED_RUN])  # one ownership check per call
    survivor = {"expression": {"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}},
                "fingerprint": "a" * 64, "origin": "template", "template_id": "momentum",
                "status": "search", "reason": "", "metrics": {"rankIc": 0.05}, "outOfSample": {}}
    repository = FactorMiningRepository()

    # A normal run writes the same expression twice: the search stage, then the frozen review.
    assert repository.upsert_candidates(user_id=4, run_id=9, candidates=[survivor]) == 1
    assert repository.upsert_candidates(
        user_id=4, run_id=9, candidates=[{**survivor, "status": "frozen",
                                          "outOfSample": {"rankIc": 0.03}}],
    ) == 1

    sql, rows = cursor.statements[1]
    assert "ON CONFLICT (user_id, expression_hash) DO UPDATE" in sql
    assert sql.count("?") == len(rows[0])  # every placeholder is bound
    assert len(rows) == 1
    assert "frozen" in cursor.statements[3][1][0]
    # The convergence is the whole SET list, not just the pointer: a conflict has to update the
    # verdict, its reason, and both metric payloads, or the survivor keeps its search-stage state.
    update = sql.split("DO UPDATE", 1)[1]
    for column in ("run_id", "status", "reject_reason", "metrics_json", "out_of_sample_json"):
        assert f"{column} = EXCLUDED.{column}" in update


def test_upsert_candidates_refuses_a_run_that_is_not_the_callers(monkeypatch):
    """A foreign run raises instead of returning a count for rows that were never written."""
    cursor = _record(monkeypatch, rows=[None])
    survivor = {"expression": {"op": "close"}, "fingerprint": "a" * 64, "origin": "template",
                "template_id": "momentum", "status": "search", "reason": "", "metrics": {},
                "outOfSample": {}}

    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.runNotFound"):
        FactorMiningRepository().upsert_candidates(user_id=4, run_id=9, candidates=[survivor])

    assert len(cursor.statements) == 1  # the ownership check, and no INSERT after it


def test_upsert_candidates_rejects_a_batch_without_fingerprints(monkeypatch):
    """An empty hash would fold every such candidate into the unique key's single row."""
    def _forbidden():
        raise AssertionError("an unidentifiable batch must not open a connection")

    monkeypatch.setattr(storage, "get_db_connection", _forbidden)
    candidate = {"expression": {"op": "close"}, "fingerprint": "", "origin": "template",
                 "template_id": "momentum", "status": "search", "reason": "", "metrics": {},
                 "outOfSample": {}}

    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.fingerprintInvalid"):
        FactorMiningRepository().upsert_candidates(user_id=4, run_id=9, candidates=[candidate])


def test_finish_run_lifts_the_summary_counts_and_scopes_the_update(monkeypatch):
    cursor = _record(monkeypatch)

    FactorMiningRepository().finish_run(
        9, user_id=4, status="success",
        result={"summary": {"trials": 250, "candidates": 12, "frozen": 3}},
    )

    sql, params = cursor.statements[0]
    assert params[3:6] == (250, 12, 3)  # trials / candidates / frozen, read as totals
    assert params[-2:] == (9, 4)
    assert "WHERE id = ? AND user_id = ?" in sql


def test_finish_run_backfills_the_snapshot_only_when_the_result_carries_one(monkeypatch):
    """``COALESCE(NULLIF(?, ''), snapshot_id)`` is what keeps a late empty result from erasing it.

    The statement is the only offline evidence there is: ``persist_run`` stores the fingerprint
    before the search and this call may arrive without one, so the empty case must bind ``''``
    (which ``NULLIF`` turns into "keep what is stored") and the populated case must bind the
    fingerprint through.
    """
    cursor = _record(monkeypatch)
    repository = FactorMiningRepository()

    repository.finish_run(9, user_id=4, status="success", result={"summary": {"trials": 1}})
    repository.finish_run(9, user_id=4, status="success",
                          result={"summary": {"trials": 1, "snapshotId": "e" * 64}})

    empty_sql, empty_params = cursor.statements[0]
    filled_sql, filled_params = cursor.statements[1]
    assert "snapshot_id = COALESCE(NULLIF(?, ''), snapshot_id)" in empty_sql
    assert empty_params[6] == ""
    assert filled_params[6] == "e" * 64
    assert empty_sql == filled_sql


def test_persist_run_serializes_config_datetimes_as_iso8601(monkeypatch):
    """``asdict(MiningConfig)`` holds native datetimes, so one boundary serializer converts them."""
    cursor = _record(monkeypatch)

    FactorMiningRepository().persist_run(
        user_id=4,
        config={"market": "USStock", "timeframe": "1d",
                "start_date": datetime(2024, 1, 1), "end_date": datetime(2024, 10, 31)},
        source_id=3,
        source_name="source",
        snapshot_id="",
        manifest={"apiVersion": 1},
    )

    sql, params = cursor.statements[0]
    stored = [json.loads(item) for item in params if isinstance(item, str) and item.startswith("{")]
    assert sql.count("?") == len(params)  # every placeholder is bound
    assert any(item.get("start_date") == "2024-01-01T00:00:00" for item in stored)
    assert any(item.get("end_date") == "2024-10-31T00:00:00" for item in stored)
    assert {"apiVersion": 1} in stored


def test_count_trials_counts_only_within_the_callers_own_run(monkeypatch):
    """The scope is in the statement: the count joins the run and filters on its owner.

    Offline evidence only -- that the statement carries the user scope. Whether another user's
    ``run_id`` really counts 0 is a database question, and belongs to the integration script.
    """
    cursor = _record(monkeypatch, rows=[{"total": 250}])

    assert FactorMiningRepository().count_trials(9, user_id=4) == 250

    sql, params = cursor.statements[0]
    assert params == (9, 4)
    assert "run.user_id = ?" in sql


def test_list_runs_scopes_to_the_user_and_skips_the_result_payload(monkeypatch):
    cursor = _record(monkeypatch)

    rows = FactorMiningRepository().list_runs(user_id=4, limit=5)

    sql, params = cursor.statements[0]
    assert rows == []
    assert params == (4, 5)
    assert "user_id = ?" in sql
    assert "result_json" not in sql  # the heavy payload belongs to the detail read only


def test_list_candidates_scopes_to_the_user_and_the_requested_filters(monkeypatch):
    cursor = _record(monkeypatch)

    rows = FactorMiningRepository().list_candidates(
        user_id=4, run_id=9, status="frozen", market="USStock", limit=5, offset=10,
    )

    sql, params = cursor.statements[0]
    assert rows == []
    assert "user_id = ?" in sql and "run_id = ?" in sql
    assert "status = ?" in sql and "market = ?" in sql
    assert params == (4, 9, "frozen", "USStock", 5, 10)


def test_single_row_reads_scope_by_user(monkeypatch):
    """Another user's row is not readable: the scope is in the statement, not in the caller."""
    cursor = _record(monkeypatch)
    repository = FactorMiningRepository()

    assert repository.get_run(9, user_id=4) is None
    assert repository.get_candidate(7, user_id=4) is None

    assert cursor.statements[0][1] == (9, 4)
    assert cursor.statements[1][1] == (7, 4)
    for sql, _params in cursor.statements:
        assert "WHERE id = ? AND user_id = ?" in sql
