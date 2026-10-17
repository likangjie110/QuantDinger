# tests/test_factor_mining_futures_curve.py
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from app.data_sources import futures_curve

_FIXTURES = Path(__file__).parent / "fixtures" / "deribit"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Unrecorded(BaseException):
    """A request no fixture answers; not an Exception, so the source cannot swallow it."""


def _canonical(params):
    return tuple(sorted((str(key), str(value)) for key, value in (params or {}).items()))


def _load_bodies():
    bodies = {}
    for path in sorted(_FIXTURES.glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for item in fixture["responses"]:
            bodies[(item["url"], _canonical(item["params"]))] = item["body"]
    return bodies


@pytest.fixture(autouse=True)
def _recorded(monkeypatch):
    bodies = _load_bodies()
    def get(url, *, params=None, **_kwargs):
        key = (url, _canonical(params))
        if key not in bodies:
            raise _Unrecorded(f"unrecorded request: {url} {params}")
        return _Response(bodies[key])
    monkeypatch.setattr(futures_curve.requests, "get", get)


def test_monthly_expiry_is_the_last_friday_at_0800_utc():
    rows = futures_curve._monthly_expiries("BTC", datetime(2026, 8, 1), datetime(2026, 10, 31))
    assert ("BTC-25SEP26", pd.Timestamp("2026-09-25 08:00")) in rows
    assert ("BTC-30OCT26", pd.Timestamp("2026-10-30 08:00")) in rows
    assert all(expiry.weekday() == 4 and expiry.hour == 8 for _name, expiry in rows)

def test_instrument_name_does_not_zero_pad_the_day():
    assert futures_curve._instrument_name("BTC", datetime(2026, 11, 6)) == "BTC-6NOV26"

def test_archived_contract_metadata_is_read_from_the_recorded_body():
    meta = futures_curve._instrument("BTC-25SEP26")
    assert meta["creation"] == pd.Timestamp("2025-09-26 08:00:21")
    assert meta["expiration"] == pd.Timestamp("2026-09-25 08:00")

def test_an_unknown_instrument_is_skipped_not_raised():
    assert futures_curve._instrument("BTC-25JAN27") is None

def test_daily_close_is_indexed_when_it_became_known():
    series = futures_curve._daily_closes(
        "BTC-25DEC26", datetime(2026, 8, 1), datetime(2026, 10, 2))
    # The source answers with the candle containing the requested start
    # (raw tick 2026-07-31 08:00, close 64035.0); it is only known a day later.
    assert series.index[0] == pd.Timestamp("2026-08-01 08:00")
    assert series.iloc[0] == pytest.approx(64035.0)

def test_an_underlying_outside_btc_eth_returns_an_empty_series():
    empty = futures_curve.fetch_term_structure(
        "Crypto", "Crypto:SOL/USDT@swap", start=datetime(2026, 8, 1), end=datetime(2026, 10, 1))
    assert empty.empty

def test_a_failed_request_returns_an_empty_series(monkeypatch):
    import requests
    def fail(*_args, **_kwargs):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(futures_curve.requests, "get", fail)
    empty = futures_curve._daily_closes("BTC-25DEC26", datetime(2026, 8, 1), datetime(2026, 10, 2))
    assert empty.empty

def test_underlying_parses_btc_and_eth_and_refuses_everything_else():
    assert futures_curve._underlying("Crypto:BTC/USDT@swap") == "BTC"
    assert futures_curve._underlying("Crypto:ETH/USDT") == "ETH"
    assert futures_curve._underlying("Crypto:SOL/USDT@swap") is None
