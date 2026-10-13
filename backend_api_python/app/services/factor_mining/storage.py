"""Persistence for factor mining runs, candidates, and the trial ledger.

Three lifetimes in three tables. A **run** is one job: its config, the fingerprint of the panel it
searched, and the counts it ended with. A **candidate** converges -- ``(user_id, expression_hash)``
is unique, so the same expression evaluated by a later run updates that row instead of adding a
second one, and ``run_id`` keeps pointing at the run that judged it last. A **trial** does not
converge: one row per evaluation, rejected candidates included, because that count is the
deflated-Sharpe denominator the frozen review prices the search with, and a ledger missing its
failures would understate exactly the bias the correction exists for.

Serialization happens here and nowhere else. The engine's record mixes timestamp styles by design
-- ``config`` is a ``MiningConfig`` dumped by ``asdict``, so it carries native ``datetime`` objects
under snake_case keys, while ``split`` is already ISO-8601 strings -- and both shapes are reviewed
contracts this layer will not rewrite. Instead one serializer makes any payload JSON-safe and
renders datetimes as ISO-8601, so every timestamp a stored run holds reads the same way.

No clock, network, or panel access: every method takes and returns plain mappings, and the
``_hydrate*`` helpers are static so the row shapes are testable without a database.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from app.utils.db import get_db_connection


_RUN_COLUMNS = """
    id, user_id, source_id, source_name, market, timeframe, start_date, end_date,
    snapshot_id, status, trials_count, candidates_count, frozen_count, error_message,
    created_at, config_json, manifest_json
"""

_CANDIDATE_COLUMNS = """
    id, user_id, run_id, expression_json, expression_hash, origin, template_id, market,
    timeframe, status, reject_reason, metrics_json, out_of_sample_json, created_at, updated_at
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


class FactorMiningRepository:
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
    ) -> int:
        """Open a run row and return its id.

        ``config`` is the run's ``MiningConfig`` as a mapping -- the snake_case ``asdict`` shape
        ``engine.run`` returns -- which is where the denormalised market and window columns are
        read from. ``snapshot_id`` is usually empty here: the panel is prepared inside the search,
        after this row exists, so ``finish_run`` backfills the fingerprint from the result.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_factor_mining_runs
                (user_id, source_id, source_name, market, timeframe, start_date, end_date,
                 snapshot_id, config_json, manifest_json, result_json, status,
                 trials_count, candidates_count, frozen_count, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, 0, 0, 0, '', NOW())
                """,
                (
                    int(user_id), int(source_id), str(source_name or ""),
                    str(config.get("market") or ""), str(config.get("timeframe") or ""),
                    _window_text(config.get("start_date")), _window_text(config.get("end_date")),
                    str(snapshot_id or ""), _dump(dict(config)), _dump(dict(manifest)),
                    str(status or "running"),
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
        status: str,
        result: Mapping[str, Any],
        error_message: str = "",
    ) -> None:
        """Close a run row with its status, payload, and the counts lifted out of the result.

        The counts come from ``summary``, where they are integer totals rather than lists, and
        ``frozen`` stays 0 for anything but the orchestration layer's final write: only the review
        knows how many survivors held up out of sample. The panel fingerprint is backfilled here
        because ``persist_run`` precedes the search; an empty one in the result leaves whatever the
        insert stored.
        """
        summary = result.get("summary") if isinstance(result, Mapping) else None
        summary = summary if isinstance(summary, Mapping) else {}
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                UPDATE qd_factor_mining_runs
                SET status = ?, error_message = ?, result_json = ?,
                    trials_count = ?, candidates_count = ?, frozen_count = ?,
                    snapshot_id = COALESCE(NULLIF(?, ''), snapshot_id)
                WHERE id = ?
                """,
                (
                    str(status or ""), str(error_message or ""), _dump(dict(result)),
                    _count(summary.get("trials")), _count(summary.get("candidates")),
                    _count(summary.get("frozen")), str(summary.get("snapshotId") or ""),
                    int(run_id),
                ),
            )
            db.commit()
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
        last page.
        """
        rows = [
            (
                int(run_id), int(user_id), str(trial.get("fingerprint") or ""),
                str(trial.get("stage") or ""), str(trial.get("verdict") or ""),
                str(trial.get("reason") or ""), _dump(trial.get("metrics") or {}),
            )
            for trial in trials
        ]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(
                """
                INSERT INTO qd_factor_trials
                (run_id, user_id, expression_hash, stage, verdict, reject_reason, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            db.commit()
            cur.close()
        return len(rows)

    def upsert_candidates(
        self,
        *,
        user_id: int,
        run_id: int,
        candidates: Sequence[Mapping[str, Any]],
    ) -> int:
        """Write the candidate library and return how many rows were written.

        ``(user_id, expression_hash)`` is the identity, so re-evaluating an expression updates its
        row -- status, reason, metrics, out-of-sample metrics -- and moves ``run_id`` to the run
        that judged it last, rather than growing the table with a copy per run. A normal run calls
        this twice over the same expressions: the search writes survivors as ``search``, and the
        frozen review writes their final status and out-of-sample metrics on top.

        The market and timeframe are read from the owning run inside the statement, because a
        candidate record carries neither: it is the run that knows which market was searched.
        """
        rows = [
            (
                int(user_id), int(run_id), _dump(candidate.get("expression") or {}),
                str(candidate.get("fingerprint") or ""), str(candidate.get("origin") or ""),
                str(candidate.get("template_id") or ""), str(candidate.get("status") or ""),
                str(candidate.get("reason") or ""), _dump(candidate.get("metrics") or {}),
                _dump(candidate.get("outOfSample") or {}), int(run_id),
            )
            for candidate in candidates
        ]
        if not rows:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(
                """
                INSERT INTO qd_factor_candidates
                (user_id, run_id, expression_json, expression_hash, origin, template_id,
                 market, timeframe, status, reject_reason, metrics_json, out_of_sample_json,
                 created_at, updated_at)
                SELECT ?, ?, ?, ?, ?, ?, run.market, run.timeframe, ?, ?, ?, ?, NOW(), NOW()
                FROM qd_factor_mining_runs AS run
                WHERE run.id = ?
                ON CONFLICT (user_id, expression_hash) DO UPDATE
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
                """,
                rows,
            )
            db.commit()
            cur.close()
        return len(rows)

    def count_trials(self, run_id: int) -> int:
        """The run's whole ledger: the denominator the out-of-sample review is priced by.

        Scoped by ``run_id`` rather than by user because it answers the orchestration layer's own
        question about a run it just wrote. A user-facing count comes from ``get_run``, which is
        user-scoped and carries the same total as ``trialsCount``.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT COUNT(*) AS total FROM qd_factor_trials WHERE run_id = ?",
                (int(run_id),),
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

    def list_candidates(
        self,
        *,
        user_id: int,
        run_id: int | None = None,
        status: str = "",
        market: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """The candidate library, newest first, filtered by the run or the state it ended in.

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
    value = _decode(raw)
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
