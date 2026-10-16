"""Data preparation tests for factor mining.

Every data source is faked: no network call and no database runs here. The one real
dependency is the Strategy V2 compiler, because ``prepare`` derives the point-in-time
universe from the compiled manifest.
"""

from datetime import datetime
import re

import pandas as pd
import pytest

from app.services.backtest_limits import BacktestRangeLimitError, backtest_warmup_calendar_days
from app.services.factor_mining.dsl import MAX_LOOKBACK
from app.services.factor_mining.engine import (
    MiningConfig,
    MiningDataProvider,
    _warmup_bars,
    split_periods,
)
from app.services.factor_mining.templates import MAX_BUDGET


# Minimal compilable portfolio source: the manifest needs initialize/universe/subscription
# and more than one instrument to be a portfolio strategy.
def _source(*instruments):
    return f'''
def initialize(context):
    context.set_universe([{", ".join(f'"{item}"' for item in instruments)}])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    pass
'''


_SOURCE = _source("USStock:AAPL", "USStock:MSFT", "USStock:NVDA", "USStock:TSLA")


class _FakeBacktestService:
    def __init__(self):
        self.resolve_calls = []
        self.fetch_calls = []

    def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
        self.resolve_calls.append((user_id, start_date, end_date))
        return ([{"market": "USStock", "symbol": "AAPL", "effectiveFrom": None},
                 {"market": "USStock", "symbol": "MSFT", "effectiveFrom": None},
                 {"market": "USStock", "symbol": "NVDA", "effectiveFrom": None}], 42)

    def fetch_frames(self, candidates, frequency, start_date, end_date):
        self.fetch_calls.append((frequency, start_date, end_date))
        index = pd.date_range(start_date, end_date, freq="B")
        frames = {f"USStock:{item['symbol']}": pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0}, index=index)
            for item in candidates}
        return frames, []


class _AShareService(_FakeBacktestService):
    """Chinese A-share members: their daily data carries a tighter provider policy than US."""

    def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
        return ([{"market": "AStock", "symbol": symbol, "effectiveFrom": None}
                 for symbol in ("600000.SH", "600519.SH", "000001.SZ")], None)


def _config(**overrides):
    base = dict(market="USStock", timeframe="1d",
                start_date=datetime(2024, 1, 1), end_date=datetime(2024, 12, 31),
                source_id=7)
    base.update(overrides)
    return MiningConfig(**base)


def _provider(service):
    return MiningDataProvider(backtest_service=service)


def test_split_periods_keeps_freeze_tail_disjoint_from_search():
    split = split_periods(_config(freeze_ratio=0.25))
    assert split.search_start == datetime(2024, 1, 1)
    assert split.search_end < split.freeze_start
    assert split.freeze_end == datetime(2024, 12, 31)


def test_prepare_panel_uses_point_in_time_members():
    service = _FakeBacktestService()
    provider = _provider(service)
    panel = provider.prepare(user_id=5, code=_SOURCE, config=_config())
    assert service.resolve_calls[0][0] == 5
    assert [item["symbol"] for item in panel.members] == ["AAPL", "MSFT", "NVDA"]
    assert set(panel.fields) >= {"open", "high", "low", "close", "volume"}
    assert re.fullmatch(r"[0-9a-f]{64}", panel.snapshot_id)


def test_snapshot_id_tracks_the_data_of_every_symbol():
    config = _config()
    first = _provider(_FakeBacktestService()).prepare(user_id=1, code=_SOURCE, config=config)
    again = _provider(_FakeBacktestService()).prepare(user_id=1, code=_SOURCE, config=config)
    assert first.snapshot_id == again.snapshot_id

    class _RevisedService(_FakeBacktestService):
        def fetch_frames(self, candidates, frequency, start_date, end_date):
            frames, skipped = super().fetch_frames(candidates, frequency, start_date, end_date)
            frames["USStock:NVDA"] = frames["USStock:NVDA"] * 2.0
            return frames, skipped

    revised = _provider(_RevisedService()).prepare(user_id=1, code=_SOURCE, config=config)
    assert revised.snapshot_id != first.snapshot_id


def test_snapshot_id_survives_symbols_with_missing_bars():
    class _GappyService(_FakeBacktestService):
        def fetch_frames(self, candidates, frequency, start_date, end_date):
            frames, skipped = super().fetch_frames(candidates, frequency, start_date, end_date)
            frame = frames["USStock:AAPL"]
            frame.iloc[0, frame.columns.get_loc("close")] = float("nan")
            return frames, skipped

    panel = _provider(_GappyService()).prepare(user_id=1, code=_SOURCE, config=_config())
    assert re.fullmatch(r"[0-9a-f]{64}", panel.snapshot_id)


def test_prepare_panel_fetches_only_the_search_window():
    service = _FakeBacktestService()
    provider = _provider(service)
    config = _config(freeze_ratio=0.25)
    provider.prepare(user_id=1, code=_SOURCE, config=config)
    split = split_periods(config)
    fetch_frequency, fetch_start, fetch_end = service.fetch_calls[0]
    assert fetch_start <= split.search_start
    assert fetch_end < split.freeze_end


def test_prepare_panel_rejects_universe_smaller_than_three():
    class _TinyService(_FakeBacktestService):
        def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
            return ([{"market": "USStock", "symbol": "AAPL", "effectiveFrom": None}], None)

    provider = _provider(_TinyService())
    with pytest.raises(ValueError, match="factorMining.universeTooSmall"):
        provider.prepare(user_id=1, code=_SOURCE, config=_config())


def test_prepare_panel_rejects_a_source_without_market_data():
    class _EmptyService(_FakeBacktestService):
        def fetch_frames(self, candidates, frequency, start_date, end_date):
            return {}, [{"symbol": "USStock:AAPL", "reason": "strategyV2.noMarketData"}]

    provider = _provider(_EmptyService())
    with pytest.raises(ValueError, match="factorMining.noMarketData"):
        provider.prepare(user_id=1, code=_SOURCE, config=_config())


def test_prepare_panel_rejects_a_single_instrument_source():
    with pytest.raises(ValueError, match="strategyV2.factorResearchPortfolioOnly"):
        _provider(_FakeBacktestService()).prepare(
            user_id=1, code=_source("USStock:AAPL"), config=_config())


def test_prepare_panel_rejects_a_usable_universe_smaller_than_three():
    class _DegradedService(_FakeBacktestService):
        def fetch_frames(self, candidates, frequency, start_date, end_date):
            frames, _ = super().fetch_frames(candidates, frequency, start_date, end_date)
            frames.pop("USStock:NVDA")
            return frames, [{"symbol": "USStock:NVDA", "reason": "strategyV2.noMarketData"}]

    with pytest.raises(ValueError, match="factorMining.usableUniverseTooSmall"):
        _provider(_DegradedService()).prepare(user_id=1, code=_SOURCE, config=_config())


def test_prepare_panel_rejects_a_window_beyond_the_market_data_policy():
    # Non-US daily data is capped at 1095 days. Four years leave a ~3.1-year search window after
    # the 30% freeze, and even the enumeration warmup's 284 days push the fetch to 1305 days --
    # over the cap, so the run is rejected before any bar is requested.
    service = _AShareService()
    with pytest.raises(BacktestRangeLimitError):
        _provider(service).prepare(
            user_id=1, code=_SOURCE,
            config=_config(start_date=datetime(2021, 1, 1), end_date=datetime(2024, 12, 31)))
    assert service.fetch_calls == []


def test_enumeration_warmup_is_unchanged_from_v1():
    """Enumeration's warmup and its fetch are the ones v1 computed, to the day.

    Every candidate enumeration produces comes from a template, so the run can measure them
    before it fetches -- the same template selection, budget and seed the search loop expands --
    and ask for exactly what they need. The ceiling would spend 3.5x the provider history for
    that and, where a policy caps the fetched range, get the run rejected instead of searched
    shorter: a reversal-only A-share run used to look back 76 days and fit inside the 1095-day
    CNStock policy, and it still does.
    """
    short = _config(template_ids=("reversal",))
    # reversal's longest path is 20 bars plus a 5-bar smoothing variant, so the floor binds
    assert _warmup_bars(short, "enumeration") == 40
    assert backtest_warmup_calendar_days("1d", _warmup_bars(short, "enumeration")) == 76
    # every template: 145 bars longest plus the 5-bar margin, against the 504 the ceiling cost
    assert _warmup_bars(_config(), "enumeration") == 150
    assert backtest_warmup_calendar_days("1d", _warmup_bars(_config(), "enumeration")) == 284

    service = _AShareService()
    panel = _provider(service).prepare(user_id=1, code=_SOURCE, config=short)
    assert panel.frequency == "1d"
    _, fetch_start, _ = service.fetch_calls[0]
    assert (split_periods(short).search_start - fetch_start).days == 76


def test_the_evolution_fetch_starts_at_the_dsl_ceiling():
    """Evolution takes the ceiling, and the fetch follows it: an evolved lookback is unknowable.

    A crossover grafts an arbitrary subtree -- carrying any window the DSL accepts -- onto an
    arbitrary path, so no number short of the ceiling is correct for a run that breeds. The price
    is provider history: the ceiling's 953 calendar days do not fit the 1095-day CNStock policy
    for a 2024 range, so this run is refused before a bar is requested, while the same range under
    enumeration still fetches what its candidates need.
    """
    config = _config(template_ids=("reversal",))
    assert _warmup_bars(config, "evolution") == MAX_LOOKBACK
    assert backtest_warmup_calendar_days("1d", _warmup_bars(config, "evolution")) == 953
    refused = _AShareService()
    with pytest.raises(BacktestRangeLimitError):
        _provider(refused).prepare(user_id=1, code=_SOURCE, config=config,
                                   search_mode="evolution")
    assert refused.fetch_calls == []

    # US daily data allows 10 years, so the same run fetches the ceiling's window there.
    service = _FakeBacktestService()
    _provider(service).prepare(user_id=1, code=_SOURCE, config=config, search_mode="evolution")
    _, fetch_start, _ = service.fetch_calls[0]
    assert (split_periods(config).search_start - fetch_start).days == 953


def test_prepare_panel_looks_back_warmup_bars_but_stops_at_the_search_end():
    service = _FakeBacktestService()
    config = _config(freeze_ratio=0.25)
    _provider(service).prepare(user_id=1, code=_SOURCE, config=config)
    frequency, fetch_start, fetch_end = service.fetch_calls[0]
    split = split_periods(config)
    assert frequency == config.timeframe
    assert fetch_start < split.search_start
    assert fetch_end == split.search_end


def test_prepare_panel_keeps_panels_aligned_and_fills_missing_fields():
    class _RaggedService(_FakeBacktestService):
        def fetch_frames(self, candidates, frequency, start_date, end_date):
            index = pd.date_range(start_date, end_date, freq="B")
            full = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0}
            return ({
                "USStock:AAPL": pd.DataFrame(full, index=index),
                "USStock:MSFT": pd.DataFrame(
                    {name: value for name, value in full.items() if name != "volume"},
                    index=index[1:]),
                "USStock:NVDA": pd.DataFrame(full, index=index),
            }, [])

    panel = _provider(_RaggedService()).prepare(user_id=1, code=_SOURCE, config=_config())
    close = panel.fields["close"]
    for field, values in panel.fields.items():
        assert values.index.equals(close.index), field
        assert list(values.columns) == list(close.columns), field
    assert close.index.is_monotonic_increasing
    assert list(close.columns) == ["USStock:AAPL", "USStock:MSFT", "USStock:NVDA"]
    assert panel.fields["volume"]["USStock:MSFT"].isna().all()
    assert close["USStock:MSFT"].isna().iloc[0]


def test_prepare_panel_records_skipped_symbols_as_warnings():
    class _SkippingService(_FakeBacktestService):
        def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
            members, universe_id = super().resolve_candidates(
                user_id=user_id, manifest=manifest, start_date=start_date, end_date=end_date)
            return ([*members, {"market": "USStock", "symbol": "TSLA", "effectiveFrom": None}],
                    universe_id)

        def fetch_frames(self, candidates, frequency, start_date, end_date):
            frames, _ = super().fetch_frames(candidates, frequency, start_date, end_date)
            frames.pop("USStock:NVDA")
            return frames, [{"symbol": "USStock:NVDA", "reason": "strategyV2.noMarketData"}]

    panel = _provider(_SkippingService()).prepare(user_id=1, code=_SOURCE, config=_config())
    assert panel.warnings == ["USStock:NVDA: strategyV2.noMarketData"]
    assert "USStock:NVDA" not in panel.frames


def test_warnings_follow_a_stable_order_for_reproducible_run_records():
    class _TwoSkippedService(_FakeBacktestService):
        def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
            return ([{"market": "USStock", "symbol": symbol, "effectiveFrom": None}
                     for symbol in ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN")], None)

        def fetch_frames(self, candidates, frequency, start_date, end_date):
            frames, _ = super().fetch_frames(candidates, frequency, start_date, end_date)
            frames.pop("USStock:TSLA")
            frames.pop("USStock:AMZN")
            return frames, [
                {"symbol": "USStock:TSLA", "reason": "strategyV2.noMarketData"},
                {"symbol": "USStock:AMZN", "reason": "strategyV2.noMarketData"},
            ]

    panel = _provider(_TwoSkippedService()).prepare(user_id=1, code=_SOURCE, config=_config())
    assert panel.warnings == ["USStock:AMZN: strategyV2.noMarketData",
                              "USStock:TSLA: strategyV2.noMarketData"]


@pytest.mark.parametrize("ratio", [0.0, 0.5, -0.1])
def test_split_periods_rejects_freeze_ratio_outside_the_open_interval(ratio):
    with pytest.raises(ValueError, match="factorMining.freezeRatioInvalid"):
        split_periods(_config(freeze_ratio=ratio))


def _payload(**overrides):
    base = {"market": "USStock", "timeframe": "1d", "startDate": "2024-01-01",
            "endDate": "2024-12-31", "sourceId": 7}
    base.update(overrides)
    return base


def test_from_payload_normalizes_timeframe_and_resolves_rates():
    config = MiningConfig.from_payload(
        _payload(freezeRatio=0.25, commission="0.0008"),
        default_commission=0.0005, default_slippage=0.0005,
    )
    assert config.timeframe == "1D"
    assert config.start_date == datetime(2024, 1, 1)
    assert config.end_date.date() == datetime(2024, 12, 31).date()
    assert config.freeze_ratio == 0.25
    assert config.commission == 0.0008
    assert config.slippage == 0.0005
    with pytest.raises(ValueError, match="factorMining.budgetExceeded"):
        MiningConfig.from_payload(
            _payload(budget=MAX_BUDGET + 1),
            default_commission=0.0005, default_slippage=0.0005,
        )


@pytest.mark.parametrize("budget", [0, -5])
def test_from_payload_keeps_a_budget_the_validator_must_see(budget):
    config = MiningConfig.from_payload(
        _payload(budget=budget), default_commission=0.0005, default_slippage=0.0005,
    )
    assert config.budget == budget
