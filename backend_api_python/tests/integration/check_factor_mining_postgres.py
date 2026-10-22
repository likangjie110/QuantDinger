"""Opt-in factor mining integration checks against an isolated PostgreSQL schema.

Requires DATABASE_URL. The migrations under test (v1 then the v3 additions) are applied to a
uniquely named schema that also holds a stub ``qd_users`` table the migrations' foreign keys
need; the table names keep their ``qd_`` prefix, because only the schema they land in is the
test's. Application tables are never touched and the schema is dropped in ``finally``.

Run from ``backend_api_python/``:

    .venv/Scripts/python.exe tests/integration/check_factor_mining_postgres.py
"""
import hashlib
import importlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = (
    BACKEND_ROOT / "migrations" / "20261013_factor_mining.sql",
    BACKEND_ROOT / "migrations" / "20261018_factor_mining_v3.sql",
    BACKEND_ROOT / "migrations" / "20261019_factor_mining_research.sql",
    BACKEND_ROOT / "migrations" / "20261019_factor_mining_p1.sql",
)
# The repository modules import as ``app.*``, which only resolves with the backend root on the
# path. The other release checks assume a caller-provided PYTHONPATH instead.
sys.path.insert(0, str(BACKEND_ROOT))

TABLES = (
    "qd_factor_mining_runs",
    "qd_factor_candidates",
    "qd_factor_trials",
    "qd_factor_proposals",
    "qd_factor_mining_experiments",
    "qd_factor_mining_experiment_runs",
)
INDEXES = (
    "idx_factor_mining_runs_user_id",
    "idx_factor_candidates_user_id",
    "idx_factor_candidates_user_hash",
    "idx_factor_candidates_context_hash",
    "idx_factor_candidates_user_status",
    "idx_factor_trials_run_stage",
    "idx_factor_proposals_user_run",
    "idx_factor_proposals_user_hash",
    "idx_factor_mining_runs_user_job",
    "idx_factor_mining_experiment_runs_group",
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
    migration = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS)
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

        def rollback(self):
            self.native.rollback()

    @contextmanager
    def connect():
        native = psycopg2.connect(dsn)
        try:
            with native.cursor() as cur:
                cur.execute("SET TIME ZONE 'UTC'")
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

    def run(user_id, budget=25, job_id=""):
        """One run row through the repository, and its id."""
        return repository.persist_run(
            user_id=user_id,
            config={"market": "USStock", "timeframe": "1D", "budget": budget},
            source_id=7, source_name="factor mining check", snapshot_id="",
            manifest={"name": "check"}, job_id=job_id,
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
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cur.execute("CREATE TABLE qd_users(id BIGINT PRIMARY KEY)")
            cur.execute("INSERT INTO qd_users(id) VALUES (1), (2)")
            cur.execute("""CREATE TABLE qd_agent_jobs (
                id BIGSERIAL PRIMARY KEY, job_id VARCHAR(40) NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES qd_users(id), agent_token_id INTEGER,
                kind VARCHAR(40) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'queued',
                request JSONB NOT NULL DEFAULT '{}', result JSONB, error TEXT, progress JSONB,
                idempotency_key VARCHAR(120), created_at TIMESTAMP DEFAULT NOW(),
                started_at TIMESTAMP, finished_at TIMESTAMP)""")
            cur.execute(migration)

        missing = [table for table in TABLES
                   if scalar("SELECT to_regclass(%s)::text", (table,)) is None]
        assert not missing, f"missing tables: {missing}"
        present = {row["indexname"] for row in query(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schema,))}
        missing = [index for index in INDEXES if index not in present]
        assert not missing, f"missing indexes: {missing}"
        assert scalar(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'qd_factor_mining_runs' "
            "AND column_name = 'job_id'", (schema,)) == 1, \
            "qd_factor_mining_runs.job_id is missing"
        print("    indexes in schema: " + ", ".join(sorted(present)))
        ok("schema_tables_and_indexes")

        # The ledger is written through the repository's own INSERT ... SELECT, so a trial's owner
        # comes from the run it belongs to rather than from the argument.
        job_id = "check-" + uuid4().hex
        run_id = run(1, job_id=job_id)

        # The v3 additions are read and written through the repository exactly as the service
        # does: a proposal row round-trips, and the job id on the run is what maps a job to it.
        proposal = {
            "round": 1, "ordinal": 1, "expression": {"op": "close"},
            "fingerprint": fingerprint, "hypothesis": "check", "model": "m", "provider": "p",
            "promptHash": "a" * 64, "dslVersion": 1, "source": "llm",
            "status": "proposed", "reason": "",
        }
        assert repository.save_proposals(run_id, user_id=1, proposals=[proposal]) == 1, \
            "save_proposals did not attempt one row"
        rows = repository.list_proposals(run_id, user_id=1)
        assert len(rows) == 1, f"list_proposals read {len(rows)} rows"
        assert rows[0]["expression"] == {"op": "close"}, "the expression did not round-trip"
        assert repository.get_run_by_job_id(job_id, user_id=1)["id"] == run_id, \
            "the job id does not map back to its run"
        assert repository.get_run_by_job_id(job_id, user_id=2) is None, \
            "a job id crossed tenants"
        ok("proposal_rows_and_job_id_round_trip")
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
        assert scalar("SELECT COUNT(*) FROM qd_factor_proposals") == 1, \
            "re-apply changed the proposals"
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

        context = {"version": 1, "sourceId": 7, "market": "USStock", "timeframe": "1D",
                   "startDate": "2024-01-01", "endDate": "2024-10-31",
                   "freezeRatio": .3, "holdoutRatio": .1}
        repeated_run = run(1)

        def register(run_id, user_id=1, count=5, research=context):
            return repository.register_experiment_trials(
                run_id, user_id=user_id, context=research, trials_count=count)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(register, [repeated_run] * 8))
        assert {result["cumulativeTrials"] for result in results} == {5}
        assert scalar("SELECT COUNT(*) FROM qd_factor_mining_experiment_runs") == 1
        ok("concurrent_same_run_is_counted_once")
        concurrent_runs = [run(1) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda run_id: register(run_id, count=2), concurrent_runs))
        assert register(repeated_run)["cumulativeTrials"] == 13
        assert register(repeated_run)["runTrials"] == 5
        other = register(run(2), user_id=2)
        assert other["cumulativeTrials"] == 5
        assert other["experimentId"] != results[0]["experimentId"]
        try:
            register(repeated_run, user_id=2)
            raise AssertionError("foreign run registration was accepted")
        except storage.FactorMiningWriteError as exc:
            assert str(exc) == storage.RUN_NOT_FOUND
        ok("concurrent_runs_accumulate_without_crossing_tenants")

        groups_before = scalar("SELECT COUNT(*) FROM qd_factor_mining_experiments")
        try:
            register(repeated_run, research={**context, "sourceId": 99})
            raise AssertionError("a registered run changed research groups")
        except storage.FactorMiningWriteError:
            pass
        assert scalar("SELECT COUNT(*) FROM qd_factor_mining_experiments") == groups_before
        ok("failed_registration_rolls_back_new_group")

        failed_run = run(1)

        class FailingCursor(cursor_type):
            def execute(self, statement, args=None):
                if "UPDATE qd_factor_mining_experiments" in statement:
                    raise RuntimeError("injected research update failure")
                return super().execute(statement, args)

        @contextmanager
        def fail_connect():
            with connect() as db:
                db.cursor = lambda: FailingCursor(db.native.cursor(cursor_factory=RealDictCursor))
                yield db

        storage.get_db_connection = fail_connect
        try:
            register(failed_run)
            raise AssertionError("the injected failure did not fire")
        except RuntimeError as exc:
            assert str(exc) == "injected research update failure"
        finally:
            storage.get_db_connection = connect
        assert scalar("SELECT COUNT(*) FROM qd_factor_mining_experiment_runs WHERE run_id = %s",
                      (failed_run,)) == 0
        assert register(repeated_run)["cumulativeTrials"] == 13
        ok("contribution_and_counter_roll_back_together")

        feedback_config = {"market": "USStock", "timeframe": "1D", "source_id": 7,
                           "start_date": "2024-01-01", "end_date": "2024-10-31",
                           "holding_period": 5, "freeze_ratio": .3, "holdout_ratio": .1}
        universe = {"kind": "static", "instruments": [{"symbol": "AAPL"}]}
        feedback_run = repository.persist_run(
            user_id=1, config=feedback_config, source_id=7, source_name="feedback check",
            snapshot_id="", manifest={"universe": universe})
        failure = {**pooled, "status": "rejected", "reason": "factorMining.reject.weakOos",
                   "outOfSample": {"rankIc": -.01}}
        repository.finish_run(feedback_run, user_id=1, status="success", result={
            "trials": [trial("L2", "reject", "coverage")], "candidates": [failure]})
        repository.upsert_candidates(user_id=1, run_id=pooled_run, candidates=[pooled])
        from app.services.factor_mining.research import feedback_context as make_feedback_context

        feedback_context = make_feedback_context(feedback_config, {"universe": universe})
        assert feedback_context == {**context, "holdingPeriod": 5, "universe": universe}
        feedback = repository.failure_summary(user_id=1, market="USStock", timeframe="1D",
                                             exclude_run_id=0, context=feedback_context)
        assert feedback["runIds"] == [feedback_run]
        assert feedback["recentFailures"][0]["outOfSample"]["rankIc"] == -.01
        for key, value in (
            ("holdingPeriod", 10), ("sourceId", 9), ("universe", {"kind": "other"}),
            ("startDate", "2024-02-01"), ("endDate", "2024-09-30"),
            ("freezeRatio", .25), ("holdoutRatio", .2),
        ):
            isolated = repository.failure_summary(
                user_id=1, market="USStock", timeframe="1D", exclude_run_id=0,
                context={**feedback_context, key: value})
            assert isolated == {"runIds": [], "stageCounts": [], "recentFailures": []}, (key, isolated)
        ok("immutable_oos_feedback_matches_research_context")

        with admin.cursor() as cur:
            cur.execute(migration)
        assert register(repeated_run)["cumulativeTrials"] == 13
        ok("research_migration_reapply_preserves_counters")

        contexts = [("USStock", "1D"), ("Crypto", "1D"), ("USStock", "4H")]
        scoped_ids = []
        for market, timeframe in contexts:
            scoped_run = repository.persist_run(
                user_id=1, config={"market": market, "timeframe": timeframe},
                source_id=7, source_name="context isolation", snapshot_id="", manifest={})
            repository.upsert_candidates(user_id=1, run_id=scoped_run, candidates=entry)
            scoped_ids.append(scalar(
                "SELECT id FROM qd_factor_candidates WHERE user_id=1 AND market=%s "
                "AND timeframe=%s AND expression_hash=%s", (market, timeframe, fingerprint)))
        assert len(set(scoped_ids)) == 3
        saved_ids = set(scoped_ids)
        with admin.cursor() as cur:
            cur.execute(migration)
        assert {row["id"] for row in query("SELECT id FROM qd_factor_candidates WHERE id = ANY(%s)",
                                           (scoped_ids,))} == saved_ids
        ok("candidate_market_timeframe_identity_and_upgrade_preserve_ids")

        from app.utils import agent_jobs
        from app.utils import factor_mining_jobs as mining_jobs

        agent_jobs.get_db_connection = connect
        mining_jobs.get_db_connection = connect

        def job(user_id=1):
            job_id = "p1-" + uuid4().hex
            query("INSERT INTO qd_agent_jobs(job_id,user_id,kind,status,request) "
                  "VALUES(%s,%s,'factor_mining','queued','{}')", (job_id, user_id))
            return job_id

        def attempt(job_id, claim):
            owner = repository.for_execution(job_id=job_id, user_id=claim["user_id"], token=claim["token"])
            run_id = owner.persist_run(user_id=claim["user_id"],
                config={"market": "USStock", "timeframe": "1D"}, source_id=7,
                source_name="fenced check", snapshot_id="", manifest={}, job_id=job_id)
            return owner, run_id

        def result(run_id, hash_char="c"):
            return {"runId": run_id, "candidates": [{"expression": {"op": "close"},
                    "fingerprint": hash_char * 64, "status": "frozen", "metrics": {"rankIc": .03}}],
                    "summary": {"trials": 1, "candidates": 1, "frozen": 1, "snapshotId": "b" * 64}}

        replacement_job = job()
        old_claim = mining_jobs.claim_job(replacement_job)
        old_owner, old_run = attempt(replacement_job, old_claim)
        query("UPDATE qd_agent_jobs SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE job_id=%s",
              (replacement_job,))
        new_claim = mining_jobs.claim_job(replacement_job)
        assert old_claim["token"] != new_claim["token"]
        assert scalar("SELECT status FROM qd_factor_mining_runs WHERE id=%s", (old_run,)) == "failed"
        try:
            old_owner.save_trials(old_run, user_id=1, trials=[trial("L2", "pass", "")])
            raise AssertionError("stale worker wrote after replacement")
        except mining_jobs.JobExecutionLost:
            pass
        assert repository.count_trials(old_run, user_id=1) == 0
        live_owner, live_run = attempt(replacement_job, new_claim)
        live_result = result(live_run)
        live_owner.publish_run(live_run, user_id=1, result=live_result)
        assert scalar("SELECT status FROM qd_agent_jobs WHERE job_id=%s", (replacement_job,)) == "succeeded"
        assert scalar("SELECT status FROM qd_factor_mining_runs WHERE id=%s", (live_run,)) == "success"
        assert scalar("SELECT COUNT(*) FROM qd_factor_candidates WHERE run_id=%s", (live_run,)) == 1
        assert mining_jobs.claim_job(replacement_job) is None
        ok("expired_owner_takeover_fences_old_writes_and_publishes_once")

        cancelled_job = job()
        cancelled_claim = mining_jobs.claim_job(cancelled_job)
        cancelled_owner, cancelled_run = attempt(cancelled_job, cancelled_claim)
        agent_jobs.cancel_job(cancelled_job, user_id=1)
        try:
            cancelled_owner.publish_run(cancelled_run, user_id=1, result=result(cancelled_run, "d"))
            raise AssertionError("cancelled worker published candidates")
        except agent_jobs.JobCancelledError:
            pass
        assert scalar("SELECT status FROM qd_factor_mining_runs WHERE id=%s", (cancelled_run,)) == "cancelled"
        assert scalar("SELECT COUNT(*) FROM qd_factor_candidates WHERE run_id=%s", (cancelled_run,)) == 0
        assert scalar("SELECT status FROM qd_agent_jobs WHERE job_id=%s", (cancelled_job,)) == "cancelled"
        ok("cancel_before_publish_keeps_job_run_and_library_consistent")

        atomic_job = job()
        atomic_claim = mining_jobs.claim_job(atomic_job)
        atomic_owner, atomic_run = attempt(atomic_job, atomic_claim)
        entered = threading.Event()
        proceed = threading.Event()
        write_candidates = atomic_owner._write_candidates

        def pause_publish(cur, **kwargs):
            entered.set()
            assert proceed.wait(5), "publish test was not released"
            return write_candidates(cur, **kwargs)

        atomic_owner._write_candidates = pause_publish
        with ThreadPoolExecutor(max_workers=2) as executor:
            publish = executor.submit(atomic_owner.publish_run, atomic_run, user_id=1,
                                      result=result(atomic_run, "e"))
            assert entered.wait(5), "publication did not acquire its job lock"
            cancellation = executor.submit(agent_jobs.cancel_job, atomic_job, user_id=1)
            proceed.set()
            publish.result(timeout=10)
            assert cancellation.result(timeout=10)["status"] == "succeeded"
        assert scalar("SELECT status FROM qd_factor_mining_runs WHERE id=%s", (atomic_run,)) == "success"
        assert scalar("SELECT COUNT(*) FROM qd_factor_candidates WHERE run_id=%s", (atomic_run,)) == 1
        ok("publish_before_cancel_commits_all_success_and_cancel_cannot_overwrite")

        failed_job = job()
        failed_claim = mining_jobs.claim_job(failed_job)
        failed_owner, failed_attempt = attempt(failed_job, failed_claim)

        class CandidateFailure(cursor_type):
            def executemany(self, statement, rows):
                if "INSERT INTO qd_factor_candidates" in statement:
                    raise RuntimeError("injected candidate failure")
                return super().executemany(statement, rows)

        @contextmanager
        def candidate_failure_connect():
            with connect() as db:
                db.cursor = lambda: CandidateFailure(db.native.cursor(cursor_factory=RealDictCursor))
                yield db

        storage.get_db_connection = candidate_failure_connect
        try:
            failed_owner.publish_run(failed_attempt, user_id=1, result=result(failed_attempt, "f"))
            raise AssertionError("candidate failure was not raised")
        except RuntimeError as exc:
            assert str(exc) == "injected candidate failure"
        finally:
            storage.get_db_connection = connect
        assert scalar("SELECT status FROM qd_agent_jobs WHERE job_id=%s", (failed_job,)) == "running"
        assert scalar("SELECT status FROM qd_factor_mining_runs WHERE id=%s", (failed_attempt,)) == "running"
        assert scalar("SELECT COUNT(*) FROM qd_factor_candidates WHERE run_id=%s", (failed_attempt,)) == 0
        agent_jobs.cancel_job(failed_job, user_id=1)
        ok("failed_publication_rolls_back_candidates_run_and_job_together")

        executor_before = agent_jobs._get_executor
        celery_setting = os.environ.get("CELERY_TASKS_ENABLED")
        agent_jobs._get_executor = lambda: type("NoDispatch", (), {"submit": lambda *_args: None})()
        os.environ["CELERY_TASKS_ENABLED"] = "false"
        barrier = threading.Barrier(8)

        def submit_once(_index):
            barrier.wait(timeout=10)
            try:
                return agent_jobs.submit_job(user_id=2, agent_token_id=None, kind="factor_mining",
                    request_payload={"sourceId": 7}, runner=lambda payload: payload)
            except ValueError as exc:
                assert str(exc) == "factorMining.concurrentLimit"
                return None

        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                receipts = list(executor.map(submit_once, range(8)))
            accepted = [receipt for receipt in receipts if receipt is not None]
            assert len(accepted) == 1
            assert scalar("SELECT COUNT(*) FROM qd_agent_jobs WHERE user_id=2 "
                          "AND kind='factor_mining' AND status IN ('queued','running')") == 1
            agent_jobs.cancel_job(accepted[0]["job_id"], user_id=2)
        finally:
            agent_jobs._get_executor = executor_before
            if celery_setting is None:
                os.environ.pop("CELERY_TASKS_ENABLED", None)
            else:
                os.environ["CELERY_TASKS_ENABLED"] = celery_setting
        ok("eight_concurrent_submissions_accept_exactly_one_job")

        leased_job = job()
        leased_claim = mining_jobs.claim_job(leased_job)
        assert mining_jobs.renew_lease(leased_job, user_id=1, token=leased_claim["token"])
        with ThreadPoolExecutor(max_workers=2) as executor:
            def competing_claim(_index):
                try:
                    mining_jobs.claim_job(leased_job)
                    raise AssertionError("a live lease was replaced")
                except mining_jobs.JobLeaseBusy:
                    return True
            assert all(executor.map(competing_claim, range(2)))
        _, leased_run = attempt(leased_job, leased_claim)
        query("UPDATE qd_agent_jobs SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE job_id=%s",
              (leased_job,))
        new_owner = mining_jobs.claim_job(leased_job)
        try:
            mining_jobs.renew_lease(leased_job, user_id=1, token=leased_claim["token"])
            raise AssertionError("stale owner renewed a replaced lease")
        except mining_jobs.JobExecutionLost:
            pass
        assert scalar("SELECT execution_token FROM qd_agent_jobs WHERE job_id=%s", (leased_job,)) == new_owner["token"]
        assert scalar("SELECT status FROM qd_factor_mining_runs WHERE id=%s", (leased_run,)) == "failed"
        agent_jobs.cancel_job(leased_job, user_id=1)
        ok("live_lease_rejects_competitors_and_stale_owner_cannot_renew")

        replay_job = job()
        first_claim = mining_jobs.claim_job(replay_job)
        first_owner, first_run = attempt(replay_job, first_claim)
        recorded = {"round": 1, "ordinal": 1, "expression": {"op": "neg", "args": ["close"]},
                    "status": "proposed", "dslVersion": 1, "hypothesis": "retained"}
        first_owner.save_proposals(first_run, user_id=1, proposals=[recorded])
        query("UPDATE qd_agent_jobs SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE job_id=%s",
              (replay_job,))
        second_claim = mining_jobs.claim_job(replay_job)
        _second_owner, second_run = attempt(replay_job, second_claim)
        query("UPDATE qd_agent_jobs SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE job_id=%s",
              (replay_job,))
        mining_jobs.claim_job(replay_job)
        assert repository.get_run_by_job_id(replay_job, user_id=1)["id"] == second_run
        assert repository.get_recovery_run(replay_job, user_id=1)["id"] == first_run
        assert repository.get_recovery_run(replay_job, user_id=2) is None
        assert repository.list_proposals(first_run, user_id=1)[0]["hypothesis"] == "retained"
        agent_jobs.cancel_job(replay_job, user_id=1)
        ok("two_crashes_recover_earlier_proposals_and_never_cross_tenants")

        print(json.dumps({"passed": passed, "count": len(passed)}))
        return 0
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


if __name__ == "__main__":
    sys.exit(main())
