"""Data preparation tests for factor mining.

Every data source is faked: no network call and no database runs here. The one real
dependency is the Strategy V2 compiler, because ``prepare`` derives the point-in-time
universe from the compiled manifest.
"""

from datetime import datetime

import pandas as pd
import pytest

from app.services.factor_mining.engine import MiningConfig, MiningDataProvider, split_periods
from app.services.factor_mining.templates import MAX_BUDGET


# Minimal compilable portfolio source: the manifest needs initialize/universe/subscription
# and more than one instrument to be a portfolio strategy.
_SOURCE = '''
def initialize(context):
    context.set_universe(["USStock:AAPL", "USStock:MSFT", "USStock:NVDA"])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    pass
'''


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


class _FakeSnapshotStore:
    def save(self, frame):
        return {"snapshotId": "a" * 64, "snapshotFormat": "test"}


def _config(**overrides):
    base = dict(market="USStock", timeframe="1d",
                start_date=datetime(2024, 1, 1), end_date=datetime(2024, 12, 31),
                source_id=7)
    base.update(overrides)
    return MiningConfig(**base)


def _provider(service):
    return MiningDataProvider(backtest_service=service, snapshot_store=_FakeSnapshotStore())


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
    assert panel.snapshot_id == "a" * 64


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
    single = _SOURCE.replace('"USStock:AAPL", "USStock:MSFT", "USStock:NVDA"', '"USStock:AAPL"')
    with pytest.raises(ValueError, match="strategyV2.factorResearchPortfolioOnly"):
        _provider(_FakeBacktestService()).prepare(user_id=1, code=single, config=_config())


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
            }, [])

    panel = _provider(_RaggedService()).prepare(user_id=1, code=_SOURCE, config=_config())
    close = panel.fields["close"]
    for field, values in panel.fields.items():
        assert values.index.equals(close.index), field
        assert list(values.columns) == list(close.columns), field
    assert close.index.is_monotonic_increasing
    assert list(close.columns) == ["USStock:AAPL", "USStock:MSFT"]
    assert panel.fields["volume"]["USStock:MSFT"].isna().all()
    assert close["USStock:MSFT"].isna().iloc[0]


def test_prepare_panel_records_skipped_symbols_as_warnings():
    class _SkippingService(_FakeBacktestService):
        def fetch_frames(self, candidates, frequency, start_date, end_date):
            frames, _ = super().fetch_frames(candidates, frequency, start_date, end_date)
            frames.pop("USStock:NVDA")
            return frames, [{"symbol": "USStock:NVDA", "reason": "strategyV2.noMarketData"}]

    panel = _provider(_SkippingService()).prepare(user_id=1, code=_SOURCE, config=_config())
    assert panel.warnings == ["USStock:NVDA: strategyV2.noMarketData"]
    assert "USStock:NVDA" not in panel.frames


@pytest.mark.parametrize("ratio", [0.0, 0.5, -0.1])
def test_split_periods_rejects_freeze_ratio_outside_the_open_interval(ratio):
    with pytest.raises(ValueError, match="factorMining.freezeRatioInvalid"):
        split_periods(_config(freeze_ratio=ratio))


def test_from_payload_normalizes_timeframe_and_resolves_rates():
    config = MiningConfig.from_payload(
        {"market": "USStock", "timeframe": "1d", "startDate": "2024-01-01",
         "endDate": "2024-12-31", "sourceId": 7, "freezeRatio": 0.25,
         "commission": "0.0008"},
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
            {"market": "USStock", "timeframe": "1d", "startDate": "2024-01-01",
             "endDate": "2024-12-31", "sourceId": 7, "budget": MAX_BUDGET + 1},
            default_commission=0.0005, default_slippage=0.0005,
        )
