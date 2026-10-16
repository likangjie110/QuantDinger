"""Funding-rate and open-interest history for one perpetual, one venue per request.

Two entry points, :func:`fetch_funding_history` and :func:`fetch_open_interest_history`. Both
return a tz-naive UTC ``pd.Series`` -- the index convention every panel in this repository uses --
and both return an empty series rather than raising when the market, the venue or the provider
cannot answer. Whether a market is *allowed* to read a derivative field is the field set's
decision (``factor_mining.dsl.fields_for``), so a market outside :data:`DERIVATIVES_MARKETS` gets
nothing from here instead of a substitute.

The two fields are fetched independently and share no state: one endpoint failing costs its own
series, not the other's.

Stamp semantics, measured against the venues on 2026-10-16 -- a factor reads these series
point-in-time, so what an index means is part of the contract:

* Funding rates are indexed by their **settlement instant** (Gate settles BTC at 00:00, 08:00 and
  16:00 UTC). The rate at ``t`` is the one that crystallized at ``t``. Binance stamps some rows a
  millisecond or three late; that reporting noise is floored away, because a settlement stamped
  00:00:00.002 would fall past a bar stamped 00:00:00 and be read one interval late.
* Open interest is indexed by the instant the observation became known. Binance stamps a daily row
  with the observation it holds, so its rows publish at their own stamp; Gate stamps a bucket's
  *last* observation at the bucket's *start* (a 1d row stamped 10-09 repeats the open interest the
  1h series reports at 10-09 23:00, and its ``last_funding_rate`` repeats the 10-10 00:00
  settlement), so its rows are published one bucket later. OKX's stamp semantics are unverified,
  and an unverified stamp takes the same one-bucket delay: publishing late can only cost freshness,
  never correctness.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import datetime

import pandas as pd
import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

# The markets whose panel carries derivative fields. A market outside this set has no source here:
# admitting a field is the field set's call, and an empty series is this module's way of saying it
# has nothing to add rather than overruling that call.
DERIVATIVES_MARKETS = frozenset({"Crypto"})
# A symbol that names no venue in its ``@venue`` suffix is read on this one -- the repository's
# default public crypto venue (``CRYPTO_PUBLIC_DEFAULT_EXCHANGE``).
DEFAULT_VENUE = "binance"

# The venue name each market's instrument has to be spelled in.
_INSTRUMENT_NAME = {
    "binance": "{}{}",       # BTCUSDT
    "gate": "{}_{}",         # BTC_USDT
    "okx": "{}-{}-SWAP",     # BTC-USDT-SWAP
}
_QUOTE_ASSETS = ("USDT", "USDC", "USD")

_BINANCE = "https://fapi.binance.com"
_GATE = "https://api.gateio.ws/api/v4/futures/usdt"
_OKX = "https://www.okx.com"

# Page ceilings as the venues document them. They are also what the tests lower to walk the paging
# loops against recordings that fit in a fixture.
_BINANCE_FUNDING_PAGE = 1000    # /fapi/v1/fundingRate limit ceiling
_BINANCE_OI_PAGE = 500          # /futures/data/openInterestHist limit ceiling
_GATE_FUNDING_PAGE = 1000       # /funding_rate limit ceiling
_GATE_OI_PAGE = 1000            # /contract_stats limit ceiling
_OKX_FUNDING_PAGE = 100         # /public/funding-rate-history limit ceiling
_OKX_OI_PAGE = 100              # /open-interest-history limit ceiling
# Runaway guard. Each loop also breaks when a cursor stops advancing, so this only bounds what a
# venue would have to do to make the walk unbounded: answer with pages that advance and never end.
_MAX_PAGES = 200
_TIMEOUT_SECONDS = 10
_DAY_MS = 86_400_000
_MS_PER_SECOND = 1000
# Gate refuses a funding ``from`` older than this and keeps no deeper history.
_GATE_FUNDING_LOOKBACK_SECONDS = 180 * 86_400


def fetch_funding_history(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Realized funding rates for one perpetual, one value per settlement, **in percent**.

    The index is each rate's settlement instant (tz-naive UTC, whole seconds). The value at ``t`` is
    the rate that crystallized at ``t``, which is also when it became known, so a point-in-time
    reader takes the last settlement at or before its bar and no earlier bar sees it. Points are
    spaced by the venue's own funding interval -- 8h on BTC, 4h or 1h on some contracts -- and are
    never annualised and never rescaled to the caller's bar size: a 4h settlement is the rate paid
    over those four hours, and a series mixing both intervals stays readable as such.

    Values are percent per settlement, the unit the repository reports as ``funding_rate``
    (``professional_report`` stores the same field as ``unit: percent`` over a ``decimal`` source),
    so a venue decimal of ``0.0001`` comes back as ``0.01``.

    ``start`` and ``end`` bound the series inclusively. An unsupported market, an unsupported
    venue, a symbol with no base/quote pair, or a provider that fails before its first page gives
    an empty series instead of raising; a provider that fails after some pages gives the rows
    already collected, which are the recent end of the window -- the half a factor reads -- and
    never a silently shortened one.
    """
    return _history(market=market, symbol=symbol, start=start, end=end, field="funding")


def fetch_open_interest_history(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Open interest **levels** for one perpetual, in USD notional, one value per daily bucket.

    A level, not a change: a change rate is something a factor derives, and a source that
    pre-computed one would make that choice for every candidate. The three venues report the same
    quantity (``open_interest_usd``, ``sumOpenInterestValue``, ``oiUsd``) even though the contract
    counts behind it differ, and it is *not* in the base asset's units.

    The index is the instant the observation became known (tz-naive UTC): where a venue prints a
    bucket's last observation at the bucket's *start*, the row is published one bucket later, so no
    value is readable before it was observed. See the module docstring for what was measured per
    venue; a reader aligning this series onto bars forward-fills from these instants.

    Bounds, empty answers and partial failures behave as in :func:`fetch_funding_history`.
    """
    return _history(market=market, symbol=symbol, start=start, end=end, field="open_interest")


def _history(*, market: str, symbol: str, start: datetime, end: datetime, field: str) -> pd.Series:
    """The one path both entry points take: decide, fetch, filter, index."""
    if market not in DERIVATIVES_MARKETS:
        return _empty()
    parsed = _instrument(symbol, market)
    if parsed is None:
        logger.debug("no base/quote pair in %r", symbol)
        return _empty()
    venue, base, quote = parsed
    fetcher = _FETCHERS.get((venue, field))
    template = _INSTRUMENT_NAME.get(venue)
    if fetcher is None or template is None:
        logger.debug("no %s history for venue %r", field, venue)
        return _empty()
    lower, upper = _utc(start), _utc(end)
    if lower > upper:
        return _empty()
    return _series(fetcher(template.format(base, quote), lower, upper), lower, upper)


def _instrument(symbol: str, market: str) -> tuple[str, str, str] | None:
    """``Crypto:BTC/USDT@binance:swap`` -> ``("binance", "BTC", "USDT")``, or ``None``.

    The venue is read from the ``@venue`` suffix of a portfolio member key, because the caller's
    signature carries no exchange argument and the key is where the run recorded one. A bare pair
    (``BTC/USDT``, or the unified ``BTC/USDT:USDT``) takes :data:`DEFAULT_VENUE`; a caller that
    must not read another venue's funding passes the key.
    """
    text = str(symbol or "").strip()
    if not text:
        return None
    head, _, suffix = text.partition("@")
    venue, _, _market_type = suffix.partition(":")
    venue = venue.strip().lower() or DEFAULT_VENUE
    # ``Crypto:BTC/USDT`` prefixes the market, ``BTC/USDT:USDT`` suffixes the settle asset.
    prefix = f"{market}:"
    if head[: len(prefix)].lower() == prefix.lower():
        head = head[len(prefix):]
    base, quote = _pair(head.partition(":")[0].strip().upper())
    if not base or not quote:
        return None
    return venue, base, quote


def _pair(token: str) -> tuple[str, str]:
    """``BTC/USDT``, ``BTC-USDT-SWAP`` and ``BTCUSDT`` all name their base and quote asset."""
    for separator in ("/", "-", "_"):
        if separator in token:
            parts = token.split(separator)
            return parts[0], parts[1]
    for quote in _QUOTE_ASSETS:
        if token.endswith(quote) and len(token) > len(quote):
            return token[: -len(quote)], quote
    return "", ""


def _utc(value: datetime) -> pd.Timestamp:
    """A tz-naive UTC timestamp, the convention every panel index in this repository uses."""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC")
    return stamp.tz_localize(None)


def _millis(stamp: pd.Timestamp) -> int:
    return int(stamp.tz_localize("UTC").value // 1_000_000)


def _seconds(stamp: pd.Timestamp) -> int:
    return _millis(stamp) // _MS_PER_SECOND


def _number(value: object) -> float | None:
    """``None`` for anything that is not a finite number, so one bad row cannot cost a page."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _get(url: str, params: dict) -> object:
    response = requests.get(
        url, params=params, headers={"Accept": "application/json"}, timeout=_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json()


def _rows(
    payload: object,
    *,
    time_key: str,
    value_keys: tuple[str, ...],
    scale: float,
    time_unit: str = "ms",
) -> list[tuple[int, float]]:
    """Venue rows -> ascending ``(ms timestamp, value)``, dropping rows that are not numbers.

    ``payload`` is the venue's list of mappings, or a mapping whose ``data`` list holds them.
    ``value_keys`` is tried in order, so a venue reporting both a capped and a realized rate keeps
    the one the repository reads first. ``scale`` puts the rate into percent, which is the only
    unit this module converts: a timestamp is carried through as the venue printed it.
    """
    items = payload.get("data") if isinstance(payload, dict) else payload
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        stamp = _number(item.get(time_key))
        value = next((found for key in value_keys if (found := _number(item.get(key))) is not None), None)
        if stamp is None or value is None:
            continue
        millis = int(stamp) if time_unit == "ms" else int(stamp) * _MS_PER_SECOND
        rows.append((millis, value * scale))
    rows.sort()
    return rows


def _series(rows: Iterable[tuple[int, float]], lower: pd.Timestamp, upper: pd.Timestamp) -> pd.Series:
    """Ascending series over the requested window, one point per timestamp, in whole seconds.

    Timestamps are floored to the second before the window is applied. Binance prints real
    sub-second offsets on some settlements (``...600002``, recorded 2026-10-16): they are reporting
    noise around a scheduled instant, and a settlement stamped 00:00:00.002 would otherwise fall
    past a bar stamped 00:00:00 and be read one funding interval late by a forward-fill.
    """
    lower_ms, upper_ms = _millis(lower), _millis(upper)
    frame = pd.DataFrame(list(rows), columns=["time", "value"])
    if frame.empty:
        return _empty()
    frame["time"] = frame["time"] // _MS_PER_SECOND * _MS_PER_SECOND
    frame = frame[(frame["time"] >= lower_ms) & (frame["time"] <= upper_ms)]
    frame = frame.drop_duplicates(subset="time", keep="last").sort_values("time")
    if frame.empty:
        return _empty()
    stamps = pd.DatetimeIndex(pd.to_datetime(frame["time"].to_numpy(), unit="ms", utc=True)).tz_convert(None)
    return pd.Series(frame["value"].to_numpy(dtype="float64"), index=stamps.as_unit("ns"))


def _empty() -> pd.Series:
    """An empty, correctly indexed series: a caller can align it without asking what it holds."""
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))


def _binance_funding(instrument: str, lower: pd.Timestamp, upper: pd.Timestamp) -> list[tuple[int, float]]:
    """Oldest-first settlements, paged forward from ``startTime``.

    Binance serves its full funding history, so a window wider than one page walks on from one ms
    past the newest row read. That millisecond matters: production rows carry real offsets
    (``...600002``), so a page cannot be resumed at the settlement it ended on.
    """
    rows: list[tuple[int, float]] = []
    cursor, end = _millis(lower), _millis(upper)
    try:
        for _ in range(_MAX_PAGES):
            page = _rows(
                _get(f"{_BINANCE}/fapi/v1/fundingRate", {
                    "symbol": instrument, "startTime": cursor, "endTime": end,
                    "limit": _BINANCE_FUNDING_PAGE,
                }),
                time_key="fundingTime", value_keys=("fundingRate",), scale=100.0,
            )
            if not page:
                break
            rows.extend(page)
            newest = page[-1][0]
            following = newest + 1
            if newest >= end or following <= cursor or len(page) < _BINANCE_FUNDING_PAGE:
                break
            cursor = following
    except Exception as exc:
        logger.warning("binance funding history for %s stopped after %d rows: %s", instrument, len(rows), exc)
    return rows


def _binance_open_interest(instrument: str, lower: pd.Timestamp, upper: pd.Timestamp) -> list[tuple[int, float]]:
    """Daily USD notional, asked by ``endTime`` alone.

    Binance keeps one month of open interest and rejects an older ``startTime`` outright
    (``-1130``), so a windowed request would turn a long backtest into an empty column: the month
    it does keep is what this asks for, and the caller's bounds filter the answer.
    """
    try:
        return _rows(
            _get(f"{_BINANCE}/futures/data/openInterestHist", {
                "symbol": instrument, "period": "1d", "endTime": _millis(upper),
                "limit": _BINANCE_OI_PAGE,
            }),
            time_key="timestamp", value_keys=("sumOpenInterestValue",), scale=1.0,
        )
    except Exception as exc:
        logger.warning("binance open interest history for %s failed: %s", instrument, exc)
        return []


def _gate_funding(instrument: str, lower: pd.Timestamp, upper: pd.Timestamp) -> list[tuple[int, float]]:
    """Newest-first settlements, paged backwards.

    ``from`` is inclusive and ``to`` exclusive, so a window ending on a settlement asks for one
    second past it -- asking for the settlement itself would drop it. Gate keeps 180 days and
    refuses an older ``from``, so the floor is clamped to what it will answer; a window ending
    before that floor has no Gate funding to give.
    """
    rows: list[tuple[int, float]] = []
    floor = max(_seconds(lower), _seconds(upper) - _GATE_FUNDING_LOOKBACK_SECONDS)
    cursor = _seconds(upper) + 1
    try:
        for _ in range(_MAX_PAGES):
            page = _rows(
                _get(f"{_GATE}/funding_rate", {
                    "contract": instrument, "from": floor, "to": cursor, "limit": _GATE_FUNDING_PAGE,
                }),
                time_key="t", value_keys=("r",), scale=100.0, time_unit="s",
            )
            if not page:
                break
            rows.extend(page)
            oldest = page[0][0] // _MS_PER_SECOND
            if oldest <= floor or oldest >= cursor:
                break
            cursor = oldest
    except Exception as exc:
        logger.warning("gate funding history for %s stopped after %d rows: %s", instrument, len(rows), exc)
    return rows


def _gate_open_interest(instrument: str, lower: pd.Timestamp, upper: pd.Timestamp) -> list[tuple[int, float]]:
    """Daily USD notional, paged backwards on ``to``, published a bucket after its stamp.

    Gate answers the newest ``limit`` rows at or below ``to``, oldest first, and ignores ``from``
    (it validates that parameter against its own 180-day rule and then returns the whole tail), so
    the walk is anchored on ``to`` alone, moving to one second below the oldest row just read.
    """
    rows: list[tuple[int, float]] = []
    cursor = _seconds(upper)
    try:
        for _ in range(_MAX_PAGES):
            page = _rows(
                _get(f"{_GATE}/contract_stats", {
                    "contract": instrument, "interval": "1d", "to": cursor, "limit": _GATE_OI_PAGE,
                }),
                time_key="time", value_keys=("open_interest_usd",), scale=1.0, time_unit="s",
            )
            if not page:
                break
            rows.extend(page)
            oldest = page[0][0] // _MS_PER_SECOND
            if oldest <= _seconds(lower):
                break
            following = oldest - 1
            if following >= cursor:
                break
            cursor = following
    except Exception as exc:
        logger.warning("gate open interest history for %s stopped after %d rows: %s", instrument, len(rows), exc)
    return [(stamp + _DAY_MS, value) for stamp, value in rows]


def _okx_funding(instrument: str, lower: pd.Timestamp, upper: pd.Timestamp) -> list[tuple[int, float]]:
    """Newest-first settlements, paged backwards on ``after`` (records strictly earlier than it)."""
    rows: list[tuple[int, float]] = []
    cursor = _millis(upper) + 1
    try:
        for _ in range(_MAX_PAGES):
            page = _rows(
                _get(f"{_OKX}/api/v5/public/funding-rate-history", {
                    "instId": instrument, "after": cursor, "limit": _OKX_FUNDING_PAGE,
                }),
                time_key="fundingTime", value_keys=("realizedRate", "fundingRate"), scale=100.0,
            )
            if not page:
                break
            rows.extend(page)
            oldest = page[0][0]
            if oldest <= _millis(lower) or oldest >= cursor:
                break
            cursor = oldest
    except Exception as exc:
        logger.warning("okx funding history for %s stopped after %d rows: %s", instrument, len(rows), exc)
    return rows


def _okx_open_interest(instrument: str, lower: pd.Timestamp, upper: pd.Timestamp) -> list[tuple[int, float]]:
    """Daily USD notional, paged backwards on the exclusive ``end``, published a bucket later."""
    rows: list[tuple[int, float]] = []
    cursor = _millis(upper) + 1
    try:
        for _ in range(_MAX_PAGES):
            page = _okx_interest_rows(
                _get(f"{_OKX}/api/v5/rubik/stat/contracts/open-interest-history", {
                    "instId": instrument, "period": "1D", "end": cursor, "limit": _OKX_OI_PAGE,
                })
            )
            if not page:
                break
            rows.extend(page)
            oldest = page[0][0]
            if oldest <= _millis(lower) or oldest >= cursor:
                break
            cursor = oldest
    except Exception as exc:
        logger.warning("okx open interest history for %s stopped after %d rows: %s", instrument, len(rows), exc)
    return [(stamp + _DAY_MS, value) for stamp, value in rows]


def _okx_interest_rows(payload: object) -> list[tuple[int, float]]:
    """``[ts, oi, oiCcy, oiUsd]`` rows -> ascending USD notional.

    The statistics rows are lists rather than mappings, and a three-field row is the currency
    aggregate: it names no contract and carries no USD notional, so it is skipped rather than read
    as one.
    """
    items = payload.get("data") if isinstance(payload, dict) else payload
    rows = []
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        stamp, value = _number(item[0]), _number(item[3])
        if stamp is None or value is None:
            continue
        rows.append((int(stamp), value))
    rows.sort()
    return rows


_Fetcher = Callable[[str, pd.Timestamp, pd.Timestamp], list[tuple[int, float]]]
# One table, so "which venues does this field know" has one answer and an unlisted pair is an empty
# series rather than an exception raised from a dispatch branch nobody reads.
_FETCHERS: dict[tuple[str, str], _Fetcher] = {
    ("binance", "funding"): _binance_funding,
    ("binance", "open_interest"): _binance_open_interest,
    ("gate", "funding"): _gate_funding,
    ("gate", "open_interest"): _gate_open_interest,
    ("okx", "funding"): _okx_funding,
    ("okx", "open_interest"): _okx_open_interest,
}
