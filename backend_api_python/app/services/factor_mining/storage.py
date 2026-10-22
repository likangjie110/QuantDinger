"""Persistence for factor mining runs, candidates, and the trial ledger.

The primary records have distinct lifetimes. A **run** is one job: its config, panel fingerprint
and final counts. A **candidate** converges by user, market, timeframe and expression fingerprint,
so re-evaluation in the same context updates its row and latest-run pointer. A **trial** does not
converge: one row per evaluation, rejected candidates included, because that count is the
deflated-Sharpe denominator the frozen review prices the search with, and a ledger missing its
failures would understate exactly the bias the correction exists for. A **proposal** is what an AI
run asked to evaluate, kept even when the evaluator never saw it, so a run can be audited and
replayed from the record rather than from a fresh model call.

A research group accumulates tuning attempts over the same source and split. Each run contributes
once through a transactional registration; group totals price the next frozen review. Historical
AI feedback reads completed run results, because the candidate library is a mutable current view.

Serialization happens here and nowhere else. The engine's record mixes timestamp styles by design
-- ``config`` is a ``MiningConfig`` dumped by ``asdict``, so it carries native ``datetime`` objects
under snake_case keys, while ``split`` is already ISO-8601 strings -- and both shapes are reviewed
contracts this layer will not rewrite. Instead one serializer makes any payload JSON-safe and
renders datetimes as ISO-8601, so every timestamp a stored run holds reads the same way.

Writes are scoped like reads, and audibly. A run that is not the caller's is refused with a closed
key rather than written as zero rows: a caller that believes it stored 250 trials while the ledger
got none would understate the deflated-Sharpe denominator silently, which is the one error this
layer exists to make impossible.

No clock, network, or panel access: every method takes and returns plain mappings, and the
``_hydrate*`` helpers are static so the row shapes are testable without a database.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from app.utils.db import get_db_connection
from app.services.factor_mining.research import context_hash


RUN_NOT_FOUND = "factorMining.runNotFound"
FINGERPRINT_INVALID = "factorMining.fingerprintInvalid"

# ``normalize_expression`` always yields a sha256 hex digest; anything else would join rows for
# different expressions inside the same market and timeframe.
_FINGERPRINT_LENGTH = 64


class FactorMiningWriteError(ValueError):
    """A write this repository refuses: the run is not the caller's, or a row has no identity.

    Subclasses ``ValueError`` because that is how this feature reports every closed i18n key, and
    because the HTTP layer already turns ``ValueError`` into ``code: 0`` carrying the key instead
    of a 500. The key is the message, exactly as elsewhere in ``factor_mining``.
    """


_RUN_COLUMNS = """
    id, user_id, source_id, source_name, market, timeframe, start_date, end_date,
    snapshot_id, status, trials_count, candidates_count, frozen_count, error_message,
    created_at, config_json, manifest_json
"""

_CANDIDATE_COLUMNS = """
    id, user_id, run_id, expression_json, expression_hash, origin, template_id, market,
    timeframe, status, reject_reason, metrics_json, out_of_sample_json, created_at, updated_at
"""

_PROPOSAL_COLUMNS = """
    id, user_id, run_id, round, ordinal, expression_json, expression_hash, hypothesis,
    model, provider, prompt_hash, dsl_version, source, status, reject_reason, created_at
"""

# Column -> the key callers read. The API speaks camelCase; the tables keep the house snake_case.
_RUN_KEYS = {
    "id": "id",
    "user_id": "userId",
    "source_id": "sourceId",
    "source_name": "sourceName",
    "market": "market",
    "timeframe": "timeframe",
    "start_date": "startDate",
    "end_date": "endDate",
    "snapshot_id": "snapshotId",
    "status": "status",
    "trials_count": "trialsCount",
    "candidates_count": "candidatesCount",
    "frozen_count": "frozenCount",
    "error_message": "errorMessage",
    "created_at": "createdAt",
}

_CANDIDATE_KEYS = {
    "id": "id",
    "user_id": "userId",
    "run_id": "runId",
    "expression_hash": "expressionHash",
    "origin": "origin",
    "template_id": "templateId",
    "market": "market",
    "timeframe": "timeframe",
    "status": "status",
    "reject_reason": "reason",
    "created_at": "createdAt",
    "updated_at": "updatedAt",
}

_PROPOSAL_KEYS = {
    "id": "id",
    "run_id": "runId",
    "round": "round",
    "ordinal": "ordinal",
    "expression_hash": "expressionHash",
    "hypothesis": "hypothesis",
    "model": "model",
    "provider": "provider",
    "prompt_hash": "promptHash",
    "dsl_version": "dslVersion",
    "source": "source",
    "status": "status",
    "reject_reason": "reason",
    "created_at": "createdAt",
}


class FactorMiningRepository:
    def __init__(self) -> None:
        self._execution: tuple[str, int, str] | None = None

    def for_execution(self, *, job_id: str, user_id: int, token: str) -> FactorMiningRepository:
        """Bind a repository copy to one worker claim without mutating the service singleton."""
        if not job_id or not token:
            raise FactorMiningWriteError("factorMining.workerInterrupted")
        repository = type(self)()
        repository._execution = (str(job_id), int(user_id), str(token))
        return repository

    def _execution_guard(self, cur: Any) -> None:
        if self._execution is not None:
            from app.utils.factor_mining_jobs import assert_execution

            job_id, user_id, token = self._execution
            assert_execution(cur, job_id=job_id, user_id=user_id, token=token)

    def _run_scope(self) -> tuple[str, tuple[Any, ...]]:
        if self._execution is None:
            return "", ()
        job_id, _user_id, token = self._execution
        return " AND job_id = ? AND execution_token = ?", (job_id, token)

    def _assert_execution_user(self, user_id: int, job_id: str | None = None) -> None:
        if self._execution is not None:
            owner_job, owner_user, _token = self._execution
            if int(user_id) != owner_user or (job_id is not None and job_id != owner_job):
                raise FactorMiningWriteError(RUN_NOT_FOUND)

    def _assert_run_owner(self, cur: Any, run_id: int, user_id: int) -> None:
        """Refuse a write aimed at a run that is missing or belongs to someone else.

        Checked before the write rather than inferred from it. An ``INSERT ... SELECT`` scoped by
        ownership would store nothing for a foreign run and still hand back a row count, and the
        caller records that count as the trial ledger's size.
        """
        self._assert_execution_user(user_id)
        self._execution_guard(cur)
        scope, values = self._run_scope()
        cur.execute(
            "SELECT 1 FROM qd_factor_mining_runs WHERE id = ? AND user_id = ?" + scope,
            (int(run_id), int(user_id), *values),
        )
        if cur.fetchone() is None:
            raise FactorMiningWriteError(RUN_NOT_FOUND)

    def persist_run(
        self,
        *,
        user_id: int,
        config: Mapping[str, Any],
        source_id: int,
        source_name: str,
        snapshot_id: str,
        manifest: Mapping[str, Any],
        status: str = "running",
        job_id: str = "",
    ) -> int:
        """Open a run row and return its id.

        ``config`` is the run's ``MiningConfig`` as a mapping -- the snake_case ``asdict`` shape
        ``engine.run`` returns -- which is where the denormalised market and window columns are
        read from. ``snapshot_id`` is usually empty here: the panel is prepared inside the search,
        after this row exists, so ``finish_run`` backfills the fingerprint from the result.
        ``job_id`` is the agent job that drives this run; it is what lets a proposal lookup work
        while the job is still running or after it failed.
        """
        self._assert_execution_user(user_id, job_id)
        if self._execution is not None and status != "running":
            raise FactorMiningWriteError("factorMining.invalidParameter")
        with get_db_connection() as db:
            cur = db.cursor()
            self._execution_guard(cur)
            cur.execute(
                """
                INSERT INTO qd_factor_mining_runs
                (user_id, source_id, source_name, market, timeframe, start_date, end_date,
                 snapshot_id, config_json, manifest_json, result_json, status, job_id, execution_token,
                 trials_count, candidates_count, frozen_count, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, 0, 0, 0, '', NOW())
                """,
                (
                    int(user_id), int(source_id), str(source_name or ""),
                    str(config.get("market") or ""), str(config.get("timeframe") or ""),
                    _window_text(config.get("start_date")), _window_text(config.get("end_date")),
                    str(snapshot_id or ""), _dump(dict(config)), _dump(dict(manifest)),
                    str(status or "running"), str(job_id or ""),
                    self._execution[2] if self._execution else "",
                ),
            )
            run_id = int(cur.lastrowid or 0)
            db.commit()
            cur.close()
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        user_id: int,
        status: str,
        result: Mapping[str, Any],
        error_message: str = "",
    ) -> None:
        """Close a run row with its status, payload, and the counts lifted out of the result.

        The counts come from ``summary``, where they are integer totals rather than lists, and
        ``frozen`` stays 0 for anything but the orchestration layer's final write: only the review
        knows how many survivors held up out of sample. The panel fingerprint is backfilled here
        because ``persist_run`` precedes the search; an empty one in the result leaves whatever the
        insert stored. The update is scoped to the caller's own run and simply matches nothing
        otherwise -- there is no payload a caller could lose by failing to close a foreign run.
        """
        if self._execution is not None and status not in {"failed", "cancelled"}:
            raise FactorMiningWriteError("factorMining.invalidParameter")
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                if self._execution is not None:
                    self._assert_run_owner(cur, run_id, user_id)
                self._write_run_result(cur, run_id, user_id, status, result, error_message)
                if self._execution is not None:
                    self._write_job_result(cur, "cancelled" if status == "cancelled" else "failed",
                                           result, error_message)
                db.commit()
            finally:
                cur.close()

    def _write_run_result(
        self, cur: Any, run_id: int, user_id: int, status: str,
        result: Mapping[str, Any], error_message: str = "",
    ) -> None:
        summary = result.get("summary") or {}
        scope, values = self._run_scope()
        cur.execute(
            """UPDATE qd_factor_mining_runs
               SET status = ?, error_message = ?, result_json = ?,
                   trials_count = ?, candidates_count = ?, frozen_count = ?,
                   snapshot_id = COALESCE(NULLIF(?, ''), snapshot_id)
               WHERE id = ? AND user_id = ?""" + scope,
            (str(status), str(error_message), _dump(dict(result)),
             _count(summary.get("trials")), _count(summary.get("candidates")),
             _count(summary.get("frozen")), str(summary.get("snapshotId") or ""),
             int(run_id), int(user_id), *values),
        )

    def _write_job_result(
        self, cur: Any, status: str, result: Mapping[str, Any], error_message: str = "",
    ) -> None:
        job_id, user_id, token = self._execution
        cur.execute(
            """UPDATE qd_agent_jobs
               SET status = ?, result = ?::jsonb, error = ?, progress = ?::jsonb,
                   finished_at = NOW(), lease_expires_at = NULL
               WHERE job_id = ? AND user_id = ? AND execution_token = ? AND status = 'running'""",
            (status, _dump(dict(result)), error_message or None, _dump({"phase": status}),
             job_id, user_id, token),
        )

    def publish_run(self, run_id: int, *, user_id: int, result: Mapping[str, Any]) -> None:
        """Publish final candidates and both successful timelines in one fenced transaction."""
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                self._assert_run_owner(cur, run_id, user_id)
                self._write_candidates(cur, user_id=user_id, run_id=run_id,
                                       candidates=result.get("candidates") or ())
                self._write_run_result(cur, run_id, user_id, "success", result)
                if self._execution is not None:
                    self._write_job_result(cur, "succeeded", result)
                db.commit()
            finally:
                cur.close()

    def save_trials(
        self,
        run_id: int,
        *,
        user_id: int,
        trials: Sequence[Mapping[str, Any]],
    ) -> int:
        """Append every evaluation to the run's ledger and return how many rows were written.

        Nothing is filtered by verdict: a rejected candidate is as much a part of the search as a
        survivor, and the frozen review's deflated-Sharpe benchmark is priced by the whole count.
        The return value is the length of this call's batch, which is the caller's ``trials_count``
        -- read from the batch rather than from the driver, whose batch execution reports only its
        last page, and honest because a foreign or missing run raises instead of storing nothing.

        The row's ``user_id`` is read from the owning run inside the statement, so a trial's owner
        is the run it belongs to rather than whatever the caller passed in.
        """
        rows = [
            (
                int(run_id), str(trial.get("fingerprint") or ""),
                str(trial.get("stage") or ""), str(trial.get("verdict") or ""),
                str(trial.get("reason") or ""), _dump(trial.get("metrics") or {}),
                int(run_id), int(user_id),
            )
            for trial in trials
        ]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                self._assert_run_owner(cur, run_id, user_id)
                cur.executemany(
                    """
                    INSERT INTO qd_factor_trials
                    (run_id, user_id, expression_hash, stage, verdict, reject_reason, metrics_json)
                    SELECT ?, run.user_id, ?, ?, ?, ?, ?
                    FROM qd_factor_mining_runs AS run
                    WHERE run.id = ? AND run.user_id = ?
                    """,
                    rows,
                )
                db.commit()
            finally:
                cur.close()
        return len(rows)

    def register_experiment_trials(
        self, run_id: int, *, user_id: int, context: Mapping[str, Any], trials_count: int,
    ) -> dict[str, Any]:
        """Count a run once, serializing concurrent registrations in the same research group."""
        if isinstance(trials_count, bool) or not isinstance(trials_count, int) or trials_count < 0:
            raise FactorMiningWriteError("factorMining.invalidParameter")
        self._assert_execution_user(user_id)
        fingerprint = context_hash(context)
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                # Lock the run before the group: a repeated run cannot change groups concurrently.
                self._execution_guard(cur)
                scope, values = self._run_scope()
                cur.execute("SELECT id FROM qd_factor_mining_runs WHERE id = ? AND user_id = ?" +
                            scope + " FOR UPDATE", (int(run_id), int(user_id), *values))
                if cur.fetchone() is None:
                    raise FactorMiningWriteError(RUN_NOT_FOUND)
                cur.execute("""
                    INSERT INTO qd_factor_mining_experiments (user_id, context_hash, context_json)
                    VALUES (?, ?, ?) ON CONFLICT (user_id, context_hash) DO NOTHING RETURNING id
                    """, (int(user_id), fingerprint, _dump(dict(context))))
                cur.fetchone()
                cur.execute("""
                    SELECT id, cumulative_trials FROM qd_factor_mining_experiments
                    WHERE user_id = ? AND context_hash = ? FOR UPDATE
                    """, (int(user_id), fingerprint))
                group = cur.fetchone()
                if group is None:
                    raise FactorMiningWriteError("factorMining.invalidParameter")
                experiment_id = int(group["id"])
                cumulative = int(group["cumulative_trials"])
                cur.execute("""
                    INSERT INTO qd_factor_mining_experiment_runs
                        (run_id, user_id, experiment_id, trials_count)
                    VALUES (?, ?, ?, ?) ON CONFLICT (run_id) DO NOTHING
                    RETURNING experiment_id, trials_count
                    """, (int(run_id), int(user_id), experiment_id, trials_count))
                contribution = cur.fetchone()
                if contribution is not None:
                    cur.execute("""
                        UPDATE qd_factor_mining_experiments
                        SET cumulative_trials = cumulative_trials + ?, updated_at = NOW()
                        WHERE id = ? AND user_id = ? RETURNING cumulative_trials
                        """, (trials_count, experiment_id, int(user_id)))
                    cumulative = int(cur.fetchone()["cumulative_trials"])
                else:
                    cur.execute("""
                        SELECT experiment_id, trials_count FROM qd_factor_mining_experiment_runs
                        WHERE run_id = ? AND user_id = ?
                        """, (int(run_id), int(user_id)))
                    contribution = cur.fetchone()
                    if contribution is None or int(contribution["experiment_id"]) != experiment_id:
                        raise FactorMiningWriteError("factorMining.invalidParameter")
                db.commit()
                return {"experimentId": experiment_id, "contextHash": fingerprint,
                        "runTrials": int(contribution["trials_count"]), "cumulativeTrials": cumulative}
            finally:
                cur.close()

    def upsert_candidates(
        self,
        *,
        user_id: int,
        run_id: int,
        candidates: Sequence[Mapping[str, Any]],
    ) -> int:
        """Write the candidate library and return how many rows were written.

        ``(user_id, market, timeframe, expression_hash)`` is the identity. Re-evaluating an
        expression in the same context updates its status and metrics and moves ``run_id`` to the
        run that judged it last. Normal mining publishes only after the frozen review; this helper
        remains available for explicit repository operations in the same transaction boundary.

        The market and timeframe are read from the owning run inside the statement, because a
        candidate record carries neither: it is the run that knows which market was searched. A
        foreign or missing run raises rather than returning a row count for rows that were never
        written, and a candidate with no fingerprint raises rather than being stored under ``''``
        -- the unique key would fold every such candidate into one row and lose the rest.
        """
        if not candidates:
            return 0
        self._candidate_rows(user_id, run_id, candidates)
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                self._assert_run_owner(cur, run_id, user_id)
                self._write_candidates(cur, user_id=user_id, run_id=run_id, candidates=candidates)
                db.commit()
            finally:
                cur.close()
        return len(candidates)

    @staticmethod
    def _candidate_rows(user_id: int, run_id: int, candidates: Sequence[Mapping[str, Any]]) -> list:
        for index, candidate in enumerate(candidates):
            if len(str(candidate.get("fingerprint") or "")) != _FINGERPRINT_LENGTH:
                raise FactorMiningWriteError(f"{FINGERPRINT_INVALID}:{index}")
        return [
            (int(user_id), int(run_id), _dump(candidate.get("expression") or {}),
             str(candidate.get("fingerprint") or ""), str(candidate.get("origin") or ""),
             str(candidate.get("template_id") or ""), str(candidate.get("status") or ""),
             str(candidate.get("reason") or ""), _dump(candidate.get("metrics") or {}),
             _dump(candidate.get("outOfSample") or {}), int(run_id))
            for candidate in candidates
        ]

    def _write_candidates(
        self, cur: Any, *, user_id: int, run_id: int, candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        rows = self._candidate_rows(user_id, run_id, candidates)
        if rows:
            cur.executemany(
                """
                    INSERT INTO qd_factor_candidates
                    (user_id, run_id, expression_json, expression_hash, origin, template_id,
                     market, timeframe, status, reject_reason, metrics_json, out_of_sample_json,
                     created_at, updated_at)
                    SELECT ?, ?, ?, ?, ?, ?, run.market, run.timeframe, ?, ?, ?, ?, NOW(), NOW()
                    FROM qd_factor_mining_runs AS run
                    WHERE run.id = ?
                    ON CONFLICT (user_id, market, timeframe, expression_hash) DO UPDATE
                    SET run_id = EXCLUDED.run_id,
                        expression_json = EXCLUDED.expression_json,
                        origin = EXCLUDED.origin,
                        template_id = EXCLUDED.template_id,
                        market = EXCLUDED.market,
                        timeframe = EXCLUDED.timeframe,
                        status = EXCLUDED.status,
                        reject_reason = EXCLUDED.reject_reason,
                        metrics_json = EXCLUDED.metrics_json,
                        out_of_sample_json = EXCLUDED.out_of_sample_json,
                        updated_at = NOW()
                """, rows,
            )

    def save_proposals(
        self,
        run_id: int,
        *,
        user_id: int,
        proposals: Sequence[Mapping[str, Any]],
    ) -> int:
        """Record an AI round's proposals and return how many rows were attempted.

        One row per generated proposal, including the ones that never reach the evaluator:
        ``l1_rejected`` and ``duplicate`` are outcomes worth auditing, and ``proposed`` marks a row
        the crash-recovery sweep can tell apart from one whose round result was written back.
        ``(run_id, round, ordinal)`` is the identity, so a retried batch inserts nothing twice.
        The return value is the batch length for the same reason ``save_trials`` reports it: the
        caller records it as what was generated, and a foreign run raises rather than silently
        storing zero rows.
        """
        rows = [
            (
                int(user_id), int(run_id), int(proposal.get("round") or 0),
                int(proposal.get("ordinal") or 0),
                _dump(proposal.get("expression") or {}),
                str(proposal.get("fingerprint") or ""),
                str(proposal.get("hypothesis") or ""),
                str(proposal.get("model") or ""),
                str(proposal.get("provider") or ""),
                str(proposal.get("promptHash") or ""),
                int(proposal.get("dslVersion") or 1),
                str(proposal.get("source") or "llm"),
                str(proposal.get("status") or "proposed"),
                str(proposal.get("reason") or ""),
            )
            for proposal in proposals
        ]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                self._assert_run_owner(cur, run_id, user_id)
                cur.executemany(
                    """
                    INSERT INTO qd_factor_proposals
                    (user_id, run_id, round, ordinal, expression_json, expression_hash,
                     hypothesis, model, provider, prompt_hash, dsl_version, source, status,
                     reject_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_id, round, ordinal) DO NOTHING
                    """,
                    rows,
                )
                db.commit()
            finally:
                cur.close()
        return len(rows)

    def update_proposal_status(
        self,
        run_id: int,
        *,
        user_id: int,
        round_no: int,
        ordinals: Sequence[int],
        status: str,
    ) -> int:
        """Move a round's proposals to their final state and return how many rows changed.

        Only ``proposed`` rows are written by ``save_proposals``; the round's evaluation results
        then mark the ones that reached the evaluator as ``evaluated``. Scoped by the run's owner
        in the statement itself, so a foreign run raises before anything is touched.
        """
        values = [int(ordinal) for ordinal in ordinals]
        if not values:
            return 0
        marks = ", ".join("?" for _ in values)
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                self._assert_run_owner(cur, run_id, user_id)
                cur.execute(
                    f"""
                    UPDATE qd_factor_proposals
                    SET status = ?
                    WHERE run_id = ? AND user_id = ? AND round = ? AND ordinal IN ({marks})
                    """,
                    (str(status or ""), int(run_id), int(user_id), int(round_no), *values),
                )
                updated = int(cur.rowcount)
                db.commit()
            finally:
                cur.close()
        return updated

    def count_trials(self, run_id: int, *, user_id: int) -> int:
        """The run's whole ledger: the denominator the out-of-sample review is priced by.

        Counted only within the caller's own run. The scope comes through the run rather than
        through the trial rows' own ``user_id``: a trial belongs to whoever owns the run that wrote
        it, so joining the owner is what makes a cross-tenant count impossible instead of merely
        unused.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM qd_factor_trials AS trial
                JOIN qd_factor_mining_runs AS run ON run.id = trial.run_id
                WHERE trial.run_id = ? AND run.user_id = ?
                """,
                (int(run_id), int(user_id)),
            )
            row = cur.fetchone() or {}
            cur.close()
        return _count(row.get("total"))

    def list_runs(self, *, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT {_RUN_COLUMNS}
                FROM qd_factor_mining_runs
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(user_id), int(limit)),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [self._hydrate_run(row) for row in rows]

    def get_run(self, run_id: int, *, user_id: int) -> dict[str, Any] | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT {_RUN_COLUMNS}, result_json
                FROM qd_factor_mining_runs
                WHERE id = ? AND user_id = ?
                """,
                (int(run_id), int(user_id)),
            )
            row = cur.fetchone()
            cur.close()
        return self._hydrate_run(row, include_result=True) if row else None

    def get_run_by_job_id(self, job_id: str, *, user_id: int) -> dict[str, Any] | None:
        """The newest run a job opened, hydrated like a summary read.

        A job maps to a run through the ``job_id`` column rather than through the job store, so the
        lookup works while the job is running and after it failed. The scope is the caller's: a
        job id belonging to another tenant reads as absent.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT {_RUN_COLUMNS}
                FROM qd_factor_mining_runs
                WHERE job_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(job_id or ""), int(user_id)),
            )
            row = cur.fetchone()
            cur.close()
        return self._hydrate_run(row) if row else None

    def get_recovery_run(self, job_id: str, *, user_id: int) -> dict[str, Any] | None:
        """Find recorded material even if a newer attempt died before copying proposals."""
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    f"""SELECT {_RUN_COLUMNS} FROM qd_factor_mining_runs
                        WHERE job_id = ? AND user_id = ? AND status = 'failed'
                          AND EXISTS (SELECT 1 FROM qd_factor_proposals AS proposal
                                      WHERE proposal.run_id = qd_factor_mining_runs.id
                                        AND proposal.user_id = qd_factor_mining_runs.user_id)
                        ORDER BY id DESC LIMIT 1""",
                    (str(job_id), int(user_id)),
                )
                row = cur.fetchone()
            finally:
                cur.close()
        return self._hydrate_run(row) if row else None

    def list_candidates(
        self,
        *,
        user_id: int,
        run_id: int | None = None,
        status: str = "",
        market: str = "",
        timeframe: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """The candidate library, newest first, filtered by the run, the state it ended in, or the
        market and timeframe it was searched over.

        Rows are hydrated without their metric payloads: a candidate's metrics carry its whole IC
        series and curves, which a page of fifty does not need. ``get_candidate`` returns them.
        """
        where = ["user_id = ?"]
        params: list[Any] = [int(user_id)]
        if run_id is not None:
            where.append("run_id = ?")
            params.append(int(run_id))
        if status:
            where.append("status = ?")
            params.append(str(status))
        if market:
            where.append("market = ?")
            params.append(str(market))
        if timeframe:
            where.append("timeframe = ?")
            params.append(str(timeframe))
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT {_CANDIDATE_COLUMNS}
                FROM qd_factor_candidates
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, int(limit), int(offset)),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [self._hydrate_candidate(row, include_metrics=False) for row in rows]

    def get_candidate(self, candidate_id: int, *, user_id: int) -> dict[str, Any] | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT {_CANDIDATE_COLUMNS}
                FROM qd_factor_candidates
                WHERE id = ? AND user_id = ?
                """,
                (int(candidate_id), int(user_id)),
            )
            row = cur.fetchone()
            cur.close()
        return self._hydrate_candidate(row) if row else None

    def list_proposals(
        self,
        run_id: int,
        *,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """A run's proposals in the order they were generated: ``(round, ordinal)``.

        The round/ordinal order is the record's chronology, and the replay source depends on it:
        replaying a run copies these rows back into the same rounds in this exact sequence.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT {_PROPOSAL_COLUMNS}
                FROM qd_factor_proposals
                WHERE run_id = ? AND user_id = ?
                ORDER BY round, ordinal
                LIMIT ? OFFSET ?
                """,
                (int(run_id), int(user_id), int(limit), int(offset)),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [self._hydrate_proposal(row) for row in rows]

    def failure_summary(
        self,
        *,
        user_id: int,
        market: str,
        timeframe: str,
        exclude_run_id: int,
        history_runs: int = 5,
        max_expressions: int = 10,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bounded failures from the same tenant's compatible completed run records.

        Cross-run evidence only -- a run's own results arrive through the engine's outcome callback,
        not from these tables, because trials are written after the search returns. Every query is
        scoped by ``user_id`` and market/timeframe; a supplied context additionally pins the
        source/universe, holding period, requested window and split. L5/L6 failures are read from
        result_json rather than mutable candidate rows. Holdout measurements are never included.
        """
        context_sql = ""
        context_params: list[Any] = []
        if context:
            context_sql = """
              AND source_id = ? AND LEFT(start_date, 10) = ? AND LEFT(end_date, 10) = ?
              AND COALESCE((config_json::jsonb ->> 'holding_period')::int, 5) = ?
              AND COALESCE((config_json::jsonb ->> 'freeze_ratio')::numeric, 0.3) = ?
              AND COALESCE((config_json::jsonb ->> 'holdout_ratio')::numeric, 0) = ?
              AND manifest_json::jsonb -> 'universe' = ?::jsonb
            """
            context_params = [int(context["sourceId"]), str(context["startDate"]),
                              str(context["endDate"]), int(context["holdingPeriod"]),
                              float(context["freezeRatio"]), float(context["holdoutRatio"]),
                              _dump(context.get("universe") or {})]
        recent_runs = f"""
            SELECT id, result_json
            FROM qd_factor_mining_runs
            WHERE user_id = ? AND market = ? AND timeframe = ?
              AND status = 'success' AND id <> ?
              {context_sql}
            ORDER BY id DESC
            LIMIT ?
        """
        scope = (
            int(user_id), str(market or ""), str(timeframe or ""), int(exclude_run_id),
            *context_params, max(0, min(20, int(history_runs))),
        )
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(recent_runs, scope)
                rows = cur.fetchall() or []
            finally:
                cur.close()
        run_ids = [_count(row.get("id")) for row in rows]
        counts: Counter[tuple[str, str, str]] = Counter()
        recent_failures: list[dict[str, Any]] = []
        limit = max(0, min(20, int(max_expressions)))
        for row in rows:
            record = _json_mapping(row.get("result_json"))
            for trial in record.get("trials") or []:
                counts[(str(trial.get("stage") or ""), str(trial.get("verdict") or ""),
                        str(trial.get("reason") or "")[:160])] += 1
            for candidate in record.get("candidates") or []:
                if candidate.get("status") != "rejected":
                    continue
                oos = candidate.get("outOfSample") or {}
                stage = "L6" if "poolIncrement" in oos else "L5"
                reason = str(candidate.get("reason") or "")[:160]
                counts[(stage, "reject", reason)] += 1
                if len(recent_failures) < limit:
                    recent_failures.append({
                        "expression": candidate.get("expression"),
                        "expressionHash": str(candidate.get("fingerprint") or ""),
                        "status": "rejected", "stage": stage, "reason": reason,
                        "metrics": _feedback_metrics(candidate.get("metrics") or {}),
                        "outOfSample": _feedback_metrics(oos),
                    })
        stage_counts = [{"stage": stage, "verdict": verdict, "reason": reason, "count": count}
                        for (stage, verdict, reason), count in sorted(counts.items())]
        return {
            "runIds": run_ids,
            "stageCounts": stage_counts,
            "recentFailures": recent_failures,
        }

    @staticmethod
    def _hydrate_run(row: Mapping[str, Any], *, include_result: bool = False) -> dict[str, Any]:
        """One run row as its caller reads it. ``result`` only when the detail view asks for it."""
        item = {public: row.get(column) for column, public in _RUN_KEYS.items()}
        item["config"] = _json_mapping(row.get("config_json"))
        item["manifest"] = _json_mapping(row.get("manifest_json"))
        for key in ("trialsCount", "candidatesCount", "frozenCount"):
            item[key] = _count(item[key])
        if include_result:
            item["result"] = _json_mapping(row.get("result_json"))
        return item

    @staticmethod
    def _hydrate_candidate(
        row: Mapping[str, Any], *, include_metrics: bool = True,
    ) -> dict[str, Any]:
        """One candidate row as its caller reads it.

        ``include_metrics=False`` drops the two metric payloads for list views and keeps everything
        else, the expression included. An unreadable expression stays ``None`` rather than an empty
        object, so a broken row is visible as one.
        """
        item = {public: row.get(column) for column, public in _CANDIDATE_KEYS.items()}
        item["expression"] = _expression_node(row.get("expression_json"))
        if include_metrics:
            item["metrics"] = _json_mapping(row.get("metrics_json"))
            item["outOfSample"] = _json_mapping(row.get("out_of_sample_json"))
        return item

    @staticmethod
    def _hydrate_proposal(row: Mapping[str, Any]) -> dict[str, Any]:
        """One proposal row as its caller reads it, with its expression tree decoded."""
        item = {public: row.get(column) for column, public in _PROPOSAL_KEYS.items()}
        item["expression"] = _expression_node(row.get("expression_json"))
        return item


def _feedback_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Only bounded scalar research evidence enters a prompt, never curves or holdout data."""
    keys = ("coverage", "rankIc", "icir", "averageTurnover", "stability", "sharpe",
            "deflatedSharpe", "maxDrawdown", "sampleCount", "turnover", "longShortSharpe")
    result = {key: metrics[key] for key in keys
              if key in metrics and isinstance(metrics[key], (int, float, bool, str, type(None)))}
    increment = metrics.get("poolIncrement")
    if isinstance(increment, Mapping):
        result["poolIncrement"] = {
            key: increment[key] for key in ("hasIncrementalValue", "residualIc", "deltaIc", "passed", "reason")
            if key in increment and isinstance(increment[key], (int, float, bool, str, type(None)))
        }
    return result


def _dump(payload: Any) -> str:
    """One serializer for every ``*_json`` column: any payload in, valid JSON text out."""
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> str:
    """Render what ``json`` cannot: ``config``'s native datetimes, as ISO-8601 like ``split``."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _decode(raw: Any) -> Any:
    """A stored JSON column decoded; anything unreadable reads as ``None``."""
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return None


def _json_mapping(raw: Any) -> dict[str, Any]:
    """A stored JSON object; an unreadable or non-object payload reads as empty."""
    value = raw if isinstance(raw, Mapping) else _decode(raw)
    return dict(value) if isinstance(value, Mapping) else {}


def _expression_node(raw: Any) -> dict[str, Any] | None:
    """A stored expression tree, or ``None`` when the column does not hold one."""
    value = _decode(raw)
    return dict(value) if isinstance(value, Mapping) else None


def _window_text(value: Any) -> str:
    """A run window bound as text: ``MiningConfig`` holds datetimes, payloads hold strings."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "")


def _count(value: Any) -> int:
    """A stored total as an integer; anything unreadable counts as none."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
