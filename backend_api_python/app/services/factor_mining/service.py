"""Application service: one mining request becomes one durable, cancelable agent job.

Two records, written at different moments on purpose. The **job** row (``qd_agent_jobs``) is the
timeline the API polls -- queued, running, succeeded, failed, cancelled. The **run** row
(``qd_factor_mining_runs``) is what the ledger is audited against, and it is opened *before* the
search so a crash mid-run still leaves a row saying which config was being searched.

Where each check lives. The request boundary validates what only it can see -- a source id, a
market, a budget below one -- before anything is dispatched, so a malformed request fails the
caller's own request instead of becoming a job that fails once polled, and so a rejected request
never opens a job row. Everything the payload already carries a contract for is left to its
owner: ``MiningConfig.from_payload`` keeps the hard budget cap and the date window,
``expand_candidates`` names an unknown template, and the engine's provider refuses a universe it
cannot price. Nothing here clamps, defaults over, or re-derives a value one of those layers
rejected -- a run that searched something other than what was asked for is worse than a failed
one.

Two counts are the orchestration's own. ``trials_count`` handed to the frozen review is the
*whole* ledger -- rejected candidates included -- because that count is the deflated-Sharpe
denominator, and pricing the search with the survivor count would understate exactly the bias the
correction exists for. And ``summary.frozen`` is the engine's placeholder 0: the search cannot
know how many survivors hold up out of sample, so the count is written back here, from the
review's own statuses, into the result ``finish_run`` persists.

The runner is a bound method (``runner=self._run_mining_job``) rather than a module-level
function, so the collaborators this layer injects -- repository, engine factory, backtest service
-- are the same ones the job actually runs with. ``run_mining_job`` is the module-level shell the
durable (Celery) path imports; it resolves the process singleton and calls that same bound method,
so both dispatch paths execute one implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from app.markets.registry import list_market_keys
from app.services.backtest_execution import DEFAULT_COMMISSION, DEFAULT_SLIPPAGE
from app.services.factor_mining.engine import (
    FactorMiningEngine,
    MiningConfig,
    MiningDataProvider,
)
from app.services.factor_mining.storage import FactorMiningRepository
from app.services.factor_mining.templates import (
    DEFAULT_BUDGET,
    expand_candidates,
    template_catalog,
)
from app.services.script_source import get_script_source_service
from app.services.strategy_v2.contract import compile_strategy_v2
from app.services.strategy_v2.service import StrategyV2BacktestService
from app.utils import agent_jobs


JOB_KIND = "factor_mining"

# The integer fields ``MiningConfig`` reads with ``int()``. Only an absent key falls back to its
# default there, so a blank ``""`` would raise a bare ``ValueError`` and escape this module's
# closed key set; the boundary reads it as absent instead.
_BLANK_AS_ABSENT = ("sourceId", "budget", "seed", "groups", "holdingPeriod")

_KNOWN_MARKETS = frozenset(list_market_keys())


class FactorMiningService:
    """Submit, poll, and cancel factor mining runs for one tenant.

    Collaborators are injected so the orchestration can be exercised without a database or a data
    source. ``engine_factory`` builds the engine per job, because the progress callback belongs to
    the job and not to the service; ``backtest_service`` is the one data path the engine's
    provider reads panels through.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[..., FactorMiningEngine] | None = None,
        repository: FactorMiningRepository | None = None,
        backtest_service: StrategyV2BacktestService | None = None,
    ) -> None:
        self._engine_factory = engine_factory or FactorMiningEngine
        self._repository = repository or FactorMiningRepository()
        self._backtest_service = backtest_service or StrategyV2BacktestService()

    def estimate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a payload and price its search statically: no source read, no panel, no job.

        The candidate count is the expansion's own rather than the budget: the budget is a cap,
        and a template set that cannot fill it is what the caller needs to see.
        """
        config = self._config(payload)
        seeds = expand_candidates(
            template_ids=config.template_ids or None,
            budget=config.budget,
            seed=config.seed,
        )
        return {
            "templateCount": len(config.template_ids) or len(template_catalog()),
            "candidateBudget": len(seeds),
        }

    def submit(self, *, user_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Queue one mining run and return its job receipt."""
        request_payload = dict(payload or {})
        self._config(request_payload)
        if agent_jobs.count_active_jobs(user_id=int(user_id)):
            raise ValueError("factorMining.concurrentLimit")
        receipt = agent_jobs.submit_job(
            user_id=int(user_id),
            agent_token_id=None,
            kind=JOB_KIND,
            request_payload={**request_payload, "__userId": int(user_id)},
            runner=self._run_mining_job,
        )
        return _public_job(receipt)

    def get_job(self, job_id: str, *, user_id: int) -> dict[str, Any] | None:
        """One of the caller's mining jobs; another tenant's or another feature's job is not found."""
        row = agent_jobs.get_job(str(job_id), user_id=int(user_id))
        return _public_job(row) if row and row.get("kind") == JOB_KIND else None

    def list_jobs(self, *, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """The caller's mining jobs, newest first, in the same envelope as :meth:`get_job`.

        Each job is re-read for its progress and result: a poller lists jobs to find the one that
        finished, and the summary query carries neither.
        """
        items = []
        for summary in agent_jobs.list_jobs(user_id=int(user_id), kind=JOB_KIND, limit=limit):
            row = agent_jobs.get_job(str(summary.get("job_id") or ""), user_id=int(user_id))
            if row:
                items.append(_public_job(row))
        return items

    def cancel_job(self, job_id: str, *, user_id: int) -> dict[str, Any] | None:
        """Mark one of the caller's jobs cancelled; the runner stops at its next progress report."""
        if self.get_job(job_id, user_id=user_id) is None:
            return None
        return _public_job(agent_jobs.cancel_job(str(job_id), user_id=int(user_id)))

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
        """The caller's candidate library, newest first: the repository's own filtered, paged read.

        A forwarder on purpose. The filter set, the page window, and the choice to leave a list
        row's metrics behind are the repository's decisions -- it documents each of them -- and a
        second copy here would be a second place for the library view to drift.
        """
        return self._repository.list_candidates(
            user_id=int(user_id),
            run_id=run_id,
            status=status,
            market=market,
            limit=limit,
            offset=offset,
        )

    def get_candidate(self, candidate_id: int, *, user_id: int) -> dict[str, Any] | None:
        """One candidate with its metrics and out-of-sample block; not the caller's reads as ``None``."""
        return self._repository.get_candidate(candidate_id, user_id=int(user_id))

    def _run_mining_job(self, payload: Mapping[str, Any], on_progress: Callable | None = None) -> dict[str, Any]:
        """Run one submitted job: open the run, search, ledger it, review it, close it.

        The run is closed on every path -- success with the review's counts, failure with the key
        that ended it -- and the exception is re-raised afterwards so ``agent_jobs`` records the
        job failure too. Nothing is swallowed: a review that cannot run leaves a failed run, not a
        run whose candidates quietly stay frozen.
        """
        request_payload = dict(payload or {})
        user_id = int(request_payload.pop("__userId"))
        config = self._config(request_payload)
        source = get_script_source_service().get_source(config.source_id, user_id=user_id)
        if not source:
            raise ValueError("factorMining.sourceNotFound")
        code = str(source.get("code") or "").strip()
        if not code:
            raise ValueError("factorMining.sourceCodeRequired")
        run_id = self._repository.persist_run(
            user_id=user_id,
            config=asdict(config),
            source_id=config.source_id,
            source_name=str(source.get("name") or ""),
            # The panel is prepared inside the search, after this row exists; ``finish_run``
            # backfills the fingerprint from the result.
            snapshot_id="",
            manifest=compile_strategy_v2(code).manifest.metadata(),
        )
        try:
            engine = self._engine_factory(
                data_provider=MiningDataProvider(backtest_service=self._backtest_service),
                on_progress=on_progress,
            )
            result = engine.run(user_id=user_id, code=code, config=config)
            self._repository.save_trials(run_id, user_id=user_id, trials=result["trials"])
            self._repository.upsert_candidates(
                user_id=user_id, run_id=run_id, candidates=result["candidates"],
            )
            reviewed = engine.review_frozen(
                user_id=user_id,
                code=code,
                config=config,
                survivors=result["candidates"],
                trials_count=len(result["trials"]),
            )
            self._repository.upsert_candidates(
                user_id=user_id, run_id=run_id, candidates=reviewed,
            )
            result["candidates"] = reviewed
            result["summary"]["frozen"] = sum(
                1 for candidate in reviewed if candidate.get("status") == "frozen"
            )
            self._repository.finish_run(run_id, user_id=user_id, status="success", result=result)
        except Exception as exc:
            self._repository.finish_run(
                run_id, user_id=user_id, status="failed", result={}, error_message=str(exc),
            )
            raise
        return result

    @staticmethod
    def _config(payload: Mapping[str, Any]) -> MiningConfig:
        """Read the boundary payload into a run config, rejecting what does not belong.

        A source id, a market, and a budget below one are this layer's to refuse: the first two
        are request-level facts the config merely carries, and the third is the case
        ``MiningConfig.from_payload`` deliberately leaves open -- it keeps an explicit 0 rather
        than defaulting it -- so this method is what turns it into a key. The hard budget cap
        stays with ``from_payload``, which already owns ``factorMining.budgetExceeded``.
        """
        request = {
            key: value
            for key, value in payload.items()
            if not (key in _BLANK_AS_ABSENT and isinstance(value, str) and not value.strip())
        }
        if not _positive_int(request.get("sourceId")):
            raise ValueError("factorMining.sourceRequired")
        raw_budget = request.get("budget")
        try:
            budget = DEFAULT_BUDGET if raw_budget is None else int(raw_budget)
        except (TypeError, ValueError) as exc:
            raise ValueError("factorMining.budgetInvalid") from exc
        if budget < 1:
            raise ValueError("factorMining.budgetInvalid")
        if str(request.get("market") or "") not in _KNOWN_MARKETS:
            raise ValueError("factorMining.invalidMarket")
        return MiningConfig.from_payload(
            request,
            default_commission=DEFAULT_COMMISSION,
            default_slippage=DEFAULT_SLIPPAGE,
        )


_service: FactorMiningService | None = None


def get_factor_mining_service() -> FactorMiningService:
    """The process-wide service, shared by the HTTP routes and the durable job runner."""
    global _service
    if _service is None:
        _service = FactorMiningService()
    return _service


def run_mining_job(
    payload: Mapping[str, Any],
    on_progress: Callable | None = None,
) -> dict[str, Any]:
    """Run one persisted job from its request payload.

    The entry point the Celery worker imports. It is deliberately a shell: the orchestration stays
    on the service, so both dispatch paths -- the in-process pool's bound method and this one --
    run the same implementation with the same collaborators.
    """
    return get_factor_mining_service()._run_mining_job(payload, on_progress)


def _public_job(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The job as the API spells it. Both a stored row and the submit receipt land here."""
    if not row:
        return None
    return {
        "jobId": row.get("job_id"),
        "status": row.get("status"),
        "progress": _json(row.get("progress")) or {},
        "result": _json(row.get("result")),
        "error": row.get("error"),
        "createdAt": row.get("created_at"),
        "startedAt": row.get("started_at"),
        "finishedAt": row.get("finished_at"),
    }


def _json(value: Any) -> Any:
    """A jsonb column, decoded when the driver hands it back as text."""
    return json.loads(value) if isinstance(value, str) else value


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
