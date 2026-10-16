"""Market-field assembly tests: the derivative columns, their point-in-time alignment, the run's
own session calendar, and the data fingerprint that covers them.

Every data source is faked -- the bars come from a fake Strategy V2 service, and the two
derivative entry points are monkeypatched into ``market_fields`` -- so nothing here opens a
socket or a database connection. What is *not* faked is the rest of the path: the real provider
assembles the panel and the real gate ladder scores the candidate, which is what makes the L2
coverage pin below worth anything. A field set that admits a derivative leaf and a panel that
can only partly fill it are two halves of one run, and only the engine sees both.
"""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.factor_mining import engine as engine_module
from app.services.factor_mining import market_fields
from app.services.factor_mining.dsl import (
    PRICE_VOLUME_FIELDS,
    ExpressionError,
    fields_for,
    normalize_expression,
)
from app.services.factor_mining.engine import (
    FactorMiningEngine,
    MiningDataProvider,
    _data_digest,
    _field_panels,
    split_periods,
)
from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.strategy_v2.snapshot import canonical_frame_bytes
from tests.test_factor_mining_data import _SOURCE, _FakeBacktestService
from tests.test_factor_mining_engine import (
    _NO_DEDUPLICATION,
    _config,
    _funding_seed,
)


# --- fixtures: a perpetual universe, its bars, and the two sources it reads -------------------

# Two key shapes, both of them real: ``@binance:swap`` names a venue and a product, ``@swap``
# names the product alone -- the spelling this repository builds when a member records no
# exchange. Which venue each one reads is the source's decision, taken from the key.
_BTC = "Crypto:BTC/USDT@binance:swap"
_ETH = "Crypto:ETH/USDT@binance:swap"
_SOL = "Crypto:SOL/USDT@swap"
_OKX = "Crypto:XRP/USDT@okx:swap"
_ADA = "Crypto:ADA/USDT@swap"
_DOGE = "Crypto:DOGE/USDT@swap"
_KEYS = (_BTC, _ETH, _SOL, _OKX, _ADA, _DOGE)

_CRYPTO_SOURCE = "\n".join((
    "def initialize(context):",
    f"    context.set_universe([{', '.join(chr(34) + key + chr(34) for key in _KEYS)}])",
    '    context.subscribe(frequency="1d")',
    "",
    "def handle_data(context, data):",
    "    pass",
))

# The bar volume the panel reads, and with it the units the open-interest assertion uses: USD
# notional in the billions is what a perpetual's venue reports (``derivatives`` measures it).
_OI_USD = 2.5e9


def _bars(index, drift):
    """One symbol's OHLCV over ``index``: a plain geometric drift, no idiosyncratic noise."""
    close = [100.0 * (1 + drift) ** day for day in range(len(index))]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value * 1.001 for value in close],
            "low": [value * 0.999 for value in close],
            "close": close,
            "volume": [1000.0 + day for day in range(len(index))],
        },
        index=index,
    )


class _CryptoService:
    """The Strategy V2 seam for a perpetual portfolio: prices only, keys as the members carry them."""

    def __init__(self):
        self.fetch_calls: list[tuple] = []

    def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
        return (
            [
                {
                    "key": key,
                    "market": "Crypto",
                    "symbol": key.split(":", 1)[1].partition("@")[0],
                    "effectiveFrom": None,
                }
                for key in _KEYS
            ],
            None,
        )

    def fetch_frames(self, candidates, frequency, start_date, end_date):
        self.fetch_calls.append((frequency, start_date, end_date))
        index = pd.date_range(start_date, end_date, freq="D")
        return (
            {member["key"]: _bars(index, 0.002 * (offset + 1)) for offset, member in enumerate(candidates)},
            [],
        )


class _FakeHistories:
    """The two derivative entry points: recorded, symbol-dependent, never networked.

    ``covered_days`` is how much of the requested window has settlements at all -- a real shape,
    since a venue's funding history is bounded -- and ``missing`` is a key whose series comes
    back empty, the other real shape: a symbol no source can answer for.
    """

    def __init__(self, *, covered_days: int | None = None, missing: str = "", extra_days: int = 0,
                 period_days: int = 1):
        self.calls: list[tuple] = []
        self.covered_days = covered_days
        self.missing = missing
        # How far past the requested window the series runs, as a source that answers with more
        # history than it was asked for. Zero is what the real ones do: they bound by the window.
        self.extra_days = extra_days
        # How far apart the settlements are. One day lands one on every bar of the daily
        # fixtures; a wider period leaves days between them, which is where a reading that looks
        # forward instead of back has somewhere to differ from one that does not.
        self.period_days = period_days

    def funding(self, market, symbol, *, start, end):
        self.calls.append(("funding_rate", market, symbol, start, end))
        if symbol == self.missing:
            return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))
        first = start if self.covered_days is None else max(start, end - timedelta(days=self.covered_days - 1))
        stop = end + timedelta(days=self.extra_days)
        stamps = pd.date_range(first, stop, freq=f"{self.period_days}D")
        # A per-symbol level plus a slow drift, so every cross-section has something to rank.
        base = 0.01 * (1 + sum(ord(character) for character in symbol) % 5)
        return pd.Series([base + 0.001 * step for step in range(len(stamps))], index=stamps)

    def interest(self, market, symbol, *, start, end):
        self.calls.append(("open_interest", market, symbol, start, end))
        if symbol == self.missing:
            return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))
        stamps = pd.date_range(end, end + timedelta(days=self.extra_days), freq="D")
        return pd.Series([_OI_USD] * len(stamps), index=stamps)


def _patch_histories(monkeypatch, histories):
    monkeypatch.setattr(market_fields, "fetch_funding_history", histories.funding)
    monkeypatch.setattr(market_fields, "fetch_open_interest_history", histories.interest)
    return histories


def _no_pool(*_args, **_kwargs):
    """A pool loader for a caller with nothing frozen -- the store is never opened."""
    return []


def _crypto_provider(service, **overrides):
    return MiningDataProvider(
        backtest_service=service, pool_loader=_no_pool, comparison_factor_limit=0, **overrides
    )


# --- point-in-time alignment ------------------------------------------------------------------

def test_funding_field_is_aligned_to_the_bar_it_was_known_at(monkeypatch):
    """A bar reads the last settlement at or before it, and never the one after.

    A funding settlement *is* the instant its rate became known (``derivatives`` measures that a
    settlement at ``t`` is the rate that crystallized at ``t``), so a bar stamped at ``t`` reads
    it and a bar one tick earlier does not. The series below over-returns on purpose -- it holds a
    settlement past the frame's last bar -- because the alignment, not the source, is what has to
    keep the future out: a source that answers with a wider window is a shape this module already
    expects (``derivatives`` bounds its own pages by the window, not by the panel's grid).
    """
    index = pd.DatetimeIndex([
        datetime(2024, 1, 1, 0, 0), datetime(2024, 1, 1, 8, 0),
        datetime(2024, 1, 1, 16, 0), datetime(2024, 1, 1, 23, 0),
    ])
    settlements = pd.Series(
        [0.01, 0.02, 0.03],
        index=pd.DatetimeIndex([
            datetime(2024, 1, 1, 8, 0), datetime(2024, 1, 1, 16, 0), datetime(2024, 1, 2, 0, 0),
        ]),
    )
    _patch_histories(monkeypatch, _FakeHistories(missing=_BTC))
    monkeypatch.setattr(market_fields, "fetch_funding_history", lambda *a, **k: settlements)

    enriched = market_fields.enrich_frames(
        {_BTC: _bars(index, 0.0)}, market="Crypto",
        start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 23, 59),
    )
    funding = enriched[_BTC]["funding_rate"]
    assert funding.isna().iloc[0], "the 08:00 settlement is not knowable at 00:00"
    assert funding.iloc[1] == pytest.approx(0.01), "a bar stamped at the settlement reads it"
    assert funding.iloc[2] == pytest.approx(0.02)
    assert funding.iloc[3] == pytest.approx(0.02), "the next day's settlement is not read early"


def test_fields_are_forward_filled_not_back_filled(monkeypatch):
    """A bar with no new publication carries the last one; a bar before the first stays empty.

    One rule, two directions. Forward-filling is what makes a daily settlement readable on every
    bar of its day; back-filling would answer an *earlier* bar with a later observation, which is
    the leak a point-in-time panel exists to prevent -- and it would read as full coverage while
    doing it, so no gate downstream could catch it.
    """
    index = pd.date_range("2024-01-01", periods=5, freq="D")
    _patch_histories(monkeypatch, _FakeHistories())
    monkeypatch.setattr(
        market_fields, "fetch_funding_history",
        lambda *a, **k: pd.Series([0.05], index=pd.DatetimeIndex([datetime(2024, 1, 3)])),
    )
    monkeypatch.setattr(
        market_fields, "fetch_open_interest_history",
        lambda *a, **k: pd.Series([_OI_USD], index=pd.DatetimeIndex([datetime(2024, 1, 4)])),
    )

    frame = market_fields.enrich_frames(
        {_BTC: _bars(index, 0.0)}, market="Crypto",
        start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
    )[_BTC]
    assert frame["funding_rate"].iloc[:2].isna().all(), "nothing was known yet -- not back-filled"
    assert list(frame["funding_rate"].iloc[2:]) == [0.05, 0.05, 0.05]
    assert frame["open_interest"].iloc[:3].isna().all()
    assert list(frame["open_interest"].iloc[3:]) == [_OI_USD, _OI_USD]

    # A repeated instant is a venue answering twice: the later row is the one that stands, which
    # is the rule the sources apply to their own pages.
    stamp = datetime(2024, 1, 2)
    monkeypatch.setattr(
        market_fields, "fetch_funding_history",
        lambda *a, **k: pd.Series([0.01, 0.02], index=pd.DatetimeIndex([stamp, stamp])),
    )
    repeated = market_fields.enrich_frames(
        {_BTC: _bars(index, 0.0)}, market="Crypto",
        start=datetime(2024, 1, 1), end=datetime(2024, 1, 5),
    )[_BTC]
    assert repeated["funding_rate"].loc[pd.Timestamp(stamp)] == pytest.approx(0.02)


# --- the run's own calendar -------------------------------------------------------------------

def test_session_calendar_follows_the_markets_trading_days():
    """A 24/7 market's sessions are its calendar days, and the split is counted in them.

    The frozen tail is the last ``freeze_ratio`` of the market's own sessions, and its boundary
    is one of them. Counted on the business-day calendar, a market that trades every day is
    credited with ~28% fewer sessions than it has (262 weekdays against 366 days in a year), so
    the boundary the tail starts at lands sessions away from where the market's own share falls
    -- this window's lands on a Friday where the market's 30% falls on a Sunday -- and the
    session count the run reports for its own two segments is not a count of anything the market
    trades. The stock side keeps the business-day calendar: nothing at this layer knows a
    market's holidays, and its sessions are its weekdays.
    """
    start, end = datetime(2024, 1, 1), datetime(2024, 3, 31, 23, 59, 59)
    crypto = split_periods(_config(market="Crypto", start_date=start, end_date=end))
    stock = split_periods(_config(market="USStock", start_date=start, end_date=end))

    daily = pd.date_range(start, end, freq="D")
    business = pd.bdate_range(start, end)
    assert crypto.search_end == daily[int(len(daily) * 0.7) - 1].to_pydatetime()
    assert stock.search_end == business[int(len(business) * 0.7) - 1].to_pydatetime()
    # The crypto boundary is a session of its own calendar, so it can fall on a weekend day --
    # and this window's does, which is exactly what the business-day count cannot produce.
    assert crypto.search_end.date() == datetime(2024, 3, 3).date()
    assert crypto.search_end.weekday() >= 5
    assert stock.search_end.weekday() < 5
    # Both tails are the share the caller asked for, measured in the sessions they were cut from.
    assert len(daily[daily > pd.Timestamp(crypto.search_end)]) / len(daily) == pytest.approx(0.30, abs=0.01)
    assert len(business[business > pd.Timestamp(stock.search_end)]) / len(business) == pytest.approx(0.30, abs=0.01)


# --- the panel's field set, and the window the sources are read over --------------------------

def test_a_symbol_without_derivatives_keeps_a_nan_column(monkeypatch):
    """A symbol no derivative source can answer for stays in the cross-section, empty.

    This extends the rule ``_field_panels`` already states for a column a frame lacks: dropping
    the symbol would silently change the universe every candidate is ranked in, and the coverage
    gate -- not the assembly -- is what decides what a gap means. The panel carries the run's
    whole field set, ``fields_for(market)``, so a field the run may name is present as a column
    even when nothing filled it; a field missing from the mapping altogether would be answered as
    "no such field", which is the one reading that is wrong.
    """
    _patch_histories(monkeypatch, _FakeHistories(missing=_ADA))

    panel = _crypto_provider(_CryptoService()).prepare(
        user_id=1, code=_CRYPTO_SOURCE, config=_config(market="Crypto")
    )

    assert set(panel.fields) == set(fields_for("Crypto"))
    close = panel.fields["close"]
    for field, values in panel.fields.items():
        assert values.index.equals(close.index), field
        assert list(values.columns) == list(close.columns), field
    assert _ADA in panel.frames, "the symbol is still in the panel"
    assert panel.fields["funding_rate"][_ADA].isna().all()
    assert panel.fields["open_interest"][_ADA].isna().all()
    # The symbols that did answer carry values, in the source's own units.
    assert panel.fields["funding_rate"][_BTC].notna().all()
    assert panel.fields["open_interest"].max().max() == pytest.approx(_OI_USD)


def test_a_reached_venue_cannot_be_swallowed_into_an_empty_column(monkeypatch):
    """The shared no-network guard raises something the sources' own handling cannot swallow.

    Nothing is stubbed here, so this call reaches the real source and the request stops at the
    guard (``tests/conftest.py``) instead of at a venue. What must happen is the loud outcome: a
    guard raising an ordinary ``Exception`` would be answered by the sources' own failure
    handling -- an empty series, an all-NaN column -- and this call would *return* a panel,
    which is exactly how this module's first version queried Binance without anyone noticing.
    The type is the pin, so it is asserted rather than the message.
    """
    index = pd.date_range("2024-01-01", periods=3, freq="D")
    with pytest.raises(BaseException) as caught:
        market_fields.enrich_frames(
            {_BTC: _bars(index, 0.001)}, market="Crypto",
            start=datetime(2024, 1, 1), end=datetime(2024, 1, 3),
        )
    assert not isinstance(caught.value, Exception), "the sources swallow this class into NaN"
    # The guard is a property of the suite, not of this call: an injected source still runs.
    _patch_histories(monkeypatch, _FakeHistories())
    assert market_fields.enrich_frames(
        {_BTC: _bars(index, 0.001)}, market="Crypto",
        start=datetime(2024, 1, 1), end=datetime(2024, 1, 3),
    )[_BTC]["funding_rate"].notna().all()


def test_the_sources_are_asked_with_the_members_own_key_and_the_windows_own_range(monkeypatch):
    """Both key shapes reach the source unparsed, over the bar window and nothing wider.

    Two things the key decides, and this module decides neither: the venue (``@binance:swap``)
    and the product (``@swap``, with no exchange recorded at all -- a key this repository really
    builds). Splitting the suffix here would read one shape right and misread the other as an
    exchange that does not exist. The window is the panel's own fetched range, so a search panel
    cannot carry a derivative value from the frozen tail into a run's fingerprint.
    """
    service = _CryptoService()
    histories = _patch_histories(monkeypatch, _FakeHistories())

    _crypto_provider(service).prepare(user_id=1, code=_CRYPTO_SOURCE, config=_config(market="Crypto"))

    frequency, fetch_start, fetch_end = service.fetch_calls[0]
    asked = {call[2] for call in histories.calls}
    assert asked == set(_KEYS), "every member key travels to the source as it was resolved"
    assert len(histories.calls) == 2 * len(_KEYS), "one request per symbol per field"
    assert {call[3] for call in histories.calls} == {fetch_start}
    assert {call[4] for call in histories.calls} == {fetch_end}
    assert {call[1] for call in histories.calls} == {"Crypto"}
    assert {call[0] for call in histories.calls} == {"funding_rate", "open_interest"}
    assert frequency == "1d"


def test_a_source_that_over_returns_cannot_reach_the_search_panel(monkeypatch):
    """Settlements past the panel's own end change nothing the search reads or fingerprints.

    The window the sources are asked for is the panel's, and the alignment is the second lock on
    the same door: a source that answers with more history than it was asked for must not push a
    frozen-tail value into a search-period reading -- or into the digest a run records. Both
    panels here are assembled over the same bars and the same in-window settlements, so identical
    funding columns and one identical ``snapshot_id`` mean the extra settlements were never read.
    The over-returning shape is not hypothetical (a venue that ignores the window it was handed is
    what the bar path guards against too), and the settlements are weekly on purpose: a bar
    between two of them is where a reading that looks forward has somewhere to differ from one
    that looks back, so this fails on a panel that reads the tail's settlements early rather than
    only on one that grew a row for them.
    """
    service = _CryptoService()
    config = _config(market="Crypto")
    bounded = _patch_histories(monkeypatch, _FakeHistories(period_days=7))
    first = _crypto_provider(service).prepare(user_id=1, code=_CRYPTO_SOURCE, config=config)
    over = _patch_histories(monkeypatch, _FakeHistories(period_days=7, extra_days=35))
    second = _crypto_provider(service).prepare(user_id=1, code=_CRYPTO_SOURCE, config=config)

    assert over.calls[0] == bounded.calls[0], "both runs asked for the same window"
    assert first.snapshot_id == second.snapshot_id
    assert first.fields["funding_rate"].equals(second.fields["funding_rate"])
    assert first.fields["open_interest"].equals(second.fields["open_interest"])


def test_a_field_is_routed_to_its_own_source_and_an_unknown_one_is_refused(monkeypatch):
    """The advertised fields and the dispatcher answer for the same two fields, and for nothing else.

    A catch-all branch would answer a third field with the second one's venue endpoint -- a
    plausible wrong number rather than a missing one, which is the failure mode no coverage gate
    can see -- so the refusal is pinned beside the routing.
    """
    routed: list[str] = []

    def funding(_market, _key, *, start, end):
        routed.append("funding_rate")
        return pd.Series([0.01], index=pd.DatetimeIndex([start]))

    def interest(_market, _key, *, start, end):
        routed.append("open_interest")
        return pd.Series([_OI_USD], index=pd.DatetimeIndex([start]))

    monkeypatch.setattr(market_fields, "fetch_funding_history", funding)
    monkeypatch.setattr(market_fields, "fetch_open_interest_history", interest)

    assert market_fields.derivative_fields("Crypto") == ("funding_rate", "open_interest")
    for field in market_fields.derivative_fields("Crypto"):
        assert isinstance(
            market_fields._series("Crypto", _BTC, field,
                                  start=datetime(2024, 1, 1), end=datetime(2024, 1, 2)),
            pd.Series,
        )
    assert routed == ["funding_rate", "open_interest"]
    with pytest.raises(KeyError):
        market_fields._series("Crypto", _BTC, "term_structure",
                              start=datetime(2024, 1, 1), end=datetime(2024, 1, 2))


def test_a_market_without_derivative_fields_asks_the_sources_nothing(monkeypatch):
    """A stock run reads neither source, and its panel keeps the v1 field set.

    The field set is the authority on whether a market *may* read a derivative field, and this
    module follows it rather than the market's name: a market outside it spends no request and
    gains no column, so its candidates are validated and scored against exactly the price-volume
    panel they were before the cross-market fields existed.
    """
    histories = _patch_histories(monkeypatch, _FakeHistories())

    panel = MiningDataProvider(backtest_service=_FakeBacktestService()).prepare(
        user_id=1, code=_SOURCE, config=_config()
    )

    assert histories.calls == []
    assert set(panel.fields) == set(PRICE_VOLUME_FIELDS)
    assert not any("funding_rate" in frame.columns for frame in panel.frames.values())


# --- the fingerprint --------------------------------------------------------------------------

def _frame():
    """Three flat bars, the frame both fingerprint tests read."""
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [1000.0, 1001.0, 1002.0],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )


def test_the_snapshot_fingerprint_covers_the_new_fields():
    """The data fingerprint moves with the derivative columns, and an empty one hides no prices.

    ``snapshot_id`` is how a run says which data it read: a fingerprint that ignored the new
    columns would call two runs with different funding the same dataset. The second half is the
    trap those columns set. The shared encoder refuses NaN, so the digest drops rows first -- and
    dropping rows on *every* column would send a symbol whose derivative column is empty down to
    no price rows at all, which reports two differently priced runs as one. Rows are dropped on
    the price columns alone; the rest are folded in separately.
    """
    index = pd.date_range("2024-01-01", periods=6, freq="D")
    frame = _bars(index, 0.001)
    empty = frame.assign(funding_rate=float("nan"))
    # The column is part of the panel, so its presence is part of what the fingerprint covers.
    assert _data_digest({_BTC: empty}) != _data_digest({_BTC: frame})
    assert _data_digest({_BTC: frame.assign(funding_rate=0.01)}) != \
           _data_digest({_BTC: frame.assign(funding_rate=0.02)})
    # Prices that differ must differ even when the derivative column is empty in both panels:
    # the price content has to survive a column that is empty for its own reasons.
    revised = _bars(index, 0.002).assign(funding_rate=float("nan"))
    assert _data_digest({_BTC: revised}) != _data_digest({_BTC: empty})
    # A price-only panel digests exactly as it did before these fields existed.
    assert _data_digest({_BTC: frame}) == hashlib.sha256(
        _BTC.encode("utf-8") + canonical_frame_bytes(frame)
    ).hexdigest()


# The bytes the shared encoder returned for ``_frame()`` with a complete derivative column, read
# off the encoder before the market fields existed. A literal rather than a second call to the
# function under test: the claim is that a *different* module's primitive did not move, and
# comparing it against its own current answer would hold for any change it made.
_SHARED_FINGERPRINT = "5667eca3dff4b67a32f645ae8ed49f5f1028c563cf3ff6bc87f18f5ca89fc1db"


def test_the_shared_strategy_v2_fingerprint_is_untouched():
    """``canonical_frame_bytes`` answers the same bytes it did before the new columns existed.

    It is Strategy V2's own data-version primitive -- the store names a ``snapshotId`` after it
    and backtests and research pin their data with it -- so a change here would re-version
    panels this feature never prepared. The cross-module field columns are therefore folded into
    the mining fingerprint locally (``_data_digest``) and ignored by the encoder, which is what
    this pins: the recorded bytes, and the same bytes whether the frame carries them or not.
    """
    frame = _frame()
    enriched = frame.assign(funding_rate=[0.01, 0.02, 0.03], open_interest=[_OI_USD] * 3)
    assert hashlib.sha256(canonical_frame_bytes(enriched)).hexdigest() == _SHARED_FINGERPRINT
    assert canonical_frame_bytes(enriched) == canonical_frame_bytes(frame)


# --- the coverage gate is what refuses a thin derivative column --------------------------------

def _mine(monkeypatch, provider, seed, **thresholds):
    """One enumeration run over ``seed`` alone, through the real provider and the real ladder."""
    thresholds = replace(_NO_DEDUPLICATION, **thresholds)
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **_kwargs: [seed])
    engine = FactorMiningEngine(data_provider=provider)
    return engine.run(
        user_id=1, code=_CRYPTO_SOURCE,
        config=_config(market="Crypto", thresholds=thresholds),
    )


def test_a_derivative_candidate_without_coverage_is_rejected_at_l2(monkeypatch):
    """A candidate reading data the panel mostly lacks is refused by coverage, not scored empty.

    The field set admitting a leaf is a different question from the panel having the data behind
    it: funding history is a bounded window, so a column can be present and thin at once. This is
    the one place both halves are visible -- the leaf is legal for the market, the assembled
    column covers ~19% of the search window, and L2 refuses the candidate rather than measuring a
    column of nothing as if it had been measured. The same candidate and the same data with the
    coverage bar at zero show that the barrier really is the gate: nothing else about the reading
    changes, which is why a run that lowered this bar would keep the candidate.
    """
    _patch_histories(monkeypatch, _FakeHistories(covered_days=40))
    provider = _crypto_provider(_CryptoService())
    seed = _funding_seed("funding_rate")

    strict = _mine(monkeypatch, provider, seed, min_coverage=0.6)
    lax = _mine(monkeypatch, provider, seed, min_coverage=0.0)

    trial = strict["trials"][0]
    assert trial["verdict"] == "reject"
    assert trial["stage"] == "L2"
    assert trial["reason"] == "factorMining.reject.lowCoverage"
    assert strict["candidates"] == []
    # The data was measured, not missing: the reading is a real 19% of the window, which is what
    # makes this a coverage rejection rather than a column that never arrived.
    assert 0.1 < lax["trials"][0]["metrics"]["coverage"] < 0.3
    assert lax["trials"][0]["reason"] != "factorMining.reject.lowCoverage"


# --- the panel's own assembly, without a provider ---------------------------------------------

def test_field_panels_keep_a_missing_field_as_an_empty_column():
    """``_field_panels`` carries the field set it is handed, one shape for every reader.

    The panel is pinned to one index and one column order whatever the fields are, which is what
    lets a candidate be aligned to ``fields["close"]`` and what keeps a symbol from dropping out
    of a derivative cross-section because its own frame has no such column. What the mapping is
    *not* is the field set's enforcement: a field it lacks is evaluated as a column of NaN, and
    refusing a field is ``fields_for``'s job at validation time -- both asserted here, because
    the mapping carrying the whole set is a decision about panel shape, not about admission.
    """
    index = pd.date_range("2024-01-01", periods=3, freq="D")
    frames = {
        _BTC: _bars(index, 0.001).assign(funding_rate=[0.01, 0.02, 0.03]),
        _SOL: _bars(index[1:], 0.002),
    }
    panels = _field_panels(frames, fields_for("Crypto"))
    assert set(panels) == set(fields_for("Crypto"))
    for field, panel in panels.items():
        assert panel.index.equals(panels["close"].index), field
        assert list(panel.columns) == [_BTC, _SOL], field
    assert panels["funding_rate"][_SOL].isna().all()
    assert panels["open_interest"][_BTC].isna().all()

    price_only = {name: panels[name] for name in PRICE_VOLUME_FIELDS}
    evaluated = ExpressionEvaluator(price_only, allowed_fields=fields_for("Crypto"))
    assert evaluated.evaluate("open_interest").isna().all().all(), "a missing field evaluates empty"
    with pytest.raises(ExpressionError):
        normalize_expression("open_interest", allowed_fields=PRICE_VOLUME_FIELDS)
