"""Opt-in factor mining integration checks against an isolated PostgreSQL schema.

Requires DATABASE_URL. The migration under test is applied to a uniquely named schema that also
holds a stub ``qd_users`` table the migration's foreign keys need; the table names keep their
``qd_`` prefix, because only the schema they land in is the test's. Application tables are never
touched and the schema is dropped in ``finally``.

Run from ``backend_api_python/``:

    .venv/Scripts/python.exe tests/integration/check_factor_mining_postgres.py
"""
import hashlib
import importlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "migrations" / "20261013_factor_mining.sql"
# The repository modules import as ``app.*``, which only resolves with the backend root on the
# path. The other release checks assume a caller-provided PYTHONPATH instead.
sys.path.insert(0, str(BACKEND_ROOT))

TABLES = ("qd_factor_mining_runs", "qd_factor_candidates", "qd_factor_trials")
INDEXES = (
    "idx_factor_mining_runs_user_id",
    "idx_factor_candidates_user_id",
    "idx_factor_candidates_user_hash",
    "idx_factor_candidates_user_status",
    "idx_factor_trials_run_stage",
)


def candidate(name):
    module = importlib.import_module(name)
    directory = os.environ.get("RELEASE_SOURCE_DIR")
    if directory:
        path = Path(directory) / (name.rsplit(".", 1)[-1] + ".py")
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def main():
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("BLOCKED: DATABASE_URL is not set, so there is no PostgreSQL instance to check "
              "against. Set it to a DSN whose user may CREATE SCHEMA, then re-run.")
        return 2

    storage = candidate("app.services.factor_mining.storage")
    cursor_type = candidate("app.utils.db_postgres").PostgresCursor
    migration = MIGRATION.read_text(encoding="utf-8")
    schema = "qd_factor_mining_test_" + uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    repository = storage.FactorMiningRepository()
    fingerprint = hashlib.sha256(b"close").hexdigest()
    passed = []

    class Connection:
        def __init__(self, native):
            self.native = native

        def cursor(self):
            return cursor_type(self.native.cursor(cursor_factory=RealDictCursor))

        def commit(self):
            self.native.commit()

    @contextmanager
    def connect():
        native = psycopg2.connect(dsn)
        try:
            with native.cursor() as cur:
                cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            yield Connection(native)
        finally:
            native.rollback()
            native.close()

    def query(statement, parameters=()):
        with admin.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(statement, parameters)
            return cur.fetchall() if cur.description else []

    def scalar(statement, parameters=()):
        rows = query(statement, parameters)
        return next(iter(rows[0].values())) if rows else None

    def ok(name):
        passed.append(name)
        print("OK  " + name)

    def run(user_id, budget=25):
        """One run row through the repository, and its id."""
        return repository.persist_run(
            user_id=user_id,
            config={"market": "USStock", "timeframe": "1D", "budget": budget},
            source_id=7, source_name="factor mining check", snapshot_id="",
            manifest={"name": "check"},
        )

    def trial(stage, verdict, reason):
        return {"fingerprint": fingerprint, "stage": stage, "verdict": verdict,
                "reason": reason, "metrics": {"rankIc": 0.001}}

    # The repository resolves this as a module global, so the patch goes on the module.
    storage.get_db_connection = connect
    entry = [{"expression": {"op": "close"}, "fingerprint": fingerprint, "origin": "template",
              "template_id": "momentum", "status": "search", "reason": "",
              "metrics": {}, "outOfSample": {}}]
    try:
        with admin.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cur.execute("CREATE TABLE qd_users(id BIGINT PRIMARY KEY)")
            cur.execute("INSERT INTO qd_users(id) VALUES (1), (2)")
            cur.execute(migration)

        missing = [table for table in TABLES
                   if scalar("SELECT to_regclass(%s)::text", (table,)) is None]
        assert not missing, f"missing tables: {missing}"
        present = {row["indexname"] for row in query(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schema,))}
        missing = [index for index in INDEXES if index not in present]
        assert not missing, f"missing indexes: {missing}"
        print("    indexes in schema: " + ", ".join(sorted(present)))
        ok("schema_tables_and_indexes")

        # The ledger is written through the repository's own INSERT ... SELECT, so a trial's owner
        # comes from the run it belongs to rather than from the argument.
        run_id = run(1)
        written = repository.save_trials(run_id, user_id=1, trials=[
            trial("L1", "reject", "factorMining.reject.invalid"),
            trial("L5", "pass", ""),
        ])
        assert written == 2, f"save_trials wrote {written} rows"
        assert repository.count_trials(run_id, user_id=1) == 2, "ledger count is not 2"
        ok("trial_ledger_counted")

        # ``finish_run`` lifts the three counts out of ``summary`` and gives each its own column,
        # and ``list_runs`` reads them back. Neither is covered anywhere else: the unit tests pin
        # the SQL text and the parameter positions, so swapping two of the three counts there
        # would pass every one of them and still persist the wrong numbers against a real ledger.
        repository.finish_run(run_id, user_id=1, status="success", result={"summary": {
            "trials": 2, "candidates": 1, "frozen": 1, "snapshotId": "d" * 64}})
        closed = query("SELECT status, trials_count, candidates_count, frozen_count, snapshot_id "
                       "FROM qd_factor_mining_runs WHERE id = %s", (run_id,))[0]
        assert closed["status"] == "success", f"finish_run wrote status {closed['status']}"
        assert closed["trials_count"] == 2, f"trials_count is {closed['trials_count']}"
        assert closed["candidates_count"] == 1, f"candidates_count is {closed['candidates_count']}"
        assert closed["frozen_count"] == 1, f"frozen_count is {closed['frozen_count']}"
        assert closed["snapshot_id"] == "d" * 64, "the fingerprint was not backfilled"
        runs = repository.list_runs(user_id=1)
        assert runs and runs[0]["id"] == run_id, "the owner's run list does not carry the run"
        assert runs[0]["trialsCount"] == 2, "list_runs and the table disagree about the ledger"
        assert runs[0]["frozenCount"] == 1, "list_runs and the table disagree about the review"
        assert "result" not in runs[0], "the run list dragged the stored payload along"
        assert repository.list_runs(user_id=2) == [], "cross-tenant run list leaked"
        ok("finish_run_writes_the_counts_and_list_runs_reads_them")

        # The migration runs again on every backend start, so re-applying it over live rows has to
        # be a no-op rather than a second create or a data loss.
        with admin.cursor() as cur:
            cur.execute(migration)
        assert repository.count_trials(run_id, user_id=1) == 2, "re-apply changed the ledger"
        assert scalar("SELECT COUNT(*) FROM qd_factor_mining_runs") == 1, "re-apply lost the run"
        ok("migration_reapply_is_idempotent")

        # One expression evaluated by two runs converges on one row: the library follows the run
        # that judged it last, and the row's own creation time is not rewritten by an update.
        later_run = run(1, budget=50)
        assert repository.upsert_candidates(user_id=1, run_id=run_id, candidates=entry) == 1
        created_at = scalar(
            "SELECT created_at FROM qd_factor_candidates WHERE user_id = 1 AND expression_hash = %s",
            (fingerprint,))
        candidate_id = scalar(
            "SELECT id FROM qd_factor_candidates WHERE user_id = 1 AND expression_hash = %s",
            (fingerprint,))
        entry[0].update(status="frozen")
        assert repository.upsert_candidates(user_id=1, run_id=later_run, candidates=entry) == 1
        assert scalar("SELECT COUNT(*) FROM qd_factor_candidates") == 1, "second write added a row"
        assert scalar("SELECT run_id FROM qd_factor_candidates WHERE id = %s",
                      (candidate_id,)) == later_run, "run_id did not follow the later run"
        assert scalar("SELECT status FROM qd_factor_candidates WHERE id = %s",
                      (candidate_id,)) == "frozen", "the update did not land"
        assert scalar("SELECT created_at FROM qd_factor_candidates WHERE id = %s",
                      (candidate_id,)) == created_at, "created_at was overwritten"
        ok("candidate_upsert_updates_in_place")

        # A trial is meaningless without the run that priced it, so the ledger goes with it.
        query("DELETE FROM qd_factor_mining_runs WHERE id = %s", (run_id,))
        assert scalar("SELECT COUNT(*) FROM qd_factor_trials WHERE run_id = %s", (run_id,)) == 0, \
            "trials outlived their run"
        assert scalar("SELECT COUNT(*) FROM qd_factor_trials") == 0, "another run's ledger changed"
        ok("run_delete_cascades_trials")

        # Counting and reading both scope through the owning run, so a foreign tenant reads
        # nothing rather than another tenant's numbers.
        repository.save_trials(later_run, user_id=1, trials=[trial("L2", "pass", "")])
        assert repository.count_trials(later_run, user_id=1) == 1, "the later run has no ledger"
        assert repository.count_trials(later_run, user_id=2) == 0, "cross-tenant count leaked"
        assert len(repository.list_candidates(user_id=1)) == 1, "the owner's library is empty"
        assert repository.list_candidates(user_id=2) == [], "cross-tenant list leaked"
        assert repository.get_candidate(candidate_id, user_id=2) is None, "cross-tenant read leaked"
        assert repository.get_run(later_run, user_id=2) is None, "cross-tenant run read leaked"
        ok("tenant_scope_blocks_cross_user_reads")

        # The library is not a child of a run: deleting the run that judged an expression clears
        # the pointer, because a candidate deduplicated across runs outlives any one of them.
        query("DELETE FROM qd_factor_mining_runs WHERE id = %s", (later_run,))
        assert scalar("SELECT COUNT(*) FROM qd_factor_candidates WHERE id = %s",
                      (candidate_id,)) == 1, "the library went with the run"
        assert scalar("SELECT run_id FROM qd_factor_candidates WHERE id = %s",
                      (candidate_id,)) is None, "a dead run_id was left behind"
        ok("candidate_outlives_its_run")

        # L6's reading is stored as JSON inside the candidate's out_of_sample_json, and its
        # ``complete`` flag and the two counts beside it have no consumer anywhere yet -- so
        # nothing but a round trip keeps them from being dropped between ``upsert_candidates``
        # and ``get_candidate``. A reader that cannot see "the pool was compared by half" reads
        # half a comparison as a clearance, which is the one reading the measurement forbids.
        pooled_run = run(1, budget=10)
        increment = {
            "poolSize": 3,
            "compared": ["pool:11111111", "pool:22222222"],
            "skipped": ["pool:33333333"],
            "complete": False,
            "hasIncrementalValue": None,
            "reason": "factorMining.poolIncrement.unmeasured",
            "residualIc": None,
            "rawIc": None,
            "usedCrossSections": 0,
            "skippedCrossSections": 0,
            "minAbsRankIc": 0.02,
        }
        # The column is only checked for a 64-character hex digest here, so the fingerprint of an
        # expression is as good a well-formed value as any other.
        pooled_hash = hashlib.sha256(b"ts_mean(close, 5)").hexdigest()
        pooled = dict(entry[0])
        pooled.update(
            expression={"op": "ts_mean", "args": ["close"], "params": {"n": 5}},
            fingerprint=pooled_hash, status="frozen", outOfSample={"poolIncrement": increment},
        )
        assert repository.upsert_candidates(user_id=1, run_id=pooled_run, candidates=[pooled]) == 1
        pooled_id = scalar("SELECT id FROM qd_factor_candidates WHERE expression_hash = %s",
                           (pooled_hash,))
        stored = repository.get_candidate(pooled_id, user_id=1)["outOfSample"]["poolIncrement"]
        assert stored == increment, f"the pool increment did not round-trip: {stored}"
        ok("pool_increment_round_trips")

        print(json.dumps({"passed": passed, "count": len(passed)}))
        return 0
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


if __name__ == "__main__":
    sys.exit(main())
