"""Deribit dated-futures metadata, expiry calendar and point-in-time daily closes.

The venue publishes each contract's settle history as a daily candle. A candle stamped at
``t`` covers the day that *starts* at ``t``, so its close is only knowable one day later. Series
returned here are indexed by that later instant (tz-naive UTC, the repository's convention), which
is what lets a point-in-time reader align a close with the bar on which it became known.

Requests that fail -- transport, HTTP status, venue error or undocumented payload -- answer an
empty series or ``None`` rather than raising; no caller substitutes another venue's number. Only
``Crypto`` and the underlyings in :data:`_UNDERLYINGS` exist here.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from functools import lru_cache

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


_ROLL_DAYS = 7  # the main contract is the nearest monthly with at least this many days to expiry
_CURVE_FIELDS = ("term_structure", "roll_cost")


@lru_cache(maxsize=32)
def _curve(underlying: str, start: datetime, end: datetime, *,
           instrument=_instrument, daily_closes=_daily_closes) -> pd.DataFrame:
    """Point-in-time term structure and roll cost of one underlying's monthly futures.

    The grid is the union of the contracts' close instants inside ``[start, end]``; each such
    instant is when a close became known, so a value at ``t`` never reads a later one. A contract
    joins the universe only between its creation and expiry, and its last known close is carried
    forward between candle instants. A point that needs a missing leg is NaN -- a single
    contract's price is never presented as a spread.

    The main contract is the nearest eligible one with at least ``_ROLL_DAYS`` days to expiry and
    the far leg is the next eligible contract after it. ``term_structure`` annualizes the far /
    main spread over the expiries' calendar-day gap; ``roll_cost`` keeps it un-annualized.

    The cache is deliberate: ``market_fields`` asks for both fields of one window, and one fetch
    of the venue should answer both. Its key includes the injected functions, so a test stand-in
    never reuses a live answer.
    """
    start_ts, end_ts = _utc_naive(start), _utc_naive(end)
    listed = []
    for name, _expiry in _monthly_expiries(underlying, start_ts, end_ts):
        meta = instrument(name)
        if meta is not None:
            listed.append((meta["expiration"], name, meta["creation"]))
    listed.sort()

    series = {name: daily_closes(name, start, end) for _expiration, name, _creation in listed}
    grid = pd.DatetimeIndex([])
    for closes in series.values():
        grid = grid.union(closes.index)
    grid = grid[(grid >= start_ts) & (grid <= end_ts)]

    legs = [(expiration, creation, series[name].reindex(grid, method="ffill"))
            for expiration, name, creation in listed]

    term_structure: list[float] = []
    roll_cost: list[float] = []
    for t in grid:
        eligible = [leg for leg in legs if leg[1] <= t < leg[0]]
        main_at = next((i for i, leg in enumerate(eligible)
                        if leg[0] - t >= _ROLL_DAYS * _DAY), None)
        term = roll = float("nan")
        if main_at is not None and main_at + 1 < len(eligible):
            main_expiry, _main_creation, main_closes = eligible[main_at]
            far_expiry, _far_creation, far_closes = eligible[main_at + 1]
            main_price, far_price = main_closes.get(t), far_closes.get(t)
            days = (far_expiry - main_expiry).days
            if days > 0 and pd.notna(main_price) and pd.notna(far_price):
                roll = far_price / main_price - 1.0
                term = roll * 365.0 / days
        term_structure.append(term)
        roll_cost.append(roll)

    return pd.DataFrame(
        {"term_structure": term_structure, "roll_cost": roll_cost},
        index=grid,
        dtype="float64",
    )


def _curve_field(market: str, symbol: str, field: str, *, start: datetime, end: datetime) -> pd.Series:
    """One curve field for one member symbol; empty for an unsupported market or underlying."""
    underlying = _underlying(symbol) if market in _MARKETS else None
    if underlying is None:
        return _empty_series()
    curve = _curve(underlying, start, end)
    return curve[field] if not curve.empty else _empty_series()


def fetch_term_structure(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Annualized spread between the main and the next eligible monthly contract, point in time."""
    return _curve_field(market, symbol, "term_structure", start=start, end=end)


def fetch_roll_cost(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Un-annualized spread between the main and the next eligible monthly contract, point in time."""
    return _curve_field(market, symbol, "roll_cost", start=start, end=end)
