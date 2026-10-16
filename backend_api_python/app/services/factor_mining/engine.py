"""The factor mining engine: one point-in-time panel prepared, then searched.

This layer owns what the rest of the mining pipeline reads. First the panel: bars come through
the Strategy V2 data path -- ``compile_strategy_v2`` for the manifest, ``resolve_candidates``
for the as-of universe, ``fetch_frames`` for the bars -- so mining never grows a second data
layer with its own time semantics, and the market's own non-price fields are assembled onto
those bars over the same window (``market_fields``): a derivative column is indexed by the
instant its value became known, so a bar reads the last of them published at or before it.
Second the split: the requested range is cut into a search
window and a frozen tail, and the tail is never fetched by the search; the out-of-sample review
asks for it separately, once. Third the loop: every candidate the source produces -- the
template expansion, or, in evolution, that expansion plus the stream bred from it -- is scored
through ``FactorResearchEngine`` and walked down the graded gate ladder, and every one of them
-- pass or reject -- leaves a trial behind, carrying only the metrics a gate reads: the
candidate keeps the whole measurement, but the ledger has one row per evaluation and would
otherwise grow the run's record with the budget. Evolution's first generation is scored before
it is bred from, and only what the source produces is a candidate at all: a draw evolution
refused, or dropped as a repeat, is never scored and never written down. Fourth the
review: the frozen tail is fetched once, scored by that same research engine over its own
window, and judged by the sign and selection-bias
gates, which are told the run's whole trial count because that count is what prices the search.
Fifth the increment: a survivor that cleared the tail is asked, last of all, what it still
predicts once the caller's own frozen library is taken out of it. That reading is the search
window's -- the run assembles its panels while it holds them -- because an increment is a
search-period judgment and the tail is the evidence that judges the candidate, not evidence for
it.

Decisions that are invisible in the signatures. The fetch carries enough history *before* the
search window for the candidates the mode can know about, because the panel is evaluated over
the fetched window and only then cut to the search window. Enumeration can measure its templates
before it expands them and asks for exactly what they need; evolution cannot -- a crossover can
graft a 252-bar window onto any path -- so it takes the DSL's whole ceiling, which costs
provider history on every evolved run and, where a policy caps the fetched range, shortens the
window the run can search. The field panels are pinned to one shared index and column order
with a missing field kept as a NaN column, they carry the run's whole field set
(``fields_for(market)``, derivative fields included where the market has them), and the data
fingerprint covers every one of those columns -- which is what the evaluator assumes when it
aligns a candidate to ``fields["close"]``.

The search itself holds four things that are easy to lose. The panel carries warmup bars, so
every evaluation re-cuts the frames to the search window; scoring the whole panel would fold
the warmup into the in-sample metrics and, worse, leak into the frozen tail. A candidate is
scored by the same engine that scores the built-in factors, which is why ``FactorResearchEngine
.run`` grew a ``factor_panel`` parameter -- one evaluation path, not two. One candidate's
failure is recorded, never raised: ``ExpressionError`` is not the only way an expression dies
(a scalar ``const`` divides by zero, overflows ``float``, or reaches a panel operator that
cannot take it), so the whole evaluation is guarded and the trial says which stage gave up.
And the candidates are read-only shared data -- the base expression of a template is the
same object as its two smoothing variants' inner argument, and a bred individual shares every
subtree its parents did not rewrite -- so nothing here writes into one.

The provider falls back to the real services so the search and freeze stages can construct it
without arguments; tests inject fakes. Nothing here opens a connection, reads a clock, or
mocks the network.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import statistics
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.services.backtest_execution import parse_rate
from app.services.backtest_limits import backtest_warmup_calendar_days
from app.services.factor_mining import market_fields
from app.services.factor_mining.dsl import (
    MAX_LOOKBACK,
    PRICE_VOLUME_FIELDS,
    ExpressionError,
    fields_for,
    normalize_expression,
)
from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.factor_mining.evolution import evolve
from app.services.factor_mining.gates import (
    POOL_NAMESPACE,
    GateThresholds,
    GateVerdict,
    net_return_series,
    screen_metrics,
    screen_out_of_sample,
    screen_redundancy,
    screen_stability,
)
from app.services.factor_mining.incremental import (
    UNMEASURED_REASON,
    PoolGain,
    residual_ic,
)
from app.services.factor_mining.pool import (
    DEFAULT_POOL_LIMIT,
    load_pool,
    pool_panels,
)
from app.services.factor_mining.templates import (
    DEFAULT_BUDGET,
    MAX_BUDGET,
    CandidateSeed,
    expand_candidates,
)
from app.services.factors import compute_factor, list_factors
from app.services.strategy_v2.contract import StrategyV2ContractError, compile_strategy_v2
from app.services.strategy_v2.factor_research import FactorResearchEngine
from app.services.strategy_v2.frequencies import periods_per_year
from app.services.strategy_v2.models import StrategyManifest
from app.services.strategy_v2.service import StrategyV2BacktestService, _enforce_backtest_range
from app.services.strategy_v2.snapshot import SNAPSHOT_COLUMNS, canonical_frame_bytes


MINIMUM_SYMBOLS = 3
MINIMUM_WARMUP_BARS = 40
WARMUP_MARGIN_BARS = 5
DEFAULT_FREEZE_RATIO = 0.3
DEFAULT_SEED = 42
PROGRESS_EVERY = 10
INVALID_EXPRESSION_REASON = "factorMining.reject.invalid"

# The comparison set a search feeds the de-duplication gate, and the two caps that bound its
# cost. Both bounds are explicit because both are paid for: every name costs one point-in-time
# panel rebuilt once per run -- the registry answers for one frame at a time, so a panel of
# values is one call per symbol and cross-section -- and one cross-sectional Spearman per
# candidate. The built-in cap is the registry's own size, which is what the L4 contract promises
# the comparison covers.
DEFAULT_BUILTIN_COMPARISON_LIMIT = 61

# A pooled candidate is namespaced, because a fingerprint is not a name a reader can tell apart
# from a registry factor id, and the gate reading the two sections treats them differently. The
# prefix itself is ``gates.POOL_NAMESPACE``, defined beside that gate so the two cannot drift.
# shortcut: two members whose fingerprints share an 8-character prefix would merge into one
# comparison column, which the study's name de-duplication would then do silently. At the pool's
# cap of 20 that is a one-in-45-million draw; widen the prefix if the cap ever grows past a few
# hundred.
POOL_NAME_LENGTH = 8

SEARCH_MODE_ENUMERATION = "enumeration"
SEARCH_MODE_EVOLUTION = "evolution"
_SEARCH_MODES = (SEARCH_MODE_ENUMERATION, SEARCH_MODE_EVOLUTION)

# The evolution search's own defaults. A run that breeds names all three in its payload -- the
# boundary refuses one that does not -- and these are what a config built in process starts from.
# They live on the config's fields and nowhere else: a second copy here would be a second answer
# to what a stored run's config says it searched.
DEFAULT_EVOLUTION_POPULATION = 32
DEFAULT_EVOLUTION_GENERATIONS = 20
DEFAULT_EVOLUTION_ELITE_RATIO = 0.25

# What evolution ranks a generation by: a metric every evaluated individual already carries, so
# the fitness callback never computes anything and the evolution module never reads data.
FITNESS_METRIC = "icir"


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
    # The mode the run was submitted with. The engine is handed it as an argument rather than
    # reading it here -- the candidate source is the engine's to choose -- and the caller that
    # owns this config is the one that passes it, including to the frozen review.
    search_mode: str = SEARCH_MODE_ENUMERATION
    population: int = DEFAULT_EVOLUTION_POPULATION
    generations: int = DEFAULT_EVOLUTION_GENERATIONS
    elite_ratio: float = DEFAULT_EVOLUTION_ELITE_RATIO

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
        key rather than clamped, because silently searching a different run is worse. The search
        mode's own settings are read by the same rule and are required of a run that breeds --
        see ``_evolution_settings``.
        """
        budget = _payload_int(payload, "budget", DEFAULT_BUDGET)
        if budget > MAX_BUDGET:
            raise ValueError("factorMining.budgetExceeded")
        search_mode = _search_mode(payload.get("searchMode"))
        population, generations, elite_ratio = _evolution_settings(payload, search_mode)
        return cls(
            market=str(payload.get("market") or ""),
            timeframe=_timeframe(payload.get("timeframe"), "1D"),
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
            search_mode=search_mode,
            population=population,
            generations=generations,
            elite_ratio=elite_ratio,
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


@dataclass(frozen=True)
class ComparisonSet:
    """What one panel's candidates are measured against, and what could not be measured.

    ``factors`` are the registry's own factor ids in registry order and ``pool`` are the
    caller's frozen candidates, namespaced. ``panels`` carries the point-in-time values for
    both: they are built once per run, because the research engine would otherwise rebuild
    every one of them -- the pool's expressions and every registry factor -- for every
    candidate it scores. ``skipped`` is what the pool could not rebuild, reported rather than
    dropped, since a comparison set one member short looks exactly like a pool that never had
    that member.
    """

    factors: tuple[str, ...] = ()
    pool: tuple[str, ...] = ()
    panels: Mapping[str, pd.DataFrame] = field(default_factory=dict)
    skipped: tuple[Mapping[str, str], ...] = ()

    def names(self) -> tuple[str, ...]:
        """Every name this set compares against, built-ins first."""
        return (*self.factors, *self.pool)

    def report(self) -> dict[str, Any]:
        """The set as a stored run writes it down, so a reader can tell what was compared."""
        return {
            "builtin": list(self.factors),
            "pool": list(self.pool),
            "skipped": [dict(item) for item in self.skipped],
        }


@dataclass(frozen=True)
class PoolIncrementEvidence:
    """L6's inputs: the search window one candidate's increment is measured on.

    Assembled by the search, out of the panel and the comparison set it already holds, and read by
    the review -- which is the other side of the split: the review's own panel is the frozen tail,
    and a tail is not evidence for a search-period judgment. It carries the pool's half of the
    comparison and not the registry's, because the question L6 asks is what the candidate adds to
    the *caller's* library, and the frozen tail is not in this panel at all: the search's fetch
    stops at ``search_end``.

    ``members`` are the rebuilt pool members by comparison name (the mapping
    :func:`~app.services.factor_mining.incremental.residual_ic` orthogonalizes against), and
    ``skipped`` is what the pool could not rebuild -- members the measurement never sees, so the
    review is the only place that knows the pool was bigger than what was compared.
    """

    fields: Mapping[str, pd.DataFrame]
    returns: pd.DataFrame
    members: Mapping[str, pd.DataFrame]
    skipped: tuple[str, ...] = ()


def _pool_name(fingerprint: Any) -> str:
    """The comparison name a pool member's fingerprint travels under.

    One spelling, because the name is how three places refer to the same member: the assembly that
    builds its panel, the de-duplication gate that correlates against it, and the increment's
    record that reports it as compared or as skipped. Two spellings would let a member be
    measured under one name and reported under another, which reads as a member that was never
    asked about.
    """
    return f"{POOL_NAMESPACE}{str(fingerprint or '')[:POOL_NAME_LENGTH]}"


def _computable_factor_ids(frames: Mapping[str, pd.DataFrame]) -> list[str]:
    """The registry factor ids this panel can measure, in the registry's deterministic order.

    "Can measure" is the registry's own contract rather than a curated list: every factor
    declares the fields it reads, and a panel that does not carry one of them has nothing to
    compute it from -- a price-volume panel carries no fundamentals, so the fundamental factors
    are absent from the comparison instead of present as a column of NaNs. The order is
    ``list_factors``'s, which is sorted: the correlation matrix's columns follow it, and a set
    iterated in hash order would move them from run to run.
    """
    available: set[str] = set()
    for frame in frames.values():
        available.update(str(column) for column in frame.columns)
    return [
        str(item["factor_id"])
        for item in list_factors()
        if set(item.get("required_fields") or ()) <= available
    ]


def _point_in_time_panels(
    frames: Mapping[str, pd.DataFrame],
    dates: Sequence[pd.Timestamp],
    factor_ids: Sequence[str],
) -> dict[str, pd.DataFrame]:
    """One panel per registry factor: its value at each cross-section, from the bars up to it.

    The registry computes a factor for a frame -- the value at that frame's last bar -- so a
    panel of values is one call per symbol and date, and each call is handed only the bars at or
    before its own date. That is the point of doing it here: a comparison value correlated with a
    candidate has to be knowable on the date it is measured, or the de-duplication gate would
    decide with the future. It is also why the caller caps the set: the work is symbols x dates
    x factors, once per run rather than once per candidate.

    ``frames`` are the research window's own prepared frames -- sorted, one timezone, cut at the
    window's end -- because a panel built against a differently normalized index would be empty
    on every date and would drop out of the study without a word.

    A call that raises leaves that cell of the panel missing, never a reason to end the run: the
    registry refuses a factor whose frame is too short, and a comparison factor measured over too
    few observations drops out of the study by itself.
    """
    index = pd.Index(dates)
    panels: dict[str, pd.DataFrame] = {}
    for factor_id in factor_ids:
        columns: dict[str, pd.Series] = {}
        for symbol, frame in frames.items():
            values = []
            for timestamp in dates:
                try:
                    values.append(compute_factor(factor_id, frame.loc[:timestamp]))
                except Exception:
                    values.append(float("nan"))
            columns[str(symbol)] = pd.Series(values, index=index, dtype=float)
        panels[factor_id] = pd.DataFrame(columns, index=index)
    return panels


def _comparison_set(
    *,
    panel: PreparedPanel,
    config: MiningConfig,
    user_id: int,
    start_date: datetime,
    end_date: datetime,
    pool_loader: Callable[..., list],
    factor_limit: int = DEFAULT_BUILTIN_COMPARISON_LIMIT,
    pool_limit: int = DEFAULT_POOL_LIMIT,
) -> ComparisonSet:
    """The comparison set one panel and one window are measured against, assembled once.

    Two sources. The registry's factors, filtered to the ones this panel's fields can measure and
    capped, measured point-in-time at the study's own cross-sections -- over the study's own
    prepared frames, so an injected panel is aligned to the index the candidate is scored on.
    And the caller's frozen pool, rebuilt from the stored expressions against this panel's field
    panels: the same fields the candidates themselves are scored on, which is what makes a member
    comparable at all.

    ``pool_loader`` is injected rather than called at this level so a run can be exercised
    without a candidate table; the provider passes the real one, which reads the caller's own
    pooled candidates.
    """
    frames, dates = FactorResearchEngine.research_window(
        frames=panel.frames,
        start_date=start_date,
        end_date=end_date,
        holding_period=config.holding_period,
    )
    factor_ids = _computable_factor_ids(frames)[:max(0, int(factor_limit))]
    panels = _point_in_time_panels(frames, dates, factor_ids)
    members = pool_loader(
        user_id, config.market, config.timeframe, limit=max(0, int(pool_limit)),
    )
    rebuilt, skipped = pool_panels(members, panel.fields,
                                   allowed_fields=fields_for(config.market))
    pooled: list[str] = []
    for member in members:
        member_panel = rebuilt.get(member.fingerprint)
        # A row with no fingerprint has no name to carry and no way to be reported as skipped;
        # every insert writes that column, so this drops only a hand-edited row.
        if not member.fingerprint or member_panel is None:
            continue
        name = _pool_name(member.fingerprint)
        pooled.append(name)
        panels[name] = member_panel
    return ComparisonSet(
        factors=tuple(factor_ids),
        pool=tuple(pooled),
        panels=panels,
        skipped=tuple(skipped),
    )


def _pool_increment_evidence(
    *,
    panel: PreparedPanel,
    config: MiningConfig,
    split: PeriodSplit,
    comparison: ComparisonSet,
) -> PoolIncrementEvidence:
    """L6's inputs, cut from the search window the candidates of ``panel`` were scored on.

    The window is ``split``'s search half, and both the dates and the frames are the research
    engine's own prepared ones (:meth:`FactorResearchEngine.research_window`) -- the grid the
    candidate's in-sample IC was read on, and the frames that grid was read off. Taking the frames
    from there rather than from the panel is not a shortcut around them: the research engine builds
    its window by trimming the frames at the window's end (``_prepare_frames``), and the increment
    needs that trim for the same reason it does. A forward return is read *at* a date and takes its
    exit price from bars after it, so a source that over-returns -- one more bar, or the whole tail
    it was not asked for -- would put frozen-tail prices into the last cross-sections of a
    measurement that is supposed to be the search window's alone.

    The returns are the research engine's forward return, spelled the way ``run`` spells it (entry
    at the next bar's open, exit ``holding_period`` bars later) so the raw IC the increment is
    compared against is read on the same side of the trade the candidate's own metrics were. The
    period is clamped once, with ``run``'s own rule, and both the grid and the two shifts are given
    that one value: ``holdingPeriod`` arrives from a payload with no lower bound, and a negative
    one would put the exit *before* the entry -- a reading taken off a series running backwards,
    and a verdict read off it.

    Only the pool's half of ``comparison`` travels: the registry's factors are the comparison L4
    has already made, not the library a candidate is added to.
    """
    holding_period = max(1, int(config.holding_period or 5))
    frames, dates = FactorResearchEngine.research_window(
        frames=panel.frames,
        start_date=split.search_start,
        end_date=split.search_end,
        holding_period=holding_period,
    )
    index = pd.Index(dates)
    opening = FactorResearchEngine._panel(frames, "open")
    returns = (
        opening.shift(-(holding_period + 1)) / opening.shift(-1) - 1.0
    ).reindex(index=index)
    members = {
        name: comparison.panels[name].reindex(index=index)
        for name in comparison.pool
        if name in comparison.panels
    }
    skipped = tuple(
        _pool_name(item.get("fingerprint"))
        for item in comparison.skipped
        if item.get("fingerprint")
    )
    return PoolIncrementEvidence(
        fields=panel.fields, returns=returns, members=members, skipped=skipped,
    )


# The two "nothing was measured" readings the review can reach on its own. Both carry
# ``unmeasured`` and never ``noPool``: ``noPool`` is "this caller's library is empty", which is a
# fact about the library, and the only thing these two know is that no comparison was drawn --
# neither has a pool that could be read and failed to be. An operator sent to the wrong one of the
# two looks for a library in the wrong place. The third state of the flag is the whole reason
# ``PoolGain`` has one: neither reading may retire a candidate.
_NOT_COMPARED = PoolGain(
    measured=False, has_incremental_value=None, residual_ic=None, raw_ic=None,
    used_cross_sections=0, skipped_cross_sections=0, reason=UNMEASURED_REASON,
)
_UNREADABLE_CANDIDATE = PoolGain(
    measured=False, has_incremental_value=None, residual_ic=None, raw_ic=None,
    used_cross_sections=0, skipped_cross_sections=0, reason=UNMEASURED_REASON,
)


def _pool_increment_record(
    gain: PoolGain,
    *,
    compared: Sequence[str],
    skipped: Sequence[str],
    min_abs_rank_ic: float,
) -> dict[str, Any]:
    """One survivor's ``poolIncrement`` block: the reading, the pool behind it, and the verdict.

    Both sizes are written down because they are not the same number. ``poolSize`` is the pool the
    run's own cap and self-exclusion left, and ``compared`` is what a panel could be rebuilt for;
    a member in ``skipped`` never reached the measurement at all, so a reading taken with one
    missing is a reading of a truncated pool. ``complete`` says that outright instead of leaving it
    to whoever counts a list against a number -- a clearance of a pool that was compared by half
    can still be read, but not as if it were the whole library.

    A truncated pool still judges, and that is a decision rather than an oversight: the alternative
    is a stage that goes silent the moment one member's stored expression stops rebuilding, and a
    ``False`` here is decided by members that were measured, not by the ones that were not. What
    may not happen is the truncation going unmentioned, which is what the counts are for.

    ``noPool`` -- "this caller has no library yet" -- is reported only when the pool itself was
    empty. A pool that exists and could not be read (every member in ``skipped``, so the
    measurement is handed an empty mapping) is ``unmeasured``: the two facts send an operator to
    different places, and the size and the skipped names in the same block are what tell them
    apart. ``minAbsRankIc`` is the run's own bar, restated like every other execution assumption
    so a stored reading explains the verdict it produced.
    """
    pool_size = len(compared) + len(skipped)
    reason = gain.reason
    if gain.measured is False and not compared and pool_size:
        reason = UNMEASURED_REASON
    return {
        "poolSize": pool_size,
        "compared": list(compared),
        "skipped": list(skipped),
        "complete": not skipped,
        "hasIncrementalValue": gain.has_incremental_value,
        "reason": reason,
        "residualIc": gain.residual_ic,
        "rawIc": gain.raw_ic,
        "usedCrossSections": gain.used_cross_sections,
        "skippedCrossSections": gain.skipped_cross_sections,
        "minAbsRankIc": min_abs_rank_ic,
    }


def split_periods(config: MiningConfig) -> PeriodSplit:
    """Cut ``config``'s range by the market's own sessions: the last ``freeze_ratio`` of them.

    Sessions come from the market's calendar (``market_fields.sessions``): a market that trades
    every calendar day has ~40% more sessions than a business-day count finds, so counting it on
    the week's calendar puts its frozen tail at the wrong boundary -- and every run is judged on
    exactly that segment. Every other supported market trades on weekdays, which is what this
    counted for all of them before the daily calendar existed. The frozen tail starts the day
    after the last search session, so the two windows cannot share a bar.
    """
    ratio = _freeze_ratio(config.freeze_ratio)
    calendar = market_fields.sessions(config.market, config.start_date, config.end_date)
    if len(calendar) < 2:
        raise ValueError("factorMining.dateRangeTooShort")
    search_sessions = min(len(calendar) - 1, max(1, int(len(calendar) * (1.0 - ratio))))
    search_end = calendar[search_sessions - 1].to_pydatetime()
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
        pool_loader: Callable[..., list] = load_pool,
        comparison_factor_limit: int = DEFAULT_BUILTIN_COMPARISON_LIMIT,
        pool_limit: int = DEFAULT_POOL_LIMIT,
    ) -> None:
        self._backtest_service = backtest_service or StrategyV2BacktestService()
        self._pool_loader = pool_loader
        self._comparison_factor_limit = comparison_factor_limit
        self._pool_limit = pool_limit

    def comparison_set(
        self,
        *,
        panel: PreparedPanel,
        config: MiningConfig,
        user_id: int,
        start_date: datetime,
        end_date: datetime,
    ) -> ComparisonSet:
        """What the candidates of ``panel`` are compared against over one window.

        The window is the caller's because the two panels of a run measure different stretches
        of the same universe: the search compares against what was knowable inside the search
        window, and the frozen review against what was knowable inside the frozen tail. Both
        are built here rather than in one of them, so the two records cannot end up comparing
        against panels prepared under different rules.

        The pool's own caps are the provider's; ``pool_loader`` reads the caller's candidates,
        which is the one call in here that reaches a store.
        """
        return _comparison_set(
            panel=panel,
            config=config,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
            pool_loader=self._pool_loader,
            factor_limit=self._comparison_factor_limit,
            pool_limit=self._pool_limit,
        )

    def prepare(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        search_mode: str = SEARCH_MODE_ENUMERATION,
    ) -> PreparedPanel:
        """Fetch the point-in-time search panel for ``code``'s compiled universe.

        Only the search window, with its warmup lookback, is requested. The frozen tail is the
        out-of-sample review's input and its single read happens there. The mode travels with the
        request because it decides how much history the window must carry -- see ``_warmup_bars``.
        """
        split = split_periods(config)
        return self._prepare_window(
            user_id=user_id, code=code, config=config, search_mode=search_mode,
            start_date=split.search_start, end_date=split.search_end,
        )

    def prepare_freeze(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        search_mode: str = SEARCH_MODE_ENUMERATION,
    ) -> PreparedPanel:
        """Fetch the frozen tail's panel: the same path, the window the search never read.

        The overlap check is structural, not conventional. ``split_periods`` already returns
        disjoint windows, so this can only fire if the split changes meaning -- and a tail that
        overlaps the search window is not a frozen tail. Every candidate would already have been
        fitted to the very bars that are supposed to judge it, so refusing is the honest outcome:
        a review that cannot falsify anything is worse than no review. The mode arrives from the
        search that produced the survivors, because it is their lookback the tail must reach back
        for, and an evolved survivor's is the ceiling.
        """
        split = split_periods(config)
        if split.freeze_start <= split.search_end:
            raise ValueError("factorMining.searchFreezeOverlap")
        return self._prepare_window(
            user_id=user_id, code=code, config=config, search_mode=search_mode,
            start_date=split.freeze_start, end_date=split.freeze_end,
        )

    def _prepare_window(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        search_mode: str,
        start_date: datetime,
        end_date: datetime,
    ) -> PreparedPanel:
        """One window's panel, through the one data path both callers share.

        The window is the only thing that differs between the search panel and the frozen one.
        The manifest, the as-of universe, the mode's warmup, the range check and the
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
        warmup_bars = _warmup_bars(config, search_mode)
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
        # The market's own fields, aligned to these bars. The window is the one just fetched --
        # the search panel carries the warmup, the frozen panel starts at the tail -- so no
        # series can reach past the panel's own end into another segment's readings.
        frames = market_fields.enrich_frames(
            frames, market=config.market, start=fetch_start, end=end_date,
        )
        return PreparedPanel(
            frames=frames,
            members=list(candidates),
            fields=_field_panels(frames, fields_for(config.market)),
            snapshot_id=_data_digest(frames),
            frequency=config.timeframe,
            warnings=sorted(_skipped_warning(item) for item in skipped),
        )


class FactorMiningEngine:
    """Expand the templates or breed from them, score every candidate, keep the ledger.

    Determinism is a contract, not a property: the same panel plus the same ``(config,
    search_mode)`` produce the same trials and the same candidates in the same order, because the
    template expansion is seeded, the evolution stream draws from ``random.Random(seed)`` alone,
    the gate ladder is pure, and nothing here iterates a set.

    One instance serves one run's two halves: ``run`` assembles the search window's increment
    evidence and ``review_frozen`` reads it, because the review is a later call that no longer
    holds the search panel -- and re-fetching the search window to measure an increment would be
    a second read of data the run already had. An engine that never searched reviews without it,
    which is a review with nothing to measure an increment on rather than a wrong one.
    """

    def __init__(
        self,
        *,
        data_provider: MiningDataProvider,
        on_progress: Callable[[dict], None] | None = None,
    ) -> None:
        self._data_provider = data_provider
        self._on_progress = on_progress
        self._pool_increment: PoolIncrementEvidence | None = None

    def run(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        search_mode: str = SEARCH_MODE_ENUMERATION,
    ) -> dict[str, Any]:
        """Search ``code``'s universe over ``config`` and return the run record.

        ``search_mode`` picks the candidate source, and the mode is the caller's because the
        sources need different things from this method: enumeration expands the templates, while
        evolution needs a ``fitness`` callback that only the engine can build -- it answers with
        the measurement this run wrote for an individual, and the evolution module must not read
        data or compute a metric itself. The mode also travels to the provider, because it decides
        how much history the panel must carry before the search window.

        Both sources are an ``Iterable[CandidateSeed]`` and the loop pulls one individual at a
        time, because evolution consults that callback while it streams: draining the source first
        would leave every generation ranked on nothing.

        ``candidates`` are the survivors of the ladder, in production order; ``trials`` are every
        candidate the run evaluated, rejected ones included, because that count is what the
        deflated-Sharpe benchmark needs to price the search itself. An evolution run evaluates the
        template generation as well as what it breeds, so its ledger is longer than the budget --
        and only what the source produced is in either: a draw evolution refused, or skipped as one
        the run already holds, spends budget and leaves nothing behind.
        """
        if search_mode not in _SEARCH_MODES:
            raise ValueError("factorMining.invalidParameter")
        panel = self._data_provider.prepare(
            user_id=user_id, code=code, config=config, search_mode=search_mode,
        )
        split = split_periods(config)
        # Assembled once for the whole search, not once per candidate: every comparison panel is
        # a point-in-time rebuild, and the pool's members are re-evaluated expressions.
        comparison = self._data_provider.comparison_set(
            panel=panel, config=config, user_id=user_id,
            start_date=split.search_start, end_date=split.search_end,
        )
        # L6's inputs, assembled here because this is the last place both are held: the search
        # panel, and the comparison set built over the window it covers. The review runs in a
        # later call, on the frozen tail's panel, and has neither.
        self._pool_increment = _pool_increment_evidence(
            panel=panel, config=config, split=split, comparison=comparison,
        )
        # The provider owns the field panels: they are pinned to one shared index and column
        # order, which is what keeps a candidate's dates and symbols aligned with
        # ``fields["close"]``. Recomputing them here would silently paper over a provider that
        # handed out nothing.
        evaluator = ExpressionEvaluator(panel.fields, allowed_fields=fields_for(config.market))
        members = _resolved_members(panel.members)
        measured: dict[str, float] = {}

        def fitness(expression: Mapping[str, Any]) -> float | None:
            """What this run measured for ``expression``, or ``None`` if it measured nothing.

            The template generation is chained in front of the bred stream, so by the time
            evolution asks about a generation the engine has scored every individual of it -- the
            first generation is ranked rather than bred from blind. ``None`` is for what has no
            measurement to rank on: an individual the DSL refused, one whose evaluation produced
            no finite ICIR, or an expression this run never received. Such an individual can never
            be an elite, but it stays eligible to breed -- an unmeasured individual has not earned
            the label of a bad one either.
            """
            try:
                fingerprint = normalize_expression(
                    expression, allowed_fields=fields_for(config.market)).fingerprint
            except ExpressionError:
                return None
            return measured.get(fingerprint)

        source, total = _candidate_source(config, search_mode, fitness)
        trials: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for evaluated, seed in enumerate(source, start=1):
            verdict, metrics = self._evaluate(
                seed.expression, evaluator, config, split, panel, members, comparison,
            )
            value = metrics.get(FITNESS_METRIC)
            if isinstance(value, float) and math.isfinite(value):
                measured[seed.fingerprint] = value
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
            self._progress(evaluated, total)
        return {
            "config": self._config_payload(config),
            "split": self._split_payload(split),
            "trials": trials,
            "candidates": candidates,
            "summary": self._summary(config, panel, trials, candidates, comparison),
        }

    def review_frozen(
        self,
        *,
        user_id: int,
        code: str,
        config: MiningConfig,
        survivors: Sequence[Mapping[str, Any]],
        trials_count: int,
        search_mode: str = SEARCH_MODE_ENUMERATION,
    ) -> list[dict[str, Any]]:
        """Judge every survivor on the frozen tail: the one read of the out-of-sample segment.

        The frozen panel is fetched here and nowhere else -- the search's own provider call stops
        at the search end -- so no candidate can have been shaped by the bars that judge it.
        ``trials_count`` is the run's *whole* ledger, rejected candidates included: it is the
        deflated-Sharpe denominator, and pricing the search with the survivor count instead would
        understate exactly the bias the correction exists for. Nothing is reviewed when nothing
        survived, so a search that kept no candidate does not open the tail at all. The review
        carries the search's own mode, because the tail reaches back for the survivors' lookback
        and an evolved survivor's is the ceiling.

        Every survivor is reported before it is scored. That report is where ``agent_jobs``
        checks for cancellation, and it is unmodded rather than paced like the search's: a
        review that never reported would let a cancelled job keep writing candidates into its
        run and close it as a success, so the two records would disagree about one event.

        A survivor that clears L5 is then asked L6's question -- what is left of it once the
        caller's pool is taken out -- on the search window this engine's own ``run`` left behind.
        The tail this method just fetched is deliberately not what that reads: the segment that
        judges a candidate cannot also be the segment that prices its value to the pool. The
        increment can only retire a candidate (``False``); an unmeasured one, or none at all
        because no search was run here, leaves the frozen status L5 gave it.

        The mode is refused here by the same rule ``run`` applies, and before the no-survivors
        shortcut: both entries read one config, and a mode one of them refused cannot be one the
        other quietly reads as enumeration.
        """
        if search_mode not in _SEARCH_MODES:
            raise ValueError("factorMining.invalidParameter")
        if not survivors:
            return []
        panel = self._data_provider.prepare_freeze(
            user_id=user_id, code=code, config=config, search_mode=search_mode,
        )
        split = split_periods(config)
        # The tail's own comparison set: the same rule as the search's, over the window this
        # panel covers, so a survivor's frozen measurement is priced like its in-sample one.
        comparison = self._data_provider.comparison_set(
            panel=panel, config=config, user_id=user_id,
            start_date=split.freeze_start, end_date=split.freeze_end,
        )
        # Same contract as the search: the provider owns the field panels, and the review reads
        # them rather than rebuilding anything it was handed.
        evaluator = ExpressionEvaluator(panel.fields, allowed_fields=fields_for(config.market))
        members = _resolved_members(panel.members)
        reviewed: list[dict[str, Any]] = []
        for evaluated, survivor in enumerate(survivors, start=1):
            self._report({"stage": "review", "evaluated": evaluated, "total": len(survivors)})
            record = self._review(survivor, evaluator, config, split, panel, members, trials_count,
                                  comparison)
            if record["status"] == "frozen":
                record = self._increment(record, config)
            reviewed.append(record)
        return reviewed

    def _increment(self, record: dict[str, Any], config: MiningConfig) -> dict[str, Any]:
        """L6 on one survivor L5 has cleared: what it adds to the caller's already-frozen pool.

        The reading comes from ``self._pool_increment`` -- the search window's panels -- and not
        from the tail's comparison set two methods up, which is the trap this stage exists beside:
        a survivor's frozen measurement is judged on the tail, and pricing it there too would make
        the segment that decides the outcome also decide the payoff. The panel that travels to the
        measurement is this candidate's own, re-evaluated on the search window's fields, cut to
        the grid the pool members and the returns were built on.

        An unmeasured increment keeps the candidate, which is the opposite of what L4 does with a
        pool section it can neither measure nor clear. The asymmetry is deliberate and is the one
        thing about this stage worth stating twice: at L4 a candidate let through joins the pool
        every later run compares against, and a duplicate there compounds; here a candidate let
        through is one name that already cleared L2-L5, and a reader can retire it later on
        evidence. So only a *measured* ``False`` -- read with ``is False``, never for its
        truthiness, since ``None`` is "not measured" and not "no value" -- retires a candidate,
        and the reason key it retires under is the increment's own.
        """
        evidence = self._pool_increment
        if evidence is None:
            # No search on this engine, so no search window to measure on: nothing was compared
            # against. That is not the empty library -- ``noPool`` would send an operator looking
            # for a library that is there -- and it is not a rejection either: the survivor keeps
            # what L5 gave it.
            gain, compared, skipped = _NOT_COMPARED, (), ()
        else:
            gain = self._pool_gain(record["expression"], evidence, config)
            compared, skipped = tuple(evidence.members), evidence.skipped
        record["outOfSample"]["poolIncrement"] = _pool_increment_record(
            gain,
            compared=compared,
            skipped=skipped,
            min_abs_rank_ic=config.thresholds.min_abs_rank_ic,
        )
        if gain.has_incremental_value is False:
            record["status"] = "rejected"
            record["reason"] = gain.reason
        return record

    @staticmethod
    def _pool_gain(
        expression: Mapping[str, Any],
        evidence: PoolIncrementEvidence,
        config: MiningConfig,
    ) -> PoolGain:
        """The increment itself: the candidate's search-window panel against the pool's.

        The whole body sits under one guard, for the reason every other evaluation in this module
        does: an expression can die past the DSL on a value the operator layer refuses, and one
        candidate that cannot be measured is a missing measurement -- ``None``, which retires
        nobody -- rather than the end of a review that has already judged the candidates before it.
        The guard covers the measurement and not only the evaluation, because what raises is not
        always the expression: the panels handed to it were built elsewhere, and an exception out
        of here would leave the review -- and, behind it, the job -- rather than one candidate's
        verdict unanswered.
        """
        try:
            panel = ExpressionEvaluator(
                evidence.fields, allowed_fields=fields_for(config.market)).evaluate(expression)
            return residual_ic(
                panel.reindex(index=evidence.returns.index),
                evidence.members,
                evidence.returns,
                min_abs_rank_ic=config.thresholds.min_abs_rank_ic,
            )
        except Exception:
            return _UNREADABLE_CANDIDATE

    def _evaluate(
        self,
        expression: Mapping[str, Any],
        evaluator: ExpressionEvaluator,
        config: MiningConfig,
        split: PeriodSplit,
        panel: PreparedPanel,
        members: Sequence[Mapping[str, Any]],
        comparison: ComparisonSet,
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
            normalize_expression(expression, allowed_fields=fields_for(config.market))
            candidate_panel = evaluator.evaluate(expression)
            metrics = _metrics_payload(
                self._research(config, split, panel, members, candidate_panel, comparison), config,
            )
        except Exception:
            # v1 has no per-candidate error budget: an unreadable candidate is rejected, and
            # the count of them is visible because each one still writes a trial.
            return GateVerdict(passed=False, stage="L1", reason=INVALID_EXPRESSION_REASON), {}
        for screen, payload in (
            (screen_metrics, metrics),
            (screen_stability, metrics),
            (screen_redundancy, self._correlations(metrics, comparison)),
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
        comparison: ComparisonSet,
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
            normalize_expression(survivor["expression"], allowed_fields=fields_for(config.market))
            candidate_panel = evaluator.evaluate(survivor["expression"])
            out_of_sample = _metrics_payload(
                self._research_window(
                    config, panel, members, candidate_panel,
                    split.freeze_start, split.freeze_end, comparison,
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
        comparison: ComparisonSet,
    ) -> dict[str, Any]:
        """Score the candidate panel over the search window."""
        return self._research_window(
            config, panel, members, candidate_panel, split.search_start, split.search_end,
            comparison,
        )

    def _research_window(
        self,
        config: MiningConfig,
        panel: PreparedPanel,
        members: Sequence[Mapping[str, Any]],
        candidate_panel: pd.DataFrame,
        start_date: datetime,
        end_date: datetime,
        comparison: ComparisonSet,
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

        The comparison set travels with the panel as an injected one. An assembled set that came
        back empty is handed over as ``None`` instead, which is the research engine's "use the
        built-in proxies": an empty mapping would leave the de-duplication gate with no finite
        measurement at all, and its fail-closed rule would reject every candidate of the run --
        a search with nothing pooled and no measurable registry factor would then never find
        anything.
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
            comparison_factors=comparison.names() or None,
            comparison_panels=comparison.panels or None,
        )

    def _correlations(self, metrics: Mapping[str, Any],
                      comparison: ComparisonSet | None = None) -> dict[str, float]:
        """The candidate's correlation against every factor it was compared against.

        Read out of the correlation study the research engine already ran, which puts the
        selected factor first and every factor it could compare after it. The names are the
        comparison set's own, so a pooled member arrives carrying ``POOL_NAMESPACE`` and the gate
        below partitions on that prefix. A name the study could not measure is absent from the
        study, and ``comparison`` is where it is recovered: every name that was *asked about* is
        put in the mapping, an unmeasured one as a NaN -- including the pool members that could
        not be rebuilt at all, which the assembly reports as skips rather than comparison names
        and which travel here under the name the member would have had. That presence is the
        whole point -- the gate tells "never asked about" from "asked about and unmeasurable" by
        the name being there, and fails closed on the second. A zero invented here would instead
        read as a measurement that cleared a duplicate, and dropping the name would read as a pool
        that never held it. A caller passing no ``comparison`` (a unit test of this method, or the
        proxies the study falls back to) gets exactly what the study reported.

        The selected factor is named ``"expression"``, and the guard against reading the row
        below it as the candidate's own is deliberate: the study only lists a factor it could
        compare, so the candidate's row can be missing while the matrix still holds comparison
        factors. Trimming the names list by position would then measure the candidate against
        another factor's correlations -- one factor short, and silently.
        """
        block = metrics.get("factorCorrelation") or {}
        names = list(block.get("factors") or ())
        matrix = list(block.get("matrix") or ())
        if not names or not matrix or names[0] != "expression":
            return {}
        measured = {
            name: value
            for name, value in zip(names[1:], matrix[0][1:])
        }
        if comparison is not None:
            # A member the pool could not rebuild is a skip, not a comparison name, so ``names()``
            # does not carry it -- the assembly drops a row with no fingerprint for the same
            # reason it is dropped here: it has no name to be asked about under.
            skipped = [
                _pool_name(item.get("fingerprint"))
                for item in comparison.skipped if item.get("fingerprint")
            ]
            for name in (*comparison.names(), *skipped):
                measured.setdefault(name, float("nan"))
        return measured
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
        comparison: ComparisonSet,
    ) -> dict[str, Any]:
        """Run totals plus the snapshot the search read and the costs it priced in.

        The counts are what a frozen run is audited against, and the execution assumptions
        repeat what was actually passed to the research engine rather than the engine's own
        defaults, so a stored run explains its own numbers. ``comparisonFactors`` is what the
        candidates were measured against: the names the run asked for, next to the names each
        candidate's own metrics report as measured. Reading only the latter cannot tell a factor
        that came back unrelated from one that was never measured.
        """
        return {
            "snapshotId": panel.snapshot_id,
            "warnings": list(panel.warnings),
            "trials": len(trials),
            "candidates": len(candidates),
            "comparisonFactors": comparison.report(),
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



_TIMEFRAME_SHAPE = re.compile(r"^\d{1,6}[mhdw]$")


def _timeframe(value: Any, default: str) -> str:
    """Normalise the chart timeframe and refuse one the shared readers cannot price.

    ``frequencies.periods_per_year`` parses the digits with a bare ``int()`` and
    ``backtest_limits.backtest_warmup_calendar_days`` multiplies them, so an unconstrained
    value like ``"1e400m"`` (six characters) raises inside one of those helpers -- and because
    the raise happens per candidate, inside the candidate guard, the run is recorded as a
    *success* whose every trial blames the DSL. Bounding the shape here keeps both readers
    total and keeps the blame where it belongs: on the request.
    """
    normalized = normalize_chart_timeframe(str(value or default).strip() or default)
    if not _TIMEFRAME_SHAPE.match(normalized.lower()):
        raise ValueError("factorMining.invalidParameter")
    return normalized


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


def _search_mode(value: Any) -> str:
    """Read the search mode. One the engine does not know is refused, never read as the default.

    The modes search different candidate sets over different amounts of history, so a mode that
    fell back to enumeration would run a search the caller did not ask for while the run record
    carried the mode they did name -- two records disagreeing about what ran. Only an absent key
    is the default: a value that is merely falsy, ``false`` or ``0``, is not a mode anyone named
    and is read as the malformed value it is.
    """
    mode = str(value).strip() if value not in (None, "") else SEARCH_MODE_ENUMERATION
    if mode not in _SEARCH_MODES:
        raise ValueError("factorMining.invalidParameter")
    return mode


def _evolution_settings(
    payload: Mapping[str, Any],
    search_mode: str,
) -> tuple[int, int, float]:
    """The breeding population's settings: named by an evolution run, bounded whenever supplied.

    A run that breeds must state all three. Evolution is not a mode a payload defaults into --
    a population this layer invented would be a different search than the one requested -- and
    the count itself is only half of it: ``int(population * elite_ratio)`` is the generation's
    elite quota, so the share decides how much of a generation is kept as much as the size does.
    An empty field states no more than an absent key, so all three read ``""`` the same way: as
    absent, and therefore as refused for a run that breeds. A blank share used to slip through --
    the requirement looked for a missing key while the read below treated one as absent -- and
    was priced as the placeholder instead.

    The bounds are enforced here for the same reason the budget cap is, and there is nowhere else
    to enforce them: ``evolution.py`` feeds the share straight into ``int()`` and a list slice
    without checking it, so an ``elite_ratio`` of 1 or more makes every slot of a generation an
    elite, leaves the breeding loop no room for children, and ends the stream before it yields
    anything -- a run recorded as a success that searched the template expansion and bred nothing.
    A negative share reads as a slice from the end of the ranking rather than an error, and a
    population or generation count below one ends the stream the same silent way. All of them are
    refused by key rather than clamped to a legal range: silently searching something else is the
    worse outcome.
    """
    if search_mode == SEARCH_MODE_EVOLUTION and any(
        payload.get(key) in (None, "") for key in ("population", "generations", "eliteRatio")
    ):
        raise ValueError("factorMining.invalidParameter")
    population = _payload_int(payload, "population", DEFAULT_EVOLUTION_POPULATION)
    generations = _payload_int(payload, "generations", DEFAULT_EVOLUTION_GENERATIONS)
    try:
        elite_ratio = (
            DEFAULT_EVOLUTION_ELITE_RATIO if payload.get("eliteRatio") in (None, "")
            else float(payload["eliteRatio"])
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("factorMining.invalidParameter") from exc
    if population < 1 or generations < 1 or not 0.0 <= elite_ratio < 1.0:
        raise ValueError("factorMining.invalidParameter")
    return population, generations, elite_ratio


def _portfolio_manifest(code: str) -> StrategyManifest:
    """Compile the source, keeping the portfolio-only contract the research entry enforces."""
    manifest = compile_strategy_v2(code).manifest
    if manifest.strategy_type != "portfolio":
        raise StrategyV2ContractError("strategyV2.factorResearchPortfolioOnly")
    return manifest


def _candidate_source(
    config: MiningConfig,
    search_mode: str,
    fitness: Callable[[dict], float | None],
) -> tuple[Iterable[CandidateSeed], int]:
    """The candidates this run evaluates, and the count its progress is reported against.

    Enumeration is the expansion itself and knows its own length. Evolution is that expansion
    *plus* the stream bred from it: the templates are the hypotheses this feature has, so they are
    candidates in their own right rather than breeding stock, and they are chained in front of the
    stream -- which is also why the stream's first generation is ranked: a generator's body runs
    on the first pull, and by then the generation ahead of it has been scored. The budget counts
    draws from that point on, and a draw that yields nothing leaves no trace, so the total is the
    generation plus the budget rather than a length evolution cannot know. What it breeds with --
    the population, its generations, and the elite share -- is the run config's, so the numbers a
    stored run reports are the ones the search actually used.
    """
    seeds = expand_candidates(
        template_ids=config.template_ids or None,
        budget=config.budget,
        seed=config.seed,
        allowed_fields=fields_for(config.market),
    )
    if search_mode != SEARCH_MODE_EVOLUTION:
        return seeds, len(seeds)
    return (
        itertools.chain(
            seeds,
            evolve(
                seeds,
                budget=config.budget,
                seed=config.seed,
                population=config.population,
                generations=config.generations,
                elite_ratio=config.elite_ratio,
                fitness=fitness,
                allowed_fields=fields_for(config.market),
            ),
        ),
        len(seeds) + config.budget,
    )


def _warmup_bars(config: MiningConfig, search_mode: str) -> int:
    """Bars of history the panel must carry before the search window: what the mode can know.

    Enumeration can know exactly. Every candidate it produces comes from a template, so the run
    measures them before it fetches -- the same template selection, budget and seed the search
    loop expands -- and asks for what they need. The longest template path is 145 bars, and
    budgeting the ceiling's 504 would spend 3.5x the provider history for it: on a 1095-day daily
    policy that is the difference between a four-month and a one-year search window, and a run
    caught by the cap is rejected rather than searched shorter. The margin covers the bar-to-
    calendar conversion, which can undershoot on intraday timeframes, and per-symbol gaps inside
    the warmup. The floor and ceiling stay as guards for degenerate runs and for templates that
    grow.

    Evolution can know nothing of the kind: a crossover grafts an arbitrary subtree -- with any
    window the DSL accepts -- onto an arbitrary path, so no number short of the ceiling is
    correct for a run that breeds. The price is provider history, on every evolved run.
    """
    if search_mode == SEARCH_MODE_EVOLUTION:
        return MAX_LOOKBACK
    candidates = expand_candidates(
        template_ids=config.template_ids or None,
        budget=config.budget,
        seed=config.seed,
        allowed_fields=fields_for(config.market),
    )
    longest = max(
        (normalize_expression(candidate.expression,
                              allowed_fields=fields_for(config.market)).lookback
         for candidate in candidates),
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


def _field_panels(
    frames: Mapping[str, pd.DataFrame],
    fields: Collection[str] = PRICE_VOLUME_FIELDS,
) -> dict[str, pd.DataFrame]:
    """Field name -> panel over every symbol, all panels sharing one index and column order.

    A symbol whose frame does not carry a field keeps a NaN column instead of dropping out of
    that field's cross-section, so a candidate reading it stays comparable across symbols. The
    field list is the run's own (``fields_for(market)``), and the mapping carries all of it:
    the evaluator pins every expression to this mapping's index and columns, and a pooled
    candidate is rebuilt against the same mapping, so a field the run may name is a panel every
    reader agrees on the shape of. Admitting a field is not this function's business -- that is
    ``fields_for``'s, read by the validator before anything is evaluated, and a field the
    mapping lacks is *evaluated* as a column of NaN rather than refused. What the gap means is
    the coverage gate's call.
    """
    index = pd.Index(sorted(set().union(*(frame.index for frame in frames.values()))))
    columns = list(frames)
    return {
        field: pd.DataFrame({
            key: pd.to_numeric(frame[field], errors="coerce")
            for key, frame in frames.items()
            if field in frame.columns
        }).reindex(index=index, columns=columns)
        for field in fields
    }


def _skipped_warning(item: Mapping[str, Any]) -> str:
    """One skipped symbol as a display string, so a dropped member is never silent."""
    return f"{item.get('symbol') or ''}: {item.get('reason') or ''}"


def _data_digest(frames: Mapping[str, pd.DataFrame]) -> str:
    """Content hash of the panel: every symbol's canonical OHLCV bytes, symbol names included.

    Hashing per symbol is what makes this a fingerprint of the data. The shared canonical
    encoder reads flat OHLCV columns only, so handing it a merged multi-symbol panel hashes the
    calendar alone and reports two differently-priced runs as the same dataset.

    Two things this function owns rather than borrows. The rows handed to the encoder are the
    panel's price-volume columns' rows, dropped where one of *those* is missing: once a market's
    fields are assembled a frame also carries its derivative columns, and dropping rows on their
    account would take price rows out of the digest for a reason that has nothing to do with the
    prices -- a symbol whose derivative column is empty would contribute no price content at all,
    which reports two differently-priced runs as one. And the remaining columns are folded in
    here, because the shared encoder hashes a fixed price-volume tuple and knows nothing of them:
    without that, two runs whose derivative data differs would share one fingerprint. That
    encoder stays untouched in turn -- it versions Strategy V2's own backtest and research data,
    and widening its column set would re-version panels this module never prepared.

    ``dropna`` is required, not cosmetic: that encoder refuses NaN, and one symbol with a missing
    bar would otherwise fail the run. Dropping rows stays deterministic, so the digest still moves
    when the data does. v1 keeps this fingerprint only -- there is no replayable copy of the panel.
    """
    digest = hashlib.sha256()
    for symbol in sorted(frames):
        digest.update(symbol.encode("utf-8"))
        frame = frames[symbol]
        digest.update(canonical_frame_bytes(frame[_shared_columns(frame)].dropna()))
        digest.update(_extra_field_bytes(frame))
    return digest.hexdigest()


def _shared_columns(frame: pd.DataFrame) -> list[str]:
    """The frame's price-volume columns, spelled the way the shared encoder selects them."""
    return [column for column in SNAPSHOT_COLUMNS if column in frame.columns]


def _extra_field_bytes(frame: pd.DataFrame) -> bytes:
    """The frame's columns outside the shared price-volume set, as canonical bytes.

    The document shape ``canonical_frame_bytes`` writes -- column names sorted, one row per bar,
    the bar's timestamp in nanoseconds -- with the two differences these columns need: they are
    chosen here rather than fixed, and a missing value is ``null`` instead of refused, because a
    derivative column of a symbol no source answered for is made of nothing else. Sorted rows and
    column names are what make the bytes comparable across runs.
    """
    columns = sorted(set(frame.columns) - set(SNAPSHOT_COLUMNS))
    if not columns:
        return b""
    normalized = frame.loc[:, columns].sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    rows = [
        [int(pd.Timestamp(timestamp).value),
         *[None if pd.isna(values[column]) else float(values[column]) for column in columns]]
        for timestamp, values in normalized.iterrows()
    ]
    return json.dumps(
        {"columns": columns, "rows": rows}, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
