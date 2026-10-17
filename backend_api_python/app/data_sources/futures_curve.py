"""Deribit dated-futures metadata, expiry calendar and point-in-time daily closes.

The venue publishes each contract's settle history as a daily candle. A candle stamped at
``t`` covers the day that *starts* at ``t``, so its close is only knowable one day later. Series
returned here are indexed by that later instant (tz-naive UTC, the repository's convention), which
is what lets a point-in-time reader align a close with the bar on which it became known.

Requests that fail -- transport, HTTP status, venue error or undocumented payload -- answer an
empty series or ``None`` rather than raising; no caller substitutes another venue's number. Only
``Crypto`` and the underlyings in :data:`_UNDERLYINGS` exist here, and curve assembly itself lands
in a later change.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

import pandas as pd
import requests

_DERIBIT = "https://www.deribit.com/api/v2/public"
_MARKETS = frozenset({"Crypto"})
_UNDERLYINGS = frozenset({"BTC", "ETH"})
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_TIMEOUT = 20

_DAY = pd.Timedelta(days=1)
_FRIDAY = 4
# Monthly Deribit futures expire at 08:00 UTC on the last Friday of their month.
_EXPIRY_HOUR = 8


def _empty_series() -> pd.Series:
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))


def _utc_naive(value: datetime) -> pd.Timestamp:
    """Interpret a naive datetime as UTC and drop the zone; keep tz-aware input in UTC."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(None) if stamp.tzinfo is None else stamp.tz_convert("UTC").tz_localize(None)


def _epoch_ms(value: datetime) -> int:
    return int(_utc_naive(value).value // 1_000_000)


def _get(url: str, params: dict) -> dict:
    response = requests.get(url, params=params, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _underlying(symbol: str) -> str | None:
    base = symbol.split("@", 1)[0].split("/", 1)[0]
    if base.startswith("Crypto:"):
        base = base[len("Crypto:"):]
    base = base.upper()
    return base if base in _UNDERLYINGS else None


def _instrument_name(currency: str, day: datetime) -> str:
    return f"{currency}-{day.day}{_MONTHS[day.month - 1]}{day.year % 100:02d}"


def _last_friday(year: int, month: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - _FRIDAY) % 7)


def _monthly_expiries(currency: str, start: datetime, end: datetime) -> list[tuple[str, pd.Timestamp]]:
    """Every monthly contract from ``start``'s month through two months past ``end``'s.

    The tail past ``end`` keeps a curve readable near its right edge: a contract listed after the
    window still matters if it matures while the window is being measured.
    """
    stop_year, stop_month = end.year, end.month + 2
    if stop_month > 12:
        stop_year, stop_month = stop_year + 1, stop_month - 12
    rows = []
    year, month = start.year, start.month
    while (year, month) <= (stop_year, stop_month):
        expiry = _last_friday(year, month)
        rows.append((
            _instrument_name(currency, datetime(expiry.year, expiry.month, expiry.day)),
            pd.Timestamp(expiry.year, expiry.month, expiry.day, _EXPIRY_HOUR),
        ))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return rows


def _instrument(name: str) -> dict | None:
    """Creation and expiry instants of one contract, or ``None`` when the venue does not list it."""
    try:
        result = _get(f"{_DERIBIT}/get_instrument", {"instrument_name": name}).get("result")
        if not result:
            return None
        return {
            "creation": pd.Timestamp(result["creation_timestamp"], unit="ms", tz="UTC").tz_localize(None),
            "expiration": pd.Timestamp(result["expiration_timestamp"], unit="ms", tz="UTC").tz_localize(None),
        }
    except Exception:
        return None


def _daily_closes(name: str, start: datetime, end: datetime) -> pd.Series:
    """Daily closes of one contract, indexed one day after each candle's stamp (when it was known)."""
    try:
        body = _get(
            f"{_DERIBIT}/get_tradingview_chart_data",
            {
                "instrument_name": name,
                "start_timestamp": _epoch_ms(start),
                "end_timestamp": _epoch_ms(end),
                "resolution": "1D",
            },
        )
        result = body.get("result") or {}
        if result.get("status") != "ok":
            return _empty_series()
        ticks = result.get("ticks") or []
        closes = result.get("close") or []
        index = pd.to_datetime(ticks, unit="ms", utc=True).tz_localize(None) + _DAY
        series = pd.Series(closes, index=index, dtype="float64").sort_index(kind="mergesort")
        series = series[~series.index.duplicated(keep="last")]
    except Exception:
        return _empty_series()

    start_ts, end_ts = _utc_naive(start), _utc_naive(end)
    return series[(series.index >= start_ts) & (series.index <= end_ts)]


def fetch_term_structure(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Curve values for one underlying; assembled in a later change, empty for now."""
    return _empty_series()


def fetch_roll_cost(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Roll cost for one underlying; assembled in a later change, empty for now."""
    return _empty_series()
