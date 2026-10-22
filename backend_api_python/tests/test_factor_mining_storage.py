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
from app.services.factor_mining.research import feedback_context
from app.services.factor_mining.storage import FactorMiningRepository

# What the ownership precheck's ``SELECT 1`` returns when the run belongs to the caller.
_OWNED_RUN = {"?column?": 1}


class _RecordingCursor:
    def __init__(self, rows=()):
        self.statements = []
        self.lastrowid = 9  # what PostgresCursor exposes after an INSERT .. RETURNING id
        self.rowcount = 0
        self.rows = list(rows)

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def executemany(self, sql, params_list):
        self.statements.append((sql, [tuple(row) for row in params_list]))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

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
    assert "ON CONFLICT (user_id, market, timeframe, expression_hash) DO UPDATE" in sql
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
        user_id=4, run_id=9, status="frozen", market="USStock", timeframe="1d", limit=5, offset=10,
    )

    sql, params = cursor.statements[0]
    assert rows == []
    assert "user_id = ?" in sql and "run_id = ?" in sql
    assert "status = ?" in sql and "market = ?" in sql and "timeframe = ?" in sql
    assert params == (4, 9, "frozen", "USStock", "1d", 5, 10)


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


def test_save_proposals_writes_every_row_under_the_owned_run(monkeypatch):
    cur = _record(monkeypatch, rows=[_OWNED_RUN])
    repo = FactorMiningRepository()
    written = repo.save_proposals(9, user_id=4, proposals=[
        {"round": 1, "ordinal": 1, "expression": {"op": "close", "args": [], "params": {}},
         "fingerprint": "a" * 64, "hypothesis": "h", "model": "m", "provider": "p",
         "promptHash": "b" * 64, "dslVersion": 1, "source": "llm",
         "status": "proposed", "reason": ""},
    ])
    assert written == 1
    sql, params = cur.statements[-1]
    assert "INSERT INTO qd_factor_proposals" in sql
    assert "ON CONFLICT (run_id, round, ordinal) DO NOTHING" in sql
    assert params[0][1] == 9  # run_id
    assert params[0][2] == 1  # round
    assert params[0][3] == 1  # ordinal


def test_save_proposals_refuses_a_run_that_is_not_the_callers(monkeypatch):
    """A foreign run raises before anything is written -- never a silent zero-row proposal log."""
    cursor = _record(monkeypatch, rows=[None])

    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.runNotFound"):
        FactorMiningRepository().save_proposals(9, user_id=4, proposals=[
            {"round": 1, "ordinal": 1, "expression": {}, "fingerprint": "a" * 64},
        ])

    assert len(cursor.statements) == 1  # the ownership check, and no INSERT after it


def test_list_proposals_orders_by_round_and_ordinal_and_scopes_by_user(monkeypatch):
    cur = _record(monkeypatch)
    cur.rows = [{"id": 1, "user_id": 4, "run_id": 9, "round": 1, "ordinal": 2,
                 "expression_json": "{}", "expression_hash": "", "hypothesis": "",
                 "model": "", "provider": "", "prompt_hash": "", "dsl_version": 1,
                 "source": "llm", "status": "proposed", "reject_reason": "",
                 "created_at": None}]
    rows = FactorMiningRepository().list_proposals(9, user_id=4, limit=20, offset=0)
    sql, params = cur.statements[-1]
    assert "WHERE run_id = ? AND user_id = ?" in sql
    assert "ORDER BY round, ordinal" in sql
    assert params == (9, 4, 20, 0)
    assert rows[0]["ordinal"] == 2


def test_update_proposal_status_scopes_the_update_and_returns_the_row_count(monkeypatch):
    cursor = _record(monkeypatch, rows=[_OWNED_RUN])
    cursor.rowcount = 2

    updated = FactorMiningRepository().update_proposal_status(
        9, user_id=4, round_no=1, ordinals=[1, 2], status="evaluated")

    sql, params = cursor.statements[-1]
    assert updated == 2
    assert "SET status = ?" in sql
    assert "run_id = ? AND user_id = ? AND round = ?" in sql
    assert "ordinal IN (?, ?)" in sql
    assert params == ("evaluated", 9, 4, 1, 1, 2)


def test_update_proposal_status_skips_the_database_for_an_empty_ordinal_list(monkeypatch):
    def _forbidden():
        raise AssertionError("an empty update must not open a connection")

    monkeypatch.setattr(storage, "get_db_connection", _forbidden)

    assert FactorMiningRepository().update_proposal_status(
        9, user_id=4, round_no=1, ordinals=[], status="evaluated") == 0


def test_failure_summary_never_crosses_tenants(monkeypatch):
    cur = _record(monkeypatch)
    summary = FactorMiningRepository().failure_summary(
        user_id=4, market="USStock", timeframe="1d",
        exclude_run_id=9, history_runs=5, max_expressions=10)
    statements = [sql for sql, _params in cur.statements]
    assert all("qd_factor_candidates" not in sql for sql in statements)
    for sql, params in cur.statements:
        if "user_id" in sql:
            assert 4 in params

    # The run window is what makes the summary "recent successful runs in this market" rather
    # than every run the tenant ever had, so each clause is pinned with its bound value.
    run_sql, run_params = cur.statements[0]
    for fragment in ("status = 'success'", "market = ?", "timeframe = ?", "id <> ?", "LIMIT ?"):
        assert fragment in run_sql
    assert run_params == (4, "USStock", "1d", 9, 5)
    assert "result_json" in run_sql

    assert set(summary) == {"runIds", "stageCounts", "recentFailures"}
    assert summary == {"runIds": [], "stageCounts": [], "recentFailures": []}


def test_failure_summary_uses_frozen_run_results_and_bounds_oos_feedback(monkeypatch):
    candidate = {"expression": {"op": "neg", "args": ["close"]},
                 "fingerprint": "a" * 64, "status": "rejected",
                 "reason": "factorMining.reject.poolIncrement",
                 "metrics": {"rankIc": .04}, "outOfSample": {
                     "rankIc": .01, "poolIncrement": {"passed": False, "deltaIc": -.02},
                     "daily": list(range(1000)), "holdout": {"rankIc": .99}}}
    cur = _record(monkeypatch, rows=[{"id": 8, "result_json": json.dumps({
        "trials": [{"stage": "L2", "verdict": "reject", "reason": "coverage"}],
        "candidates": [candidate]})}])
    summary = FactorMiningRepository().failure_summary(
        user_id=4, market="USStock", timeframe="1D", exclude_run_id=9,
        context={"sourceId": 3, "startDate": "2024-01-01", "endDate": "2024-10-31",
                 "freezeRatio": .3, "holdoutRatio": .1, "holdingPeriod": 5,
                 "universe": {"kind": "static"}})
    assert summary["runIds"] == [8]
    assert any(row["stage"] == "L6" and row["count"] == 1 for row in summary["stageCounts"])
    assert summary["recentFailures"][0]["outOfSample"]["rankIc"] == .01
    assert "holdout" not in json.dumps(summary)
    assert "daily" not in json.dumps(summary)
    sql, params = cur.statements[0]
    assert "holding_period" in sql and "source_id" in sql and "universe" in sql
    assert 4 in params and "2024-01-01" in params and "2024-10-31" in params


@pytest.mark.parametrize("config_change, context_key, expected_value", [
    ({"start_date": "2024-02-01"}, "startDate", "2024-02-01"),
    ({"end_date": "2024-09-30"}, "endDate", "2024-09-30"),
    ({"freeze_ratio": .25}, "freezeRatio", .25),
    ({"holdout_ratio": .2}, "holdoutRatio", .2),
], ids=["start-date", "end-date", "freeze-ratio", "holdout-ratio"])
def test_failure_summary_binds_changed_research_window_and_split(
    monkeypatch, config_change, context_key, expected_value,
):
    """Pin the SQL contract; actual row exclusion belongs to the PostgreSQL check."""
    config = {"source_id": 3, "market": "USStock", "timeframe": "1D",
              "start_date": "2024-01-01", "end_date": "2024-10-31",
              "holding_period": 5, "freeze_ratio": .3, "holdout_ratio": .1}
    universe = {"kind": "static", "instruments": [{"symbol": "AAPL"}]}
    manifest = {"universe": universe}
    original_context = {"version": 1, "sourceId": 3, "market": "USStock", "timeframe": "1D",
                        "startDate": "2024-01-01", "endDate": "2024-10-31",
                        "holdingPeriod": 5, "freezeRatio": .3, "holdoutRatio": .1,
                        "universe": universe}
    assert feedback_context(config, manifest) == original_context
    expected_context = {**original_context, context_key: expected_value}
    requested_context = feedback_context({**config, **config_change}, manifest)
    assert requested_context == expected_context
    assert requested_context != original_context

    cur = _record(monkeypatch)
    summary = FactorMiningRepository().failure_summary(
        user_id=4, market="USStock", timeframe="1D", exclude_run_id=9,
        context=requested_context)

    assert len(cur.statements) == 1
    sql, params = cur.statements[0]
    for clause in (
        "AND LEFT(start_date, 10) = ?", "AND LEFT(end_date, 10) = ?",
        "AND COALESCE((config_json::jsonb ->> 'freeze_ratio')::numeric, 0.3) = ?",
        "AND COALESCE((config_json::jsonb ->> 'holdout_ratio')::numeric, 0) = ?",
    ):
        assert clause in sql
    assert params == (
        4, "USStock", "1D", 9, 3,
        expected_context["startDate"], expected_context["endDate"], 5,
        expected_context["freezeRatio"], expected_context["holdoutRatio"],
        json.dumps(universe, ensure_ascii=False), 5,
    )
    assert summary == {"runIds": [], "stageCounts": [], "recentFailures": []}


def test_register_experiment_trials_refuses_foreign_run(monkeypatch):
    _record(monkeypatch, rows=[None])
    with pytest.raises(storage.FactorMiningWriteError, match="factorMining.runNotFound"):
        FactorMiningRepository().register_experiment_trials(
            9, user_id=4, context={"sourceId": 3}, trials_count=5)


def test_register_experiment_trials_keeps_the_original_run_contribution(monkeypatch):
    cur = _record(monkeypatch, rows=[_OWNED_RUN, None,
        {"id": 2, "cumulative_trials": 25}, None,
        {"experiment_id": 2, "trials_count": 5}])
    result = FactorMiningRepository().register_experiment_trials(
        9, user_id=4, context={"sourceId": 3}, trials_count=5)
    assert result["runTrials"] == 5 and result["cumulativeTrials"] == 25
    assert result["experimentId"] == 2
    assert not any("UPDATE qd_factor_mining_experiments" in sql for sql, _ in cur.statements)
    assert any("FOR UPDATE" in sql for sql, _ in cur.statements)


def test_get_run_by_job_id_scopes_by_user(monkeypatch):
    cur = _record(monkeypatch)
    cur.rows = [{"id": 9, "user_id": 4, "job_id": "job-1", "config_json": "{}",
                 "manifest_json": "{}"}]
    run = FactorMiningRepository().get_run_by_job_id("job-1", user_id=4)
    sql, params = cur.statements[-1]
    assert "WHERE job_id = ? AND user_id = ?" in sql
    assert params == ("job-1", 4)
    assert run["id"] == 9


def test_persist_run_records_the_job_id(monkeypatch):
    cur = _record(monkeypatch)
    FactorMiningRepository().persist_run(
        user_id=4, config={"market": "USStock", "timeframe": "1d"},
        source_id=3, source_name="s", snapshot_id="", manifest={}, job_id="job-1")
    sql, params = cur.statements[-1]
    assert "job_id" in sql
    assert "job-1" in params
