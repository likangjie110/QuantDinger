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
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app.markets.registry import list_market_keys
from app.services.backtest_execution import DEFAULT_COMMISSION, DEFAULT_SLIPPAGE
from app.services.factor_mining.dsl import DSL_VERSION, fields_for
from app.services.factor_mining.engine import (
    MAX_AI_PROPOSALS_PER_ROUND,
    MAX_AI_ROUNDS,
    SEARCH_MODE_AI,
    AISearchOptions,
    FactorMiningEngine,
    MiningConfig,
    MiningDataProvider,
    _payload_datetime,
    _timeframe,
)
from app.services.factor_mining.gates import GateThresholds
from app.services.factor_mining.proposals import LLMProposer, RESEARCH_NOTES_MAX_CHARS
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

# The numeric fields ``MiningConfig`` reads with ``int()``/``float()``. Only an absent key falls
# back to its default there, so a blank ``""`` would raise a bare ``ValueError`` and escape this
# module's closed key set; the boundary reads it as absent instead. That tolerance is the whole
# contract for the breeder's three numbers: they are optional in enumeration mode and bounded
# whenever they are given, and a form that serializes an empty box as ``""`` must not be answered
# with a key that says the parameter was illegal. The engine's own requirement is a separate check
# and stays one -- a blank still cannot satisfy a run that breeds, because evolution names all
# three rather than defaulting into a population nobody asked for.
_BLANK_AS_ABSENT = (
    "sourceId", "budget", "seed", "groups", "holdingPeriod",
    "population", "generations", "eliteRatio",
)

_KNOWN_MARKETS = frozenset(list_market_keys())

# The run's configuration as a job carries it back to its caller. Read out of the stored request
# by an explicit whitelist rather than echoed whole: that mapping also holds the job store's own
# ``__``-prefixed channel (``__userId``, ``__billing``), which is not the caller's to read. It is
# part of the job because the list view withholds ``result``, so without it a poller cannot tell
# which market or window a listed job ran, and the keys that leave are the ones a caller
# recognises their own submission by.
_JOB_CONFIG_KEYS = (
    "sourceId", "sourceName", "market", "timeframe", "startDate", "endDate", "freezeRatio",
    "budget", "seed", "templateIds", "groups", "holdingPeriod", "commission", "slippage",
    "neutralizeIndustry", "thresholds", "searchMode", "population", "generations", "eliteRatio",
    "aiRounds", "aiProposalsPerRound", "replayJobId",
)

# The run fields a replay copies from the recorded run, in the spelling a submission uses.
# ``searchMode`` and ``replayJobId`` are added by the replay itself; ``researchNotes`` is the
# caller's own and never part of the recorded config.
_REPLAY_FIELDS = (
    "sourceId", "sourceName", "market", "timeframe", "startDate", "endDate", "freezeRatio",
    "budget", "seed", "templateIds", "groups", "holdingPeriod", "commission", "slippage",
    "neutralizeIndustry", "thresholds", "aiRounds", "aiProposalsPerRound",
)

# Of those, the ones a client may also state -- and only when its *effective* value matches the
# record. The rest are the record's alone: a client repeating one is ignored rather than refused,
# because those values do not change which search the replay reproduces.
_REPLAY_CONFLICT_FIELDS = (
    "market", "timeframe", "startDate", "endDate", "budget", "thresholds",
)

# One AI run's own bounds make its proposal count at most this, so a replay reads the whole
# record rather than a first page. The public list endpoint clamps to its own page sizes.
_REPLAY_PROPOSAL_LIMIT = MAX_AI_ROUNDS * MAX_AI_PROPOSALS_PER_ROUND


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
        proposer_factory: Callable[[], Any] = LLMProposer,
    ) -> None:
        self._engine_factory = engine_factory or FactorMiningEngine
        self._repository = repository or FactorMiningRepository()
        self._backtest_service = backtest_service or StrategyV2BacktestService()
        # Built per AI run from the injected factory, so a test can hand over a fake and
        # production gets the adapter that calls the configured model.
        self._proposer_factory = proposer_factory

    def estimate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a payload and price its search statically: no source read, no panel, no job.

        The candidate count is the expansion's own rather than the budget: the budget is a cap,
        and a template set that cannot fill it is what the caller needs to see. An AI run
        expands no templates at all: its candidates are what the model proposes, and the two
        AI bounds cap that stream exactly the way the engine's own source does.
        """
        config = self._config(payload)
        if config.search_mode == SEARCH_MODE_AI:
            return {
                "templateCount": 0,
                "candidateBudget": min(
                    config.ai_rounds * config.ai_proposals_per_round, config.budget,
                ),
            }
        seeds = expand_candidates(
            template_ids=config.template_ids or None,
            budget=config.budget,
            seed=config.seed,
            allowed_fields=fields_for(config.market),
        )
        # Deduped like the expansion dedupes: a repeated id contributes nothing to the search, and
        # counting it twice would report a template total the candidate count cannot come from.
        return {
            "templateCount": len(set(config.template_ids)) or len(template_catalog()),
            "candidateBudget": len(seeds),
        }

    def submit(
        self, *, user_id: int, payload: Mapping[str, Any],
        agent_token_id: int | None = None,
    ) -> dict[str, Any]:
        """Queue one mining run and return its job receipt.

        The ``__`` prefix is the job store's own channel -- ``__billing`` is refunded from the
        stored request, ``__userId`` scopes the run, and ``__jobId`` is the id generated here so
        the run row can name the job that drives it -- so a client-supplied one is dropped here,
        where the untrusted mapping enters, rather than in ``submit_job``, which cannot tell a
        client's key from the one this method is about to inject. Order matters: strip first,
        then inject.

        A replay is resolved before the payload is validated: the recorded run's config *is* the
        submission, so validating the client's two-key request against a run config it does not
        carry could only refuse a legal replay. A field the client repeats must agree with the
        record; the recorded value wins whenever it does.

        The concurrency gate counts this feature's own jobs. Counting every kind would let a
        running backtest answer ``factorMining.concurrentLimit`` -- a key that says the caller
        already has a mining job -- and would let an unswept ghost from another feature block
        mining forever, because this feature's sweep only retires its own kind.
        """
        request_payload = {
            key: value for key, value in (payload or {}).items() if not str(key).startswith("__")
        }
        request_payload = self._replay_request(request_payload, int(user_id))
        config = self._config(request_payload)
        self._research_notes(request_payload, config)
        if agent_jobs.count_active_jobs(user_id=int(user_id), kind=JOB_KIND):
            raise ValueError("factorMining.concurrentLimit")
        job_id = uuid.uuid4().hex
        stored = {**request_payload, "__userId": int(user_id), "__jobId": job_id}
        receipt = agent_jobs.submit_job(
            user_id=int(user_id),
            agent_token_id=agent_token_id,
            kind=JOB_KIND,
            request_payload=stored,
            runner=self._run_mining_job,
            job_id=job_id,
        )
        # The job store's receipt names the job and nothing else -- it carries no ``request`` -- so
        # the config the caller just submitted is attached here. A receipt that cannot say what was
        # accepted is not a receipt, and the caller would otherwise have to poll the job to learn
        # whether their own market and window were the ones queued. ``_job_config`` still reads it
        # through the whitelist, so ``__userId`` and ``__jobId`` do not travel with it.
        return _public_job({**receipt, "request": stored})

    def get_job_proposals(
        self, job_id: str, *, user_id: int, limit: int = 50, offset: int = 0,
    ) -> list[dict[str, Any]] | None:
        """The proposals one AI job's run recorded; not the caller's job reads as ``None``.

        The job is checked first because it is what the caller holds: another tenant's id and an
        id of another feature both read as absent. A job that has not opened its run yet -- still
        queued -- reads as absent too, while a running one answers whatever rows it has written
        so far, which may be none.
        """
        job = agent_jobs.get_job(str(job_id), user_id=int(user_id))
        if not job or job.get("kind") != JOB_KIND:
            return None
        run = self._repository.get_run_by_job_id(str(job_id), user_id=int(user_id))
        if run is None:
            return None
        return self._repository.list_proposals(
            int(run["id"]), user_id=int(user_id), limit=int(limit), offset=int(offset),
        )

    def _replay_request(self, request: Mapping[str, Any], user_id: int) -> dict[str, Any]:
        """A replay request rewritten to the recorded run's config; anything else is unchanged.

        Beyond the run id the client may add only its own research notes. Of the run-shape fields
        it repeats, the six in ``_REPLAY_CONFLICT_FIELDS`` are compared by effective value and
        refused on disagreement -- silently accepting either side would make a replay of one run
        search another, which is the one thing the mode's name promises it cannot do. Every
        other field is the record's to state; a client value for one is ignored.
        """
        replay_job_id = str(request.get("replayJobId") or "").strip()
        if not replay_job_id:
            return dict(request)
        mode = str(request.get("searchMode") or "").strip()
        if mode and mode != SEARCH_MODE_AI:
            raise ValueError("factorMining.invalidParameter")
        original, _proposals = self._replay_source(replay_job_id, user_id)
        replay = _replay_fields(original, replay_job_id)
        for key in _REPLAY_CONFLICT_FIELDS:
            if key in request and _effective_field(
                key, request[key],
            ) != _effective_field(key, replay[key]):
                raise ValueError("factorMining.aiReplayIncompatible")
        if "researchNotes" in request:
            replay["researchNotes"] = request["researchNotes"]
        return replay

    def _replay_source(self, job_id: str, user_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The run a replay copies and its proposal rows, or the key that refuses the replay.

        Only an AI run can be replayed: a replay reproduces model proposals, and no other mode
        recorded any. ``success`` and ``failed`` are both replayable -- a run that failed part-way
        wrote proposals but no trials, so a replay is how its recorded material is evaluated
        again -- while ``cancelled`` and ``running`` are not. A run with no proposals has nothing
        to replay whatever its status. The proposals also have to be readable by the current DSL;
        a row without a version is read as current -- every stored row carries one, and a
        hand-built record cannot say otherwise.
        """
        run = self._repository.get_run_by_job_id(str(job_id), user_id=int(user_id))
        if not run:
            raise ValueError("factorMining.aiReplayNotFound")
        config = run.get("config")
        config = config if isinstance(config, Mapping) else {}
        if str(config.get("search_mode") or "") != SEARCH_MODE_AI:
            raise ValueError("factorMining.aiReplayIncompatible")
        if str(run.get("status") or "") not in ("success", "failed"):
            raise ValueError("factorMining.aiReplayIncompatible")
        proposals = self._repository.list_proposals(
            int(run["id"]), user_id=int(user_id), limit=_REPLAY_PROPOSAL_LIMIT, offset=0,
        )
        if not proposals:
            raise ValueError("factorMining.aiReplayIncompatible")
        for proposal in proposals:
            if int(proposal.get("dslVersion", DSL_VERSION)) != DSL_VERSION:
                raise ValueError("factorMining.aiReplayIncompatible")
        return run, proposals

    @staticmethod
    def _research_notes(payload: Mapping[str, Any], config: MiningConfig) -> str:
        """The caller's notes for the AI model: optional, bounded, and AI-only.

        Notes on a run that names no AI mode are refused rather than dropped: the caller wrote
        them for a model call that run will never make, and a stored request carrying them would
        read as a search that consulted them.
        """
        notes = payload.get("researchNotes")
        if notes is None:
            return ""
        if not isinstance(notes, str):
            raise ValueError("factorMining.invalidParameter")
        if len(notes) > RESEARCH_NOTES_MAX_CHARS:
            raise ValueError("factorMining.invalidParameter")
        if notes and config.search_mode != SEARCH_MODE_AI:
            raise ValueError("factorMining.invalidParameter")
        return notes

    def get_job(self, job_id: str, *, user_id: int) -> dict[str, Any] | None:
        """One of the caller's mining jobs; another tenant's or another feature's job is not found."""
        row = agent_jobs.get_job(str(job_id), user_id=int(user_id))
        return _public_job(row) if row and row.get("kind") == JOB_KIND else None

    def list_jobs(self, *, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """The caller's mining jobs, newest first, in the same envelope as :meth:`get_job`.

        Each job is re-read for its progress and result: a poller lists jobs to find the one that
        finished, and the summary query carries neither. Listing is also where a job orphaned by a
        dead worker is retired -- an unswept ghost is not cosmetic, because the concurrency gate
        counts active jobs, so it would otherwise block every later submission by that tenant.
        The result is left out of the listed row (``hasResult`` says whether there is one): it is
        the whole run record, and twenty of them is more than a listing can carry.
        """
        items = []
        for summary in agent_jobs.list_jobs(user_id=int(user_id), kind=JOB_KIND, limit=limit):
            if _is_stale_running_job(summary):
                agent_jobs._set_failure(
                    str(summary.get("job_id") or ""), "factorMining.workerInterrupted",
                )
            row = agent_jobs.get_job(str(summary.get("job_id") or ""), user_id=int(user_id))
            if row:
                items.append(_public_job(row, include_result=False))
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

        An AI job builds the source's options here, because the run id and the recorded
        proposals only exist at this point: the id is the row opened just above, and a replay's
        proposals are re-read and re-validated from the run it names rather than trusted from
        the submission, which may be older than the record.
        """
        request_payload = dict(payload or {})
        user_id = int(request_payload.pop("__userId"))
        job_id = str(request_payload.pop("__jobId", ""))
        config = self._config(request_payload)
        notes = self._research_notes(request_payload, config)
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
            # The id the job was submitted under, so a proposal lookup works from either side
            # while the job runs and after it failed.
            job_id=job_id,
        )
        result: dict[str, Any] = {}
        original: dict[str, Any] | None = None
        try:
            ai = None
            if config.search_mode == SEARCH_MODE_AI:
                replay_proposals: tuple[Mapping[str, Any], ...] = ()
                if config.replay_job_id:
                    original, rows = self._replay_source(config.replay_job_id, user_id)
                    replay_proposals = tuple(rows)
                ai = AISearchOptions(
                    run_id=run_id,
                    research_notes=notes,
                    proposer=self._proposer_factory(),
                    repository=self._repository,
                    replay_proposals=replay_proposals,
                )
            engine = self._engine_factory(
                data_provider=MiningDataProvider(backtest_service=self._backtest_service),
                on_progress=on_progress,
            )
            search = engine.run(user_id=user_id, code=code, config=config,
                                search_mode=config.search_mode, ai=ai)
            # The record names the run it belongs to, so a job result is auditable by itself.
            search["runId"] = run_id
            _record_ai_summary(search, config, original)
            self._repository.save_trials(run_id, user_id=user_id, trials=search["trials"])
            # The counts may only be persisted once the ledger holds them: a run row closed before
            # its trials were written would claim evaluations the ledger never received.
            result = search
            self._repository.upsert_candidates(
                user_id=user_id, run_id=run_id, candidates=result["candidates"],
            )
            reviewed = engine.review_frozen(
                user_id=user_id,
                code=code,
                config=config,
                survivors=result["candidates"],
                trials_count=len(result["trials"]),
                # The review carries the search's own mode: it is the warmup rule the frozen
                # panel is fetched with, and the survivors it judges were bred under that rule.
                # Left to the engine's default, the tail would be fetched short of an evolved
                # survivor's lookback and the out-of-sample window silently truncated.
                search_mode=config.search_mode,
            )
            self._repository.upsert_candidates(
                user_id=user_id, run_id=run_id, candidates=reviewed,
            )
            result["candidates"] = reviewed
            result["summary"]["frozen"] = sum(
                1 for candidate in reviewed if candidate.get("status") == "frozen"
            )
            self._repository.finish_run(run_id, user_id=user_id, status="success", result=result)
        except agent_jobs.JobCancelledError:
            # The job store already says cancelled, and the run row has to agree: a library view
            # that reads "failed" cannot tell the user's own cancellation from an engine failure.
            self._repository.finish_run(
                run_id, user_id=user_id, status="cancelled", result=result,
            )
            raise
        except Exception as exc:
            # ``result`` carries whatever the search already produced. A review that fails after
            # the ledger was written must not persist zero counts and an empty fingerprint against
            # a run whose trials are on disk.
            self._repository.finish_run(
                run_id, user_id=user_id, status="failed", result=result, error_message=str(exc),
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
        except (TypeError, ValueError, OverflowError) as exc:
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


def _effective_field(key: str, value: Any) -> Any:
    """One conflicting field as the run would read it, for comparing spellings of the same run.

    The readers are the config's own -- ``_timeframe``, ``_payload_datetime``,
    ``GateThresholds.from_payload`` -- so ``"d"`` matches the record's ``"1D"``, ``"20"`` its
    ``20``, and a partial threshold override its effective bar. Comparing raw payload values
    instead would refuse a replay of the very config a receipt hands back.
    """
    if key == "market":
        return str(value or "")
    if key == "timeframe":
        return _timeframe(value, "1D")
    if key in ("startDate", "endDate"):
        return _effective_date(value)
    if key == "budget":
        return _effective_budget(value)
    return GateThresholds.from_payload(value)


def _effective_date(value: Any) -> datetime:
    """A run date as the payload reader resolves it: the day, with no time of day.

    ``config_json`` stores ISO datetimes, and an end date carries the day's last second; both
    sides are cut to their date first so the stored spelling and a client's ``YYYY-MM-DD``
    compare as the same day.
    """
    if isinstance(value, datetime):
        return _payload_datetime(value.date().isoformat())
    return _payload_datetime(str(value or "")[:10])


def _effective_budget(value: Any) -> int:
    """A budget as ``_config`` reads it, raising the field's own key on an unreadable one."""
    try:
        return DEFAULT_BUDGET if value is None else int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("factorMining.budgetInvalid") from exc


def _replay_fields(run: Mapping[str, Any], replay_job_id: str) -> dict[str, Any]:
    """The recorded run's config, spelled as the submission payload a replay submits.

    The config is the source rather than the run's denormalised columns: it is the record the
    search actually read, and it carries every field on its own. Dates are cut back to the day
    because ``config_json`` stores ISO datetimes while the payload reader accepts ``YYYY-MM-DD``
    only; ``from_payload`` restores the end date to its day's end, so the cut loses nothing.
    """
    config = run.get("config")
    config = config if isinstance(config, Mapping) else {}
    return {
        "sourceId": config.get("source_id"),
        "sourceName": str(config.get("source_name") or ""),
        "market": str(config.get("market") or ""),
        "timeframe": str(config.get("timeframe") or ""),
        "startDate": str(config.get("start_date") or "")[:10],
        "endDate": str(config.get("end_date") or "")[:10],
        "freezeRatio": config.get("freeze_ratio"),
        "budget": config.get("budget"),
        "seed": config.get("seed"),
        "templateIds": list(config.get("template_ids") or ()),
        "groups": config.get("groups"),
        "holdingPeriod": config.get("holding_period"),
        "commission": config.get("commission"),
        "slippage": config.get("slippage"),
        "neutralizeIndustry": bool(config.get("neutralize_industry", False)),
        "thresholds": dict(config.get("thresholds") or {}),
        "aiRounds": config.get("ai_rounds"),
        "aiProposalsPerRound": config.get("ai_proposals_per_round"),
        "searchMode": SEARCH_MODE_AI,
        "replayJobId": replay_job_id,
    }


def _record_ai_summary(
    search: Mapping[str, Any], config: MiningConfig, original: Mapping[str, Any] | None,
) -> None:
    """Write the replay facts an AI run's summary carries: what it copied and whether the data moved.

    ``snapshotChanged`` compares the panel this run searched with the original run's recorded
    fingerprint. It is the reading a replay is for: the same proposals evaluated over different
    data are a different result, and the summary has to say which of the two happened. A run
    recorded before the fingerprint was backfilled cannot answer, so an empty original snapshot
    reports ``false`` rather than a comparison against nothing.
    """
    summary = search.get("summary")
    ai_summary = summary.get("ai") if isinstance(summary, Mapping) else None
    if not isinstance(ai_summary, dict):
        return
    ai_summary["replayOf"] = config.replay_job_id
    original_snapshot = str((original or {}).get("snapshotId") or "")
    ai_summary["snapshotChanged"] = bool(
        original_snapshot and summary.get("snapshotId") != original_snapshot
    )


def _is_stale_running_job(row: Mapping[str, Any]) -> bool:
    """A job still marked ``running`` long past the worker's own time limit: its process is gone.

    The threshold is the one the strategy-evolution list applies -- the worker's limit plus a
    grace window -- because a job inside its limit may simply be slow, and retiring a live job
    would both mislabel it and hand its concurrency slot to a second run.
    """
    if row.get("status") != "running" or not row.get("started_at"):
        return False
    started_at = row["started_at"]
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    limit_seconds = max(120, int(os.getenv("CELERY_TASK_TIME_LIMIT", "3600"))) + 300
    return (datetime.now(timezone.utc) - started_at).total_seconds() > limit_seconds


def _public_job(
    row: Mapping[str, Any] | None, *, include_result: bool = True,
) -> dict[str, Any] | None:
    """The job as the API spells it. Both a stored row and the submit receipt land here.

    ``include_result=False`` is the list view's shape. A finished run's result holds every
    surviving candidate's full metrics and every trial's ledger row, so a page of twenty would
    hand a poller more JSON than it can use -- it lists jobs to find the one that finished, and
    the summary query carries neither. ``hasResult`` says whether there is one to fetch from
    ``GET /jobs/<id>``.

    ``config`` is outside that split on purpose: it is small, and both views need it. A receipt
    has not run yet and a listed row has no result, so for either of them it is the only thing
    saying which market, window, or budget the job was opened with.
    """
    if not row:
        return None
    result = _json(row.get("result"))
    return {
        "jobId": row.get("job_id"),
        "status": row.get("status"),
        "progress": _json(row.get("progress")) or {},
        "config": _job_config(row),
        "result": result if include_result else None,
        "hasResult": bool(result),
        # Already cut to its first line by ``agent_jobs.get_job``, which owns that guarantee for
        # every job kind; repeating it here would be a second place to keep in step.
        "error": row.get("error"),
        "createdAt": row.get("created_at"),
        "startedAt": row.get("started_at"),
        "finishedAt": row.get("finished_at"),
    }


def _job_config(row: Mapping[str, Any]) -> dict[str, Any]:
    """The keys a run config reads, taken from the job's stored request.

    Whitelisted rather than echoed: the stored request is the payload the caller sent plus the
    job store's own ``__userId``, and handing that back would leak the internal channel into a
    public response. A row that carries no request -- an older row, or a stub in a test -- reads
    as an empty config rather than an error, because a job is still worth listing without one.
    """
    request = _json(row.get("request"))
    if not isinstance(request, Mapping):
        return {}
    return {key: request[key] for key in _JOB_CONFIG_KEYS if key in request}


def _json(value: Any) -> Any:
    """A jsonb column, decoded when the driver hands it back as text."""
    return json.loads(value) if isinstance(value, str) else value


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None
