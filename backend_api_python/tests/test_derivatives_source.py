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
    return transport


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
def test_funding_history_is_normalised_to_a_per_interval_rate(recorded, venue):
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


@pytest.mark.parametrize("venue", ["binance", "gate", "okx"])
def test_open_interest_history_is_returned_as_a_level_series(recorded, monkeypatch, venue):
    """The open interest *level* per bucket, so a change rate stays something a factor derives.

    These arrive as USD notional in the billions; a series of percent changes would be five
    orders of magnitude smaller, and the widest step in the recording is under 5%.

    Gate's page cap is lowered to the three rows the recordings answer at: the loop is a walk over
    ``to``, so its page width changes how many requests a window takes, not what the series holds.
    Binance and OKX answer this window within one page at their real caps, as recorded.
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


@pytest.mark.parametrize("symbol", ["BTC", "@binance:swap", ":"])
def test_a_symbol_without_a_pair_returns_an_empty_series(recorded, symbol):
    series = derivatives.fetch_open_interest_history(
        "Crypto", symbol, start=_OI_START, end=_OI_END
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
        _at("2026-10-08 08:00"): 0.00112,
    },
}


@pytest.mark.parametrize("venue", ["binance", "gate", "okx"])
def test_the_funding_series_walks_the_venue_pages(recorded, monkeypatch, venue):
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
