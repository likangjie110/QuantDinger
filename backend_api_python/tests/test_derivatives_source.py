"""Funding rate and open interest history: units, index semantics, isolation and paging.

Every provider call is answered from ``tests/fixtures/derivatives/*.json`` -- real recorded bodies
for Gate and Binance, schema-only bodies for OKX, which is unreachable from the recording host --
so nothing in this file touches the network. An unrecorded request fails the test instead of
leaving the process: the transport is keyed by URL and by the exact parameter mapping the
implementation sends, which pins the request shape as well as the response handling.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from app.data_sources import derivatives

_FIXTURES = Path(__file__).parent / "fixtures" / "derivatives"

# Two windows, both in the past so the fixtures stay valid: three settlements over one day
# (10-08 00:00, 08:00, 16:00 UTC) for the units, five over one and a third days for the pages.
_FUNDING_START = datetime(2026, 10, 8, 0, 0)
_FUNDING_END = datetime(2026, 10, 8, 16, 0)
_WIDE_START = datetime(2026, 10, 7, 0, 0)
_WIDE_END = datetime(2026, 10, 8, 8, 0)
_OI_START = datetime(2026, 10, 10, 0, 0)
_OI_END = datetime(2026, 10, 15, 0, 0)

_SYMBOL = {
    "binance": "Crypto:BTC/USDT@binance:swap",
    "gate": "Crypto:BTC/USDT@gate:swap",
    "okx": "Crypto:BTC/USDT@okx:swap",
}
_FUNDING_URL = {
    "binance": "https://fapi.binance.com/fapi/v1/fundingRate",
    "gate": "https://api.gateio.ws/api/v4/futures/usdt/funding_rate",
    "okx": "https://www.okx.com/api/v5/public/funding-rate-history",
}
_OI_URL = {
    "binance": "https://fapi.binance.com/futures/data/openInterestHist",
    "gate": "https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
    "okx": "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history",
}
_FUNDING_PAGE = {
    "binance": "_BINANCE_FUNDING_PAGE",
    "gate": "_GATE_FUNDING_PAGE",
    "okx": "_OKX_FUNDING_PAGE",
}
_OI_PAGE = {"binance": "_BINANCE_OI_PAGE", "gate": "_GATE_OI_PAGE", "okx": "_OKX_OI_PAGE"}
# The page widths the recordings themselves answer at, i.e. the real ceilings the module asks for.
_FUNDING_PAGE_WIDTH = {"binance": 1000, "gate": 1000, "okx": 100}
_OI_PAGE_WIDTH = {"binance": 500, "okx": 100}
# The clock the clamp case freezes: the hour its fixture was recorded in, so the request it pins is
# inside Gate's own 180-day edge.
_FROZEN_NOW = pd.Timestamp("2026-10-16 19:00")


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Unrecorded(BaseException):
    """A request no fixture answers.

    Deliberately not an ``Exception``: the module under test swallows provider failures into an
    empty series, and a missing recording must not be able to hide inside that.
    """


class _Recorded:
    """A ``requests`` stand-in: recorded bodies for known requests, an error for anything else."""

    def __init__(self, *, bodies, failing=()):
        self.bodies = bodies
        self.failing = set(failing)
        self.calls: list[tuple[str, dict]] = []
        self.unrecorded: list[tuple[str, dict]] = []

    def get(self, url, *, params=None, **_kwargs):
        self.calls.append((url, params))
        if url in self.failing:
            raise requests.ConnectionError(f"no route to {url}")
        key = (url, _canonical(params))
        if key not in self.bodies:
            self.unrecorded.append((url, params))
            raise _Unrecorded(f"unrecorded request: {url} {params}")
        return _Response(self.bodies[key])

    def urls(self) -> list[str]:
        return [url for url, _params in self.calls]


def _canonical(params) -> tuple:
    return tuple(sorted((str(key), str(value)) for key, value in (params or {}).items()))


def _load_bodies() -> dict:
    bodies = {}
    for path in sorted(_FIXTURES.glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for item in fixture["responses"]:
            key = (item["url"], _canonical(item["params"]))
            assert key not in bodies, f"duplicate recording in {path.name}: {key}"
            bodies[key] = item["body"]
    return bodies


_BODIES = _load_bodies()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this module reaches the network, not even by accident."""
    monkeypatch.setattr(derivatives, "requests", _Recorded(bodies={}))


@pytest.fixture
def recorded(monkeypatch, _no_network):
    transport = _Recorded(bodies=_BODIES)
    monkeypatch.setattr(derivatives, "requests", transport)
    # A default venue every case can rely on: the real one is deployment configuration, and the
    # case that pins where it comes from states it for itself.
    monkeypatch.setattr(
        derivatives, "CryptoPublicConfig", SimpleNamespace(DEFAULT_EXCHANGE="binance")
    )
    return transport


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin the one clock the module reads.

    Gate validates its 180-day funding ``from`` against *its* clock, so these fixed windows would
    start asking a different question as the real clock advances: the first request changes shape on
    2027-04-04 23:00 (the paging window) and 2027-04-05 23:00 (the one-day window), which an
    unrecorded request turns into a red test, and from 2027-04-06 the windows sit entirely past the
    venue's edge and answer with an empty series instead. Either way the test would break on a
    calendar day, and whoever meets it then did not write it. Frozen at an hour later than every
    window in this file, so the clamp stays a no-op for them.
    """
    monkeypatch.setattr(derivatives, "_now", lambda: _FROZEN_NOW)


def _at(stamp: str) -> pd.Timestamp:
    return pd.Timestamp(stamp)


# --- funding: one value per settlement, in percent ------------------------------------------

_FUNDING_EXPECTED = {
    "binance": {
        _at("2026-10-08 00:00"): -0.000992,
        _at("2026-10-08 08:00"): -0.001185,
        _at("2026-10-08 16:00"): 0.003445,
    },
    "gate": {
        _at("2026-10-08 00:00"): 0.0051,
        _at("2026-10-08 08:00"): 0.0013,
        _at("2026-10-08 16:00"): 0.0031,
    },
    "okx": {
        _at("2026-10-08 00:00"): 0.00584,
        _at("2026-10-08 08:00"): -0.00247,
        _at("2026-10-08 16:00"): 0.00361,
    },
}


@pytest.mark.parametrize("venue", ["binance", "gate", "okx"])
def test_funding_history_is_normalised_to_a_per_interval_rate(recorded, frozen_clock, venue):
    """One rate per settlement, in percent, indexed by the instant the rate settled.

    The fixtures carry the venues' own decimals (0.000051 on Gate, -0.00000992 on Binance,
    0.00003610 on OKX, which realizes a capped fundingRate of 0.00015000); every value below is
    that decimal x 100, i.e. the percent per settlement the repository reports as ``funding_rate``.
    A missing or doubled conversion fails here, and so does reading ``fundingRate`` when OKX
    reports a different ``realizedRate``.
    """
    series = derivatives.fetch_funding_history(
        "Crypto", _SYMBOL[venue], start=_FUNDING_START, end=_FUNDING_END
    )

    assert isinstance(series, pd.Series)
    assert series.index.tz is None
    assert list(series.index) == sorted(_FUNDING_EXPECTED[venue])
    for stamp, expected in _FUNDING_EXPECTED[venue].items():
        assert series.loc[stamp] == pytest.approx(expected)

    # The recordings answer at the venue's own page ceiling, so the request pins that width too.
    assert [params["limit"] for _url, params in recorded.calls] == [_FUNDING_PAGE_WIDTH[venue]]


# --- open interest: a level, in USD, per venue-reported bucket -------------------------------

_OI_EXPECTED = {
    "binance": {
        _at("2026-10-10 00:00"): 7641006984.56147,
        _at("2026-10-11 00:00"): 7679749494.333401,
        _at("2026-10-12 00:00"): 7734132958.865111,
        _at("2026-10-13 00:00"): 7642933868.6864,
        _at("2026-10-14 00:00"): 7689144706.397,
        _at("2026-10-15 00:00"): 7602233027.2145,
    },
    "gate": {
        _at("2026-10-10 00:00"): 4100680561.47568,
        _at("2026-10-11 00:00"): 4297354867.56608,
        _at("2026-10-12 00:00"): 4304366807.29088,
        _at("2026-10-13 00:00"): 4199323613.264,
        _at("2026-10-14 00:00"): 4253233823.37,
        _at("2026-10-15 00:00"): 4356362786.981272,
    },
    "okx": {
        _at("2026-10-11 00:00"): 4893000000.5,
        _at("2026-10-12 00:00"): 4951200000.25,
        _at("2026-10-13 00:00"): 5018700000.75,
        _at("2026-10-14 00:00"): 4982500000.5,
        _at("2026-10-15 00:00"): 5073100000.125,
    },
}


@pytest.mark.parametrize("venue", ["binance", "gate"])
def test_open_interest_history_is_returned_as_a_level_series(recorded, monkeypatch, venue):
    """The open interest *level* per bucket, so a change rate stays something a factor derives.

    These arrive as USD notional in the billions; a series of percent changes would be five
    orders of magnitude smaller, and the widest step in the recording is under 5%.

    Gate's page cap is lowered to the three rows the recordings answer at: the loop is a walk over
    ``to``, so its page width changes how many requests a window takes, not what the series holds.
    Binance answers this window within one page at its real cap, as recorded.
    """
    if venue == "gate":
        monkeypatch.setattr(derivatives, _OI_PAGE[venue], 3)

    series = derivatives.fetch_open_interest_history(
        "Crypto", _SYMBOL[venue], start=_OI_START, end=_OI_END
    )

    assert list(series.index) == sorted(_OI_EXPECTED[venue])
    for stamp, expected in _OI_EXPECTED[venue].items():
        assert series.loc[stamp] == pytest.approx(expected)

    steps = series.pct_change().dropna().abs()
    assert (series > 1e6).all(), "a level series, not a ratio"
    assert (steps < 0.05).all(), "a change series would move in percent, not by a factor of a billion"

    expected_limit = 3 if venue == "gate" else _OI_PAGE_WIDTH[venue]
    assert [params["limit"] for _url, params in recorded.calls] == [expected_limit] * len(recorded.calls)


def test_okx_open_interest_is_not_served(recorded):
    """A venue whose rows could not be verified answers nothing, rather than a plausible number.

    ``_okx_interest_rows`` reads the row's USD notional by *column position*, and OKX is
    unreachable from the recording host, so a wrong order would answer with a plausible number of
    the wrong kind on a field every Crypto panel carries -- a full column no coverage gate can
    question. Unlisted, the pair costs the column instead: an empty series and no request made,
    which is what L2's coverage bar is there to price. ``funding`` from the same venue is served
    and unaffected; this test exists so that re-listing the pair has to be a deliberate act.
    """
    series = derivatives.fetch_open_interest_history(
        "Crypto", _SYMBOL["okx"], start=_OI_START, end=_OI_END
    )

    assert series.empty
    assert isinstance(series.index, pd.DatetimeIndex)
    assert recorded.calls == []


def test_gate_open_interest_is_published_at_the_end_of_the_bucket_it_measured(recorded, monkeypatch):
    """Gate stamps the bucket's *last* observation at the bucket's start (measured 2026-10-16).

    Requesting the same window on both granularities shows it: the 1d row stamped 10-09 repeats
    the open interest the 1h series reports at 10-09 23:00, and its ``last_funding_rate`` repeats
    the settlement of 10-10 00:00. Publishing that value at 10-09 would put a day of look-ahead in
    front of a factor, so the 1d rows are moved to the end of the bucket they measured.
    """
    monkeypatch.setattr(derivatives, _OI_PAGE["gate"], 3)

    series = derivatives.fetch_open_interest_history(
        "Crypto", _SYMBOL["gate"], start=_OI_START, end=_OI_END
    )

    # 10-09's measurement is published at 10-10; the row measuring 10-15 would be published at
    # 10-16, past the requested window, and is dropped.
    assert _at("2026-10-10 00:00") in series.index
    assert _at("2026-10-16 00:00") not in series.index
    assert series.loc[_at("2026-10-10 00:00")] == pytest.approx(4100680561.47568)


# --- what is not supported ----------------------------------------------------------------


@pytest.mark.parametrize("market", ["USStock", "Futures", "Forex", "crypto", ""])
def test_an_unsupported_market_returns_an_empty_series_not_an_error(recorded, market):
    """The field set decides whether a market has derivatives; the source only says what it has.

    ``crypto`` in lower case is here on purpose: markets are compared exactly, so it is not the
    market whose panel carries derivative fields.
    """
    for fetch, start, end in (
        (derivatives.fetch_funding_history, _FUNDING_START, _FUNDING_END),
        (derivatives.fetch_open_interest_history, _OI_START, _OI_END),
    ):
        series = fetch(market, "Crypto:BTC/USDT@binance:swap", start=start, end=end)
        assert isinstance(series, pd.Series)
        assert series.empty
        assert isinstance(series.index, pd.DatetimeIndex)

    assert recorded.calls == []


def test_an_unsupported_venue_returns_an_empty_series(recorded):
    series = derivatives.fetch_funding_history(
        "Crypto", "Crypto:BTC/USDT@bybit:swap", start=_FUNDING_START, end=_FUNDING_END
    )

    assert series.empty
    assert recorded.calls == []


def test_a_key_that_names_only_a_market_type_reads_the_default_venue(recorded):
    """``Crypto:BTC/USDT@swap`` is a key this repository really builds.

    ``InstrumentSpec.key`` spells the market type alone when the run recorded no exchange id, so the
    word after ``@`` is a product here and not a venue: the key asks the same question the bare pair
    does and must get the same answer, not an empty column from a venue named ``swap``.
    """
    by_key = derivatives.fetch_funding_history(
        "Crypto", "Crypto:BTC/USDT@swap", start=_FUNDING_START, end=_FUNDING_END
    )
    bare = derivatives.fetch_funding_history("Crypto", "BTC/USDT", start=_FUNDING_START, end=_FUNDING_END)

    assert list(by_key.index) == sorted(_FUNDING_EXPECTED["binance"])
    assert by_key.equals(bare)
    assert recorded.urls() == [_FUNDING_URL["binance"]] * 2


@pytest.mark.parametrize("symbol", [
    "Crypto:BTC/USDT@binance:spot",
    "Crypto:BTC/USDT@okx:spot",
    "Crypto:BTC/USDT@binance:cash",
    "Crypto:BTC/USDT@binance:margin",
])
def test_a_key_that_names_a_product_other_than_a_perpetual_returns_an_empty_series(recorded, symbol):
    """A spot member has no funding rate and no open interest to read.

    The perpetual endpoint answers for the perpetual contract of the same base asset, so accepting
    the key would feed a spot symbol a perpetual's numbers -- the cross-market mix this module
    exists to avoid. A product word this module cannot place (``margin``) is refused the same way,
    because it cannot be established to be a perpetual either.
    """
    for fetch, start, end in (
        (derivatives.fetch_funding_history, _FUNDING_START, _FUNDING_END),
        (derivatives.fetch_open_interest_history, _OI_START, _OI_END),
    ):
        series = fetch("Crypto", symbol, start=start, end=end)
        assert series.empty

    assert recorded.calls == []


@pytest.mark.parametrize("symbol", [
    "BTC",
    "@binance:swap",
    ":",
])
def test_a_symbol_without_a_pair_returns_an_empty_series(recorded, symbol):
    series = derivatives.fetch_open_interest_history(
        "Crypto", symbol, start=_OI_START, end=_OI_END
    )

    assert series.empty
    assert recorded.calls == []


@pytest.mark.parametrize("configured, expected", [("gate", "gate"), ("okx", "okx"), ("", "binance")])
def test_a_symbol_that_names_no_venue_is_read_on_the_configured_venue(
    recorded, frozen_clock, monkeypatch, configured, expected
):
    """The venue behind "no venue named" is the app's, read when the call is made.

    ``CRYPTO_PUBLIC_DEFAULT_EXCHANGE`` is what the rest of the repository reads to place a bare
    crypto symbol, so a deployment pointed at Gate must get Gate's funding rates next to Gate's
    prices. A constant copied from that setting's factory value would keep reading Binance, which is
    the cross-venue mix this module exists to avoid and, worse than an empty column, a
    plausible-looking wrong number. An empty setting falls back rather than answering for nobody.
    """
    monkeypatch.setattr(
        derivatives, "CryptoPublicConfig", SimpleNamespace(DEFAULT_EXCHANGE=configured)
    )

    series = derivatives.fetch_funding_history(
        "Crypto", "BTC/USDT", start=_FUNDING_START, end=_FUNDING_END
    )

    assert list(series.index) == sorted(_FUNDING_EXPECTED[expected])
    assert recorded.urls() == [_FUNDING_URL[expected]]


def test_a_configured_venue_this_module_does_not_serve_returns_an_empty_series(recorded, monkeypatch):
    """The configuration is not a promise that this module can read that venue."""
    monkeypatch.setattr(
        derivatives, "CryptoPublicConfig", SimpleNamespace(DEFAULT_EXCHANGE="bybit")
    )

    series = derivatives.fetch_funding_history(
        "Crypto", "BTC/USDT", start=_FUNDING_START, end=_FUNDING_END
    )

    assert series.empty
    assert recorded.calls == []


# --- isolation and empty answers ------------------------------------------------------------


@pytest.mark.parametrize("failing", ["funding", "open_interest"])
def test_a_provider_failure_does_not_poison_the_other_fields(recorded, failing, monkeypatch):
    """One endpoint being down must not cost the other column its data.

    The funding and open interest endpoints are asked separately and share no state, so a
    transport that fails for one of them still answers the other; the failed field is empty
    rather than raising, and nothing about it leaks into the series that did arrive.
    """
    venue = "binance"
    failing_url = _FUNDING_URL[venue] if failing == "funding" else _OI_URL[venue]
    transport = _Recorded(bodies=_BODIES, failing=[failing_url])
    monkeypatch.setattr(derivatives, "requests", transport)

    funding = derivatives.fetch_funding_history(
        "Crypto", _SYMBOL[venue], start=_FUNDING_START, end=_FUNDING_END
    )
    open_interest = derivatives.fetch_open_interest_history(
        "Crypto", _SYMBOL[venue], start=_OI_START, end=_OI_END
    )

    if failing == "funding":
        assert funding.empty
        assert list(open_interest.index) == sorted(_OI_EXPECTED[venue])
    else:
        assert open_interest.empty
        assert list(funding.index) == sorted(_FUNDING_EXPECTED[venue])


@pytest.mark.parametrize("start, end", [
    (_FUNDING_START, _FUNDING_END),
    (_OI_START, _OI_END),
])
def test_a_venue_that_answers_nothing_gives_an_empty_series(monkeypatch, start, end):
    """A venue with no rows in the window is an empty series, not an error and not a NaN row."""
    class _Nothing:
        def get(self, url, *, params=None, **_kwargs):
            return _Response([])

    monkeypatch.setattr(derivatives, "requests", _Nothing())

    for fetch in (derivatives.fetch_funding_history, derivatives.fetch_open_interest_history):
        series = fetch("Crypto", _SYMBOL["binance"], start=start, end=end)
        assert series.empty
        assert list(series) == []


def test_a_reversed_window_is_empty_and_asks_nobody(recorded):
    for fetch in (derivatives.fetch_funding_history, derivatives.fetch_open_interest_history):
        series = fetch("Crypto", _SYMBOL["binance"], start=_FUNDING_END, end=_FUNDING_START)
        assert series.empty

    assert recorded.calls == []


def test_a_settlement_exactly_on_a_single_point_window_is_returned(recorded, frozen_clock):
    """``start == end`` is one settlement, not an empty window: the bounds are inclusive."""
    series = derivatives.fetch_funding_history(
        "Crypto", _SYMBOL["gate"], start=_at("2026-10-08 08:00"), end=_at("2026-10-08 08:00")
    )

    assert list(series.index) == [_at("2026-10-08 08:00")]
    assert series.iloc[0] == pytest.approx(0.0013)


def test_gate_funding_reads_the_tail_it_keeps_when_the_window_ends_in_the_past(recorded, frozen_clock):
    """Gate's 180-day funding edge is measured on *its* clock, not against the caller's window.

    A floor taken from the window alone asks for a ``from`` older than that edge whenever the window
    ends in the past -- which is the normal case, since a factor-mining window ends on the last
    closed bar -- and Gate answers 400, which this module turns into an empty column while it holds
    the window's tail. Measured 2026-10-16: ``now - 180d`` answers, ``now - 180d - 1h`` does not.
    """
    series = derivatives.fetch_funding_history(
        "Crypto",
        _SYMBOL["gate"],
        start=_FROZEN_NOW - pd.Timedelta(days=730),
        end=_FROZEN_NOW - pd.Timedelta(days=1),
    )

    assert len(series) == 537
    # The clamped floor is 2026-04-19 20:00, so the first settlement at or after it opens the series
    # -- 2026-04-20 00:00, two seconds early in the recording and rounded onto the grid.
    assert series.index.min() == _at("2026-04-20 00:00")
    assert series.index.max() == _at("2026-10-15 16:00")
    assert (series > -1).all() and (series < 1).all(), "percent per settlement, not a raw decimal"


def test_a_window_older_than_the_venue_keeps_is_not_asked_for(recorded, frozen_clock):
    """A window entirely past Gate's edge has no funding to give, so nobody is asked for it."""
    series = derivatives.fetch_funding_history(
        "Crypto",
        _SYMBOL["gate"],
        start=_FROZEN_NOW - pd.Timedelta(days=1095),
        end=_FROZEN_NOW - pd.Timedelta(days=730),
    )

    assert series.empty
    assert recorded.calls == []


def test_the_open_interest_page_width_is_the_venue_ceiling(recorded):
    """Gate's stats ceiling is pinned here rather than in a fixture: one row is ~1.1 KB.

    A recording at the real width would be a megabyte, so the other five ceilings are pinned by the
    requests their recordings answer (see the funding and open interest cases) and this one by
    value. It is the ceiling Gate documents and the one a live call was measured at (1000 rows).
    """
    assert derivatives._GATE_OI_PAGE == 1000


def test_a_stamp_no_calendar_can_hold_costs_the_series_not_the_call(recorded):
    """Assembling the answer is part of answering: a wild window must not raise through a caller.

    A year-5000 bound converts to a ``Timestamp`` and then overflows the moment it is turned into
    milliseconds, which is three frames below the promise that these functions return an empty
    series rather than raising.
    """
    for fetch, start, end in (
        (derivatives.fetch_funding_history, datetime(5000, 1, 1), datetime(5000, 1, 2)),
        (derivatives.fetch_open_interest_history, datetime(5000, 1, 1), datetime(5000, 2, 1)),
    ):
        series = fetch("Crypto", _SYMBOL["gate"], start=start, end=end)
        assert series.empty


# --- request shape and paging --------------------------------------------------------------


def test_binance_open_interest_asks_by_end_time_alone(recorded):
    """Binance keeps one month of open interest and answers ``-1130`` for an older startTime.

    Asking by ``endTime`` alone gets the month it does keep and lets the window filter decide
    what to publish; asking by a windowed ``startTime`` would turn a long backtest window into an
    empty column.
    """
    derivatives.fetch_open_interest_history(
        "Crypto", _SYMBOL["binance"], start=datetime(2024, 1, 1), end=_OI_END
    )

    assert recorded.calls == [(
        _OI_URL["binance"],
        {"symbol": "BTCUSDT", "period": "1d", "endTime": 1792022400000, "limit": 500},
    )]


_FUNDING_PAGED = {
    "binance": {
        _at("2026-10-07 00:00"): -0.000743,
        _at("2026-10-07 08:00"): -0.003657,
        _at("2026-10-07 16:00"): 0.001152,
        _at("2026-10-08 00:00"): -0.000992,
        _at("2026-10-08 08:00"): -0.001185,
    },
    "gate": {
        _at("2026-10-07 00:00"): 0.0028,
        _at("2026-10-07 08:00"): -0.0015,
        _at("2026-10-07 16:00"): 0.0034,
        _at("2026-10-08 00:00"): 0.0051,
        _at("2026-10-08 08:00"): 0.0013,
    },
    "okx": {
        _at("2026-10-07 00:00"): 0.00418,
        _at("2026-10-07 08:00"): -0.00076,
        _at("2026-10-07 16:00"): 0.00243,
        _at("2026-10-08 00:00"): 0.00584,
        _at("2026-10-08 08:00"): -0.00247,
    },
}


@pytest.mark.parametrize("venue", ["binance", "gate", "okx"])
def test_the_funding_series_walks_the_venue_pages(recorded, frozen_clock, monkeypatch, venue):
    """A window wider than one page is stitched back together, ascending and without repeats.

    The page cap is lowered to 2 so the recordings cover the loop: three pages each, the last one
    short, which is where the same loop stops on a real thousand-row page. A cursor that repeats
    a page, skips one, or stops at the first page fails here.
    """
    monkeypatch.setattr(derivatives, _FUNDING_PAGE[venue], 2)

    series = derivatives.fetch_funding_history(
        "Crypto", _SYMBOL[venue], start=_WIDE_START, end=_WIDE_END
    )

    assert list(series.index) == sorted(_FUNDING_PAGED[venue])
    for stamp, expected in _FUNDING_PAGED[venue].items():
        assert series.loc[stamp] == pytest.approx(expected)
    assert len(recorded.calls) == 3


def test_the_open_interest_series_walks_the_venue_pages(recorded, monkeypatch):
    """Gate's stats rows come oldest-first and its ``to`` bounds the *newest* row returned.

    Two pages of three cover the window; the cursor moves to just below the oldest row of the page
    just read, and the loop stops once it has reached the requested start.
    """
    monkeypatch.setattr(derivatives, _OI_PAGE["gate"], 3)

    series = derivatives.fetch_open_interest_history(
        "Crypto", _SYMBOL["gate"], start=_OI_START, end=_OI_END
    )

    assert list(series.index) == sorted(_OI_EXPECTED["gate"])
    assert [params["to"] for _url, params in recorded.calls] == [1792022400, 1791763199]
