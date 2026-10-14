"""The factor mining engine: one point-in-time panel prepared, then searched.

This layer owns what the rest of the mining pipeline reads. First the panel: bars come through
the Strategy V2 data path -- ``compile_strategy_v2`` for the manifest, ``resolve_candidates``
for the as-of universe, ``fetch_frames`` for the bars -- so mining never grows a second data
layer with its own time semantics. Second the split: the requested range is cut into a search
window and a frozen tail, and the tail is never fetched by the search; the out-of-sample review
asks for it separately, once. Third the loop: every expanded candidate is scored through
``FactorResearchEngine`` and walked down the graded gate ladder, and every one of them -- pass
or reject -- leaves a trial behind, carrying only the metrics a gate reads: the candidate keeps
the whole measurement, but the ledger has one row per evaluation and would otherwise grow the
run's record with the budget. Fourth the review: the frozen tail is fetched once, scored
by that same research engine over its own window, and judged by the sign and selection-bias
gates, which are told the run's whole trial count because that count is what prices the search.

Decisions that are invisible in the signatures. The fetch carries enough history *before* the
search window for the candidates this run will actually evaluate -- the same template
selection, budget and seed the search loop expands -- because the panel is evaluated over the
fetched window and only then cut to the search window; budgeting the DSL's ceiling instead
would cost several times the provider history and, where a policy caps it, silently shorten the
window the run can search. The field panels are pinned to one shared index and column order
with a missing field kept as a NaN column, which is what the evaluator assumes when it aligns a
candidate to ``fields["close"]``.

The search itself holds four things that are easy to lose. The panel carries warmup bars, so
every evaluation re-cuts the frames to the search window; scoring the whole panel would fold
the warmup into the in-sample metrics and, worse, leak into the frozen tail. A candidate is
scored by the same engine that scores the built-in factors, which is why ``FactorResearchEngine
.run`` grew a ``factor_panel`` parameter -- one evaluation path, not two. One candidate's
failure is recorded, never raised: ``ExpressionError`` is not the only way an expression dies
(a scalar ``const`` divides by zero, overflows ``float``, or reaches a panel operator that
cannot take it), so the whole evaluation is guarded and the trial says which stage gave up.
And the expanded seeds are read-only shared data -- the base expression of a template is the
same object as its two smoothing variants' inner argument -- so nothing here writes into them.

The provider falls back to the real services so the search and freeze stages can construct it
without arguments; tests inject fakes. Nothing here opens a connection, reads a clock, or
mocks the network.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.services.backtest_execution import parse_rate
from app.services.backtest_limits import backtest_warmup_calendar_days
from app.services.factor_mining.dsl import MAX_LOOKBACK, PRICE_VOLUME_FIELDS, normalize_expression
from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.factor_mining.gates import (
    GateThresholds,
    GateVerdict,
    net_return_series,
    screen_metrics,
    screen_out_of_sample,
    screen_redundancy,
    screen_stability,
)
from app.services.factor_mining.templates import DEFAULT_BUDGET, MAX_BUDGET, expand_candidates
from app.services.strategy_v2.contract import StrategyV2ContractError, compile_strategy_v2
from app.services.strategy_v2.factor_research import FactorResearchEngine
from app.services.strategy_v2.frequencies import periods_per_year
from app.services.strategy_v2.models import StrategyManifest
from app.services.strategy_v2.service import StrategyV2BacktestService, _enforce_backtest_range
from app.services.strategy_v2.snapshot import canonical_frame_bytes


MINIMUM_SYMBOLS = 3
MINIMUM_WARMUP_BARS = 40
WARMUP_MARGIN_BARS = 5
DEFAULT_FREEZE_RATIO = 0.3
DEFAULT_SEED = 42
PROGRESS_EVERY = 10
INVALID_EXPRESSION_REASON = "factorMining.reject.invalid"


@dataclass(frozen=True)
class MiningConfig:
    """One mining run's inputs. ``from_payload`` is the only place payload keys are read."""

    market: str
    timeframe: str
    start_date: datetime
    end_date: datetime
    source_id: int
    source_name: str = ""
    freeze_ratio: float = DEFAULT_FREEZE_RATIO
    budget: int = DEFAULT_BUDGET
    seed: int = DEFAULT_SEED
    template_ids: tuple[str, ...] = ()
    groups: int = 5
    holding_period: int = 5
    commission: float = 0.0005
    slippage: float = 0.0005
    neutralize_industry: bool = False
    thresholds: GateThresholds = GateThresholds()

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        default_commission: float,
        default_slippage: float,
    ) -> "MiningConfig":
        """Read a run payload into a config.

        Rates are parsed against the backtest defaults the caller resolved, the timeframe is
        normalized to the chart vocabulary the data sources accept, and the end date is pushed
        to the end of its day so the final session is part of the range. A budget above the
        hard cap and a freeze ratio that would leave no usable search window are rejected by
        key rather than clamped, because silently searching a different run is worse.
        """
        budget = _payload_int(payload, "budget", DEFAULT_BUDGET)
        if budget > MAX_BUDGET:
            raise ValueError("factorMining.budgetExceeded")
        return cls(
            market=str(payload.get("market") or ""),
            timeframe=normalize_chart_timeframe(str(payload.get("timeframe") or "1D")),
            start_date=_payload_datetime(payload.get("startDate")),
            end_date=_payload_datetime(payload.get("endDate")).replace(
                hour=23, minute=59, second=59,
            ),
            source_id=_payload_int(payload, "sourceId", 0),
            source_name=str(payload.get("sourceName") or ""),
            freeze_ratio=_freeze_ratio(payload.get("freezeRatio")),
            budget=budget,
            seed=_payload_int(payload, "seed", DEFAULT_SEED),
            template_ids=tuple(str(item) for item in payload.get("templateIds") or ()),
            groups=_payload_int(payload, "groups", 5),
            holding_period=_payload_int(payload, "holdingPeriod", 5),
            commission=_rate(payload.get("commission"), default_commission),
            slippage=_rate(payload.get("slippage"), default_slippage),
            neutralize_industry=bool(payload.get("neutralizeIndustry", False)),
            thresholds=GateThresholds.from_payload(payload.get("thresholds")),
        )


@dataclass(frozen=True)
class PeriodSplit:
    """Search window and frozen tail of one requested range. The tail is never searched."""

    search_start: datetime
    search_end: datetime
    freeze_start: datetime
    freeze_end: datetime


@dataclass(frozen=True)
class PreparedPanel:
    """Everything the search loop needs, plus the data version it was built from."""

    frames: Mapping[str, pd.DataFrame]
    members: list[dict]
    fields: Mapping[str, pd.DataFrame]
    snapshot_id: str
    frequency: str
    warnings: list[str]


def split_periods(config: MiningConfig) -> PeriodSplit:
    """Cut ``config``'s range by traded sessions: the last ``freeze_ratio`` of them freeze.

    Sessions come from the business-day calendar -- this module has no per-market calendar and
    every supported market trades on weekdays -- and the frozen tail starts the day after the
    last search session, so the two windows cannot share a bar.
    """
    ratio = _freeze_ratio(config.freeze_ratio)
    sessions = pd.bdate_range(config.start_date, config.end_date)
    if len(sessions) < 2:
        raise ValueError("factorMining.dateRangeTooShort")
    search_sessions = min(len(sessions) - 1, max(1, int(len(sessions) * (1.0 - ratio))))
    search_end = sessions[search_sessions - 1].to_pydatetime()
    return PeriodSplit(
        search_start=config.start_date,
        search_end=search_end,
        freeze_start=search_end + timedelta(days=1),
        freeze_end=config.end_date,
    )


class MiningDataProvider:
    """Assemble one search-window panel through the Strategy V2 data path."""

    def __init__(
        self,
        *,
        backtest_service: StrategyV2BacktestService | None = None,
    ) -> None:
        self._backtest_service = backtest_service or StrategyV2BacktestService()

    def prepare(self, *, user_id: int, code: str, config: MiningConfig) -> PreparedPanel:
        """Fetch the point-in-time search panel for ``code``'s compiled universe.

        Only the search window, with its warmup lookback, is requested. The frozen tail is the
        out-of-sample review's input and its single read happens there.
        """
        split = split_periods(config)
        return self._prepare_window(
            user_id=user_id, code=code, config=config,
            start_date=split.search_start, end_date=split.search_end,
        )

    def prepare_freeze(self, *, user_id: int, code: str, config: MiningConfig) -> PreparedPanel:
        """Fetch the frozen tail's panel: the same path, the window the search never read.

        The overlap check is structural, not conventional. ``split_periods`` already returns
        disjoint windows, so this can only fire if the split changes meaning -- and a tail that
        overlaps the search window is not a frozen tail. Every candidate would already have been
        fitted to the very bars that are supposed to judge it, so refusing is the honest outcome:
        a review that cannot falsify anything is worse than no review.
        """
        split = split_periods(config)
        if split.freeze_start <= split.search_end:
            raise ValueError("factorMining.searchFreezeOverlap")
        return self._prepare_window(
            user_id=user_id, code=code, config=config,
            start_date=split.freeze_start, end_date=split.freeze_end,
        )

    def _prepare_window(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        start_date: datetime,
        end_date: datetime,
    ) -> PreparedPanel:
        """One window's panel, through the one data path both callers share.

        The window is the only thing that differs between the search panel and the frozen one.
        The manifest, the as-of universe, the candidate-derived warmup, the range check and the
        data fingerprint are applied identically -- and the universe is resolved over the whole
        requested range rather than the window, so both panels carry the same members and the
        same point-in-time membership. A second copy of this method would be a second time
        semantics, and the frozen tail is the last place that may drift.
        """
        manifest = _portfolio_manifest(code)
        candidates, _universe_id = self._backtest_service.resolve_candidates(
            user_id=user_id,
            manifest=manifest,
            start_date=config.start_date,
            end_date=config.end_date,
        )
        if len(candidates) < MINIMUM_SYMBOLS:
            raise ValueError("factorMining.universeTooSmall")
        warmup_bars = _warmup_bars(config)
        fetch_start = _warmup_start(config.timeframe, start_date, warmup_bars)
        # The same range check the factor-research entry runs before fetching, against the
        # window this call actually loads.
        _enforce_backtest_range(
            candidates=candidates,
            timeframe=config.timeframe,
            start_date=start_date,
            end_date=end_date,
            warmup_bars=warmup_bars,
            fetch_start=fetch_start,
        )
        frames, skipped = self._backtest_service.fetch_frames(
            candidates,
            config.timeframe,
            fetch_start,
            end_date,
        )
        if not frames:
            raise ValueError("factorMining.noMarketData")
        # Fetching drops members non-deterministically, and a cross-section of one or two
        # symbols cannot be grouped, ranked or correlated -- so a degraded panel fails here
        # instead of producing trials that dilute the selection-bias correction.
        if len(frames) < MINIMUM_SYMBOLS:
            raise ValueError("factorMining.usableUniverseTooSmall")
        return PreparedPanel(
            frames=frames,
            members=list(candidates),
            fields=_field_panels(frames),
            snapshot_id=_data_digest(frames),
            frequency=config.timeframe,
            warnings=sorted(_skipped_warning(item) for item in skipped),
        )


class FactorMiningEngine:
    """Expand the templates, score every candidate, keep the ledger.

    Determinism is a contract, not a property: the same panel plus the same
    ``(template_ids, budget, seed)`` produce the same trials and the same candidates in the
    same order, because the template expansion is seeded, the gate ladder is pure, and nothing
    here iterates a set.
    """

    def __init__(
        self,
        *,
        data_provider: MiningDataProvider,
        on_progress: Callable[[dict], None] | None = None,
    ) -> None:
        self._data_provider = data_provider
        self._on_progress = on_progress

    def run(self, *, user_id: int, code: str, config: MiningConfig) -> dict[str, Any]:
        """Search ``code``'s universe over ``config`` and return the run record.

        ``candidates`` are the survivors of the ladder, in expansion order; ``trials`` are every
        candidate the run evaluated, rejected ones included, because that count is what the
        deflated-Sharpe benchmark needs to price the search itself.
        """
        panel = self._data_provider.prepare(user_id=user_id, code=code, config=config)
        split = split_periods(config)
        # The provider owns the field panels: they are pinned to one shared index and column
        # order, which is what keeps a candidate's dates and symbols aligned with
        # ``fields["close"]``. Recomputing them here would silently paper over a provider that
        # handed out nothing.
        evaluator = ExpressionEvaluator(panel.fields)
        seeds = expand_candidates(
            template_ids=config.template_ids or None,
            budget=config.budget,
            seed=config.seed,
        )
        members = _resolved_members(panel.members)
        trials: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for evaluated, seed in enumerate(seeds, start=1):
            verdict, metrics = self._evaluate(
                seed.expression, evaluator, config, split, panel, members,
            )
            trials.append({
                "fingerprint": seed.fingerprint,
                "stage": verdict.stage,
                "verdict": "pass" if verdict.passed else "reject",
                "reason": verdict.reason,
                "metrics": _trial_metrics(metrics),
            })
            if verdict.passed:
                candidates.append({
                    "expression": seed.expression,
                    "fingerprint": seed.fingerprint,
                    "origin": seed.origin,
                    "template_id": seed.template_id,
                    "status": "search",
                    "reason": "",
                    "metrics": metrics,
                    "outOfSample": {},
                })
            self._progress(evaluated, len(seeds))
        return {
            "config": self._config_payload(config),
            "split": self._split_payload(split),
            "trials": trials,
            "candidates": candidates,
            "summary": self._summary(config, panel, trials, candidates),
        }

    def review_frozen(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        survivors: Sequence[Mapping[str, Any]],
        trials_count: int,
    ) -> list[dict[str, Any]]:
        """Judge every survivor on the frozen tail: the one read of the out-of-sample segment.

        The frozen panel is fetched here and nowhere else -- the search's own provider call stops
        at the search end -- so no candidate can have been shaped by the bars that judge it.
        ``trials_count`` is the run's *whole* ledger, rejected candidates included: it is the
        deflated-Sharpe denominator, and pricing the search with the survivor count instead would
        understate exactly the bias the correction exists for. Nothing is reviewed when nothing
        survived, so a search that kept no candidate does not open the tail at all.

        Every survivor is reported before it is scored. That report is where ``agent_jobs``
        checks for cancellation, and it is unmodded rather than paced like the search's: a
        review that never reported would let a cancelled job keep writing candidates into its
        run and close it as a success, so the two records would disagree about one event.
        """
        if not survivors:
            return []
        panel = self._data_provider.prepare_freeze(user_id=user_id, code=code, config=config)
        split = split_periods(config)
        # Same contract as the search: the provider owns the field panels, and the review reads
        # them rather than rebuilding anything it was handed.
        evaluator = ExpressionEvaluator(panel.fields)
        members = _resolved_members(panel.members)
        reviewed: list[dict[str, Any]] = []
        for evaluated, survivor in enumerate(survivors, start=1):
            self._report({"stage": "review", "evaluated": evaluated, "total": len(survivors)})
            reviewed.append(
                self._review(survivor, evaluator, config, split, panel, members, trials_count)
            )
        return reviewed

    def _evaluate(
        self,
        expression: Mapping[str, Any],
        evaluator: ExpressionEvaluator,
        config: MiningConfig,
        split: PeriodSplit,
        panel: PreparedPanel,
        members: Sequence[Mapping[str, Any]],
    ) -> tuple[GateVerdict, dict[str, Any]]:
        """Score one candidate down the L1-L4 ladder: a verdict and the metrics behind it.

        The whole body sits under one guard because ``ExpressionError`` is not the only way a
        candidate dies. The DSL validates shape and ranges, not values: ``div`` of two scalar
        ``const`` nodes raises ``ZeroDivisionError``, a ``const`` big enough to overflow
        ``float()`` raises ``OverflowError``, and a scalar handed to any operator that expects a
        panel raises a bare ``AttributeError``. A template-authored expression that does any of
        those is an invalid candidate, and one invalid candidate must not end the run.

        A rejected candidate still returns whatever metrics were readable, so the ledger keeps
        the fold signs L3 reads alongside everything the other gates read.
        """
        try:
            normalize_expression(expression)
            candidate_panel = evaluator.evaluate(expression)
            metrics = _metrics_payload(
                self._research(config, split, panel, members, candidate_panel), config,
            )
        except Exception:
            # v1 has no per-candidate error budget: an unreadable candidate is rejected, and
            # the count of them is visible because each one still writes a trial.
            return GateVerdict(passed=False, stage="L1", reason=INVALID_EXPRESSION_REASON), {}
        for screen, payload in (
            (screen_metrics, metrics),
            (screen_stability, metrics),
            (screen_redundancy, self._correlations(metrics)),
        ):
            verdict = screen(payload, config.thresholds)
            if not verdict.passed:
                return verdict, metrics
        return GateVerdict(passed=True, stage="L4", reason=""), metrics

    def _review(
        self,
        survivor: Mapping[str, Any],
        evaluator: ExpressionEvaluator,
        config: MiningConfig,
        split: PeriodSplit,
        panel: PreparedPanel,
        members: Sequence[Mapping[str, Any]],
        trials_count: int,
    ) -> dict[str, Any]:
        """L5 on one survivor: the frozen metrics, then the sign and selection-bias gates.

        The guard mirrors ``_evaluate``'s, and for the same reasons: the frozen panel is a second
        evaluation of an expression that was only ever proven on the search window, and a
        candidate that cannot be scored there is rejected rather than allowed to end the review.
        ``outOfSample`` stays empty in that case -- there is no measurement to report -- and the
        survivor's own fields are copied, not rewritten, so the caller's run record is never
        edited behind its back. The frozen result is read through the same missing-key discipline
        as the search segment: an absent IC series or long/short curve is an upstream bug, and it
        surfaces as this candidate's invalid verdict instead of a silently weaker gate.
        """
        try:
            normalize_expression(survivor["expression"])
            candidate_panel = evaluator.evaluate(survivor["expression"])
            out_of_sample = _metrics_payload(
                self._research_window(
                    config, panel, members, candidate_panel,
                    split.freeze_start, split.freeze_end,
                ),
                config,
            )
            verdict = screen_out_of_sample(
                is_metrics=survivor["metrics"],
                oos_metrics=out_of_sample,
                returns=net_return_series(out_of_sample["longShortCurve"]),
                trials=trials_count,
                thresholds=config.thresholds,
            )
        except Exception:
            return {
                **survivor,
                "status": "rejected",
                "reason": INVALID_EXPRESSION_REASON,
                "outOfSample": {},
            }
        return {
            **survivor,
            "status": "frozen" if verdict.passed else "rejected",
            "reason": verdict.reason,
            "outOfSample": out_of_sample,
        }

    def _research(
        self,
        config: MiningConfig,
        split: PeriodSplit,
        panel: PreparedPanel,
        members: Sequence[Mapping[str, Any]],
        candidate_panel: pd.DataFrame,
    ) -> dict[str, Any]:
        """Score the candidate panel over the search window."""
        return self._research_window(
            config, panel, members, candidate_panel, split.search_start, split.search_end,
        )

    def _research_window(
        self,
        config: MiningConfig,
        panel: PreparedPanel,
        members: Sequence[Mapping[str, Any]],
        candidate_panel: pd.DataFrame,
        start_date: datetime,
        end_date: datetime,
    ) -> dict[str, Any]:
        """Score the candidate panel over one window of the panel's frames.

        ``panel.frames`` reach back before the window by the warmup the candidates need, and
        ``PreparedPanel`` carries no split of its own, so the window is passed on every call. The
        research engine re-cuts the frames to it (``factor_research.py`` builds ``research_index``
        from ``start_date``/``end_date``); nothing here assumes the panel is already one segment,
        because the alternative is the warmup leaking into the in-sample metrics and, on a
        mis-split panel, the frozen tail leaking into the search. The freeze review scores
        through this same call rather than a second one, so both windows are cut and priced
        identically -- the frozen tail is the one measurement that must not differ in kind from
        the search that produced the candidate.

        The annualization follows the run's own timeframe and markets, the way the factor-research
        entry derives it. ICIR scales with ``sqrt(annualization / holding_period)``, so leaving
        the engine's 252-bar default in place would inflate a weekly run's ICIR by about 2.2x and
        quietly relax ``min_icir`` by the same factor -- a bar in one unit and a measurement in
        another is exactly the incomparability reusing this engine is meant to prevent.
        """
        return FactorResearchEngine().run(
            frames=panel.frames,
            factor_panel=candidate_panel,
            factor_id="",
            start_date=start_date,
            end_date=end_date,
            groups=config.groups,
            holding_period=config.holding_period,
            commission=config.commission,
            slippage=config.slippage,
            neutralize_industry=config.neutralize_industry,
            members=members,
            annualization_periods=periods_per_year(
                config.timeframe, {str(member.get("market") or "") for member in members},
            ),
        )

    def _correlations(self, metrics: Mapping[str, Any]) -> dict[str, float]:
        """The candidate's measured correlation against each built-in factor that was scored.

        Read out of the correlation study the research engine already ran, which puts the
        selected factor first and every built-in that had enough overlapping observations after
        it. A built-in the study could not measure is absent rather than zero: ``screen_
        redundancy`` filters to finite values itself, and a zero this method invented would read
        as a measurement that cleared a duplicate. An unreadable study yields an empty mapping,
        which the gate rejects as unverifiable -- the gate owns that decision, not this method.

        The selected factor is named ``"expression"``, and the guard against reading the row
        below it as the candidate's own is deliberate: the study only lists a factor it could
        compare, so the candidate's row can be missing while the matrix still holds built-ins.
        Trimming the names list by position would then measure the candidate against a built-in's
        correlations -- one factor short, and silently.
        """
        block = metrics.get("factorCorrelation") or {}
        names = list(block.get("factors") or ())
        matrix = list(block.get("matrix") or ())
        if not names or not matrix or names[0] != "expression":
            return {}
        return {
            name: value
            for name, value in zip(names[1:], matrix[0][1:])
        }

    def _progress(self, evaluated: int, total: int) -> None:
        """Report every ``PROGRESS_EVERY``th candidate, so a long run is not a black box."""
        if evaluated % PROGRESS_EVERY == 0:
            self._report({"stage": "search", "evaluated": evaluated, "total": total})

    def _report(self, event: dict) -> None:
        """Hand one progress event to the job's callback; without one there is nothing to report."""
        if self._on_progress:
            self._on_progress(event)

    @staticmethod
    def _config_payload(config: MiningConfig) -> dict[str, Any]:
        """The run's config, with its dates spelled the way ``split`` spells its own.

        ``asdict`` keeps native ``datetime`` objects, and the durable job store renders what
        ``json`` cannot with ``default=str`` -- ``"2024-01-01 00:00:00"``, which a browser's
        ``new Date()`` reads as Invalid Date, inside the same response whose ``split`` block is
        ISO-8601. Both blocks leave through one spelling, written here rather than patched at
        the exit, so no serialization path has to know about the difference.
        """
        payload = asdict(config)
        for key in ("start_date", "end_date"):
            payload[key] = payload[key].isoformat()
        return payload

    @staticmethod
    def _split_payload(split: PeriodSplit) -> dict[str, str]:
        return {
            "searchStart": split.search_start.isoformat(),
            "searchEnd": split.search_end.isoformat(),
            "freezeStart": split.freeze_start.isoformat(),
            "freezeEnd": split.freeze_end.isoformat(),
        }

    @staticmethod
    def _summary(
        config: MiningConfig,
        panel: PreparedPanel,
        trials: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Run totals plus the snapshot the search read and the costs it priced in.

        The counts are what a frozen run is audited against, and the execution assumptions
        repeat what was actually passed to the research engine rather than the engine's own
        defaults, so a stored run explains its own numbers.
        """
        return {
            "snapshotId": panel.snapshot_id,
            "warnings": list(panel.warnings),
            "trials": len(trials),
            "candidates": len(candidates),
            # Zero here, and only here: the search cannot know how many survivors hold up out of
            # sample. The review runs after this record is persisted, so the caller that owns the
            # run writes the count back from the statuses it gets from ``review_frozen``.
            "frozen": 0,
            "executionAssumptions": {
                "commission": config.commission,
                "slippage": config.slippage,
                "groups": config.groups,
                "holdingPeriod": config.holding_period,
                "neutralizeIndustry": config.neutralize_industry,
            },
        }


def _resolved_members(
    members: Sequence[Mapping[str, Any]],
) -> Sequence[Mapping[str, Any]]:
    """The members the research engine can apply a point-in-time universe from.

    ``_membership_panel`` matches members on ``member["key"]``, the ``"market:symbol"`` handle
    ``StrategyV2BacktestService.resolve_candidates`` always injects. Members without one resolve
    to no active symbol at all, which reads as an empty cross-section on every date and turns
    every candidate into an inconclusive rejection, so they are dropped here instead. That
    leaves the empty sequence, which the research engine documents as "no membership
    restrictions" -- the honest reading of members that never carried a universe mapping.
    """
    if any(member.get("key") for member in members):
        return members
    return ()


def _metrics_payload(result: Mapping[str, Any], config: MiningConfig) -> dict[str, Any]:
    """The research engine's verdict on a candidate, as the gate ladder and the ledger read it.

    Two additions, both required. ``foldIcSigns`` is L3's fold-consistency input and only the
    search engine can write it: the research result has no folds, and the gate reads a missing
    series as "not evaluated" and skips the check, so a run that forgot it would quietly lose
    the one check that catches a factor whose edge lives in a single stretch of the window. The
    execution assumptions are re-stamped with the costs this run actually passed in rather than
    the research engine's defaults, so a stored trial explains its own numbers -- and they are
    spread over the engine's own block rather than written beside it, because that block is where
    the signal, entry, exit and annualization declarations live, and a stored candidate without
    them cannot say what it measured.
    """
    return {
        **result,
        "foldIcSigns": _fold_ic_signs(result.get("icSeries")),
        "executionAssumptions": {
            **result.get("executionAssumptions", {}),
            "commission": config.commission,
            "slippage": config.slippage,
        },
    }


# What a stored trial keeps of its metrics. The four screens are their only reader, and this is
# the set of keys they read -- plus the execution assumptions, which are the trial's own account
# of how it was priced. Everything else (the IC series, the group and long/short curves, the
# per-date cross-sections, the correlation matrix) stays on the candidate: a trial row exists for
# every candidate the run evaluated, so it is the trial ledger that makes a run's record grow
# with the budget. At 200 symbols one untrimmed trial is 0.78 MB, and a budget of 400 would
# persist a third of a gigabyte per run, three times over.
_TRIAL_METRIC_KEYS = (
    "coverage", "rankIc", "icir", "foldIcSigns", "averageTurnover", "stability",
    "executionAssumptions",
)


def _trial_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """One trial's metrics, cut to what the gates read off a ledger row.

    ``icSeries`` becomes its length under ``icObservations``: L2 asks how many observations there
    were, never what they were. That is the one key whose shape differs from the candidate's copy
    of the same measurement, and ``screen_metrics`` reads both spellings.
    """
    trimmed = {key: metrics[key] for key in _TRIAL_METRIC_KEYS if key in metrics}
    trimmed["icObservations"] = len(metrics.get("icSeries") or ())
    return trimmed


def _fold_ic_signs(ic_series: Any) -> list[float]:
    """Signs of the four equal folds of the IC series: L3's fold-consistency input.

    An absent series is an error rather than an empty result. The gate reads a series shorter
    than two entries as "not evaluated" and skips the fold check, so returning empty here would
    turn "the research engine handed back no IC series" into a silently weaker gate -- the
    candidate would be screened on everything except fold consistency and nobody would see it.
    Raising keeps the candidate-level guard's verdict (``factorMining.reject.invalid``) honest.

    An IC series with fewer observations than folds is split into that many folds rather than
    padded, so no fold is ever empty. A fold whose mean reads exactly zero contributes a
    neutral sign rather than a vote.
    """
    if ic_series is None:
        raise ValueError("factorMining.missingIcSeries")
    values = [float(item["value"]) for item in ic_series if "value" in item]
    if not values:
        raise ValueError("factorMining.missingIcSeries")
    size = math.ceil(len(values) / min(4, len(values)))
    signs: list[float] = []
    for start in range(0, len(values), size):
        mean = statistics.fmean(values[start:start + size])
        signs.append(float((mean > 0) - (mean < 0)))
    return signs



def _rate(value: Any, default: float) -> float:
    """Read a commission or slippage rate; one that cannot be read is refused by key.

    ``parse_rate`` converts through ``float()``, so a 400-digit integer literal overflows
    there instead of yielding a rate. That is the caller's input problem, not a 500.
    """
    try:
        return parse_rate(value, default=default)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("factorMining.invalidParameter") from exc


def _payload_int(payload: Mapping[str, Any], key: str, default: int) -> int:
    """Read an integer payload field; only an absent key falls back to the default.

    ``payload.get(key) or default`` would read an explicit 0 as absent, and a budget of 0 would
    then never reach the validator that owns the ``factorMining.budgetInvalid`` key. A value
    that cannot be read as an integer is refused by key rather than allowed to escape as a bare
    ``TypeError``, ``ValueError`` or ``OverflowError`` (``1e400`` parses to infinity, and
    ``int(inf)`` overflows): the route layer turns those into a 500, and its message is
    the internals of ``int()``, which is neither a closed key nor anything a client should read.
    """
    value = payload.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("factorMining.invalidParameter") from exc


def _payload_datetime(value: Any) -> datetime:
    """Read a payload date. An absent or unreadable one is the caller's contract error."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("factorMining.dateRangeRequired")
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("factorMining.dateRangeRequired") from exc


def _freeze_ratio(value: Any) -> float:
    """Read a freeze ratio. Outside ``(0, 0.5)`` the frozen tail stops being a minority."""
    try:
        ratio = DEFAULT_FREEZE_RATIO if value in (None, "") else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("factorMining.freezeRatioInvalid") from exc
    if not 0.0 < ratio < 0.5:
        raise ValueError("factorMining.freezeRatioInvalid")
    return ratio


def _portfolio_manifest(code: str) -> StrategyManifest:
    """Compile the source, keeping the portfolio-only contract the research entry enforces."""
    manifest = compile_strategy_v2(code).manifest
    if manifest.strategy_type != "portfolio":
        raise StrategyV2ContractError("strategyV2.factorResearchPortfolioOnly")
    return manifest


def _warmup_bars(config: MiningConfig) -> int:
    """Bars of history the panel must carry before the search window.

    The number follows the candidates this run will actually evaluate -- the same template
    selection, budget and seed the search loop expands -- rather than the DSL's ceiling. The
    longest template path is 145 bars, and budgeting the ceiling's 504 would spend 3.5x the
    provider history for it: on a 1095-day daily policy that is the difference between a
    four-month and a one-year search window. The margin covers the bar-to-calendar conversion,
    which can undershoot on intraday timeframes, and per-symbol gaps inside the warmup. The
    floor and ceiling stay as guards for degenerate runs and for templates that grow.
    """
    candidates = expand_candidates(
        template_ids=config.template_ids or None,
        budget=config.budget,
        seed=config.seed,
    )
    longest = max(
        (normalize_expression(candidate.expression).lookback for candidate in candidates),
        default=MAX_LOOKBACK,
    )
    return max(MINIMUM_WARMUP_BARS, min(MAX_LOOKBACK, longest + WARMUP_MARGIN_BARS))


def _warmup_start(frequency: str, search_start: datetime, warmup_bars: int) -> datetime:
    """First fetched bar: ``warmup_bars`` of history before the search window.

    The conversion to calendar days is the shared backtest one, which pads daily bars for
    weekends and holidays. On intraday timeframes it can fall short of ``warmup_bars``; the
    missing bars are then a small share of a window that holds far more of them.
    """
    return search_start - timedelta(days=backtest_warmup_calendar_days(frequency, warmup_bars))


def _field_panels(frames: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Field name -> panel over every symbol, all panels sharing one index and column order.

    A symbol whose frame does not carry a field keeps a NaN column instead of dropping out of
    that field's cross-section, so a candidate reading it stays comparable across symbols.
    """
    index = pd.Index(sorted(set().union(*(frame.index for frame in frames.values()))))
    columns = list(frames)
    return {
        field: pd.DataFrame({
            key: pd.to_numeric(frame[field], errors="coerce")
            for key, frame in frames.items()
            if field in frame.columns
        }).reindex(index=index, columns=columns)
        for field in PRICE_VOLUME_FIELDS
    }


def _skipped_warning(item: Mapping[str, Any]) -> str:
    """One skipped symbol as a display string, so a dropped member is never silent."""
    return f"{item.get('symbol') or ''}: {item.get('reason') or ''}"


def _data_digest(frames: Mapping[str, pd.DataFrame]) -> str:
    """Content hash of the panel: every symbol's canonical OHLCV bytes, symbol names included.

    Hashing per symbol is what makes this a fingerprint of the data. The shared canonical
    encoder reads flat OHLCV columns only, so handing it a merged multi-symbol panel hashes the
    calendar alone and reports two differently-priced runs as the same dataset. ``dropna`` is
    required, not cosmetic: that encoder refuses NaN, and one symbol with a missing bar would
    otherwise fail the run. Dropping rows stays deterministic, so the digest still moves when
    the data does. v1 keeps this fingerprint only -- there is no replayable copy of the panel.
    """
    digest = hashlib.sha256()
    for symbol in sorted(frames):
        digest.update(symbol.encode("utf-8"))
        digest.update(canonical_frame_bytes(frames[symbol].dropna()))
    return digest.hexdigest()
