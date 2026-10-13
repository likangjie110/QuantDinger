"""Data preparation for factor mining: one point-in-time panel, one search/freeze split.

This layer owns the two things the rest of the mining pipeline reads. First the panel: bars
come through the Strategy V2 data path -- ``compile_strategy_v2`` for the manifest,
``resolve_candidates`` for the as-of universe, ``fetch_frames`` for the bars -- so mining
never grows a second data layer with its own time semantics. Second the split: the requested
range is cut into a search window and a frozen tail, and the tail is never fetched here; the
out-of-sample review asks for it separately, once.

Two decisions are invisible in the signatures. The fetch covers the DSL's maximum lookback of
history *before* the search window, because the panel is evaluated over the fetched window and
only then cut to the search window -- a shorter warmup would judge long-window candidates on a
partly NaN factor and fail them on coverage, so the gate would compare window length instead
of signal quality. And the field panels are pinned to one shared index and column order with a
missing field kept as a NaN column, which is what the evaluator assumes when it aligns a
candidate to ``fields["close"]``.

The provider falls back to the real services so the search and freeze stages can construct it
without arguments; tests inject fakes. Nothing here opens a connection, reads a clock, or
mocks the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.services.backtest_execution import parse_rate
from app.services.backtest_limits import backtest_warmup_calendar_days
from app.services.factor_mining.dsl import MAX_LOOKBACK, PRICE_VOLUME_FIELDS, normalize_expression
from app.services.factor_mining.gates import GateThresholds
from app.services.factor_mining.templates import DEFAULT_BUDGET, MAX_BUDGET, expand_candidates
from app.services.strategy_v2.contract import StrategyV2ContractError, compile_strategy_v2
from app.services.strategy_v2.models import StrategyManifest
from app.services.strategy_v2.service import StrategyV2BacktestService, _enforce_backtest_range
from app.services.strategy_v2.snapshot import canonical_frame_bytes


MINIMUM_SYMBOLS = 3
MINIMUM_WARMUP_BARS = 40
WARMUP_MARGIN_BARS = 5
DEFAULT_FREEZE_RATIO = 0.3
DEFAULT_SEED = 42


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
            commission=parse_rate(payload.get("commission"), default=default_commission),
            slippage=parse_rate(payload.get("slippage"), default=default_slippage),
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
        manifest = _portfolio_manifest(code)
        split = split_periods(config)
        candidates, _universe_id = self._backtest_service.resolve_candidates(
            user_id=user_id,
            manifest=manifest,
            start_date=config.start_date,
            end_date=config.end_date,
        )
        if len(candidates) < MINIMUM_SYMBOLS:
            raise ValueError("factorMining.universeTooSmall")
        warmup_bars = _warmup_bars(config)
        fetch_start = _warmup_start(config.timeframe, split.search_start, warmup_bars)
        # The same range check the factor-research entry runs before fetching, against the
        # window this call actually loads: the freeze tail is task 8's separate request.
        _enforce_backtest_range(
            candidates=candidates,
            timeframe=config.timeframe,
            start_date=split.search_start,
            end_date=split.search_end,
            warmup_bars=warmup_bars,
            fetch_start=fetch_start,
        )
        frames, skipped = self._backtest_service.fetch_frames(
            candidates,
            config.timeframe,
            fetch_start,
            split.search_end,
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


def _payload_int(payload: Mapping[str, Any], key: str, default: int) -> int:
    """Read an integer payload field; only an absent key falls back to the default.

    ``payload.get(key) or default`` would read an explicit 0 as absent, and a budget of 0 would
    then never reach the validator that owns the ``factorMining.budgetInvalid`` key.
    """
    value = payload.get(key)
    return default if value is None else int(value)


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
    except (TypeError, ValueError) as exc:
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
