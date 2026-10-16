"""Funding-rate and open-interest history for one perpetual, one venue per request.

Two entry points, :func:`fetch_funding_history` and :func:`fetch_open_interest_history`. Both
return a tz-naive UTC ``pd.Series`` -- the index convention every panel in this repository uses --
and both return an empty series rather than raising when the market, the venue or the provider
cannot answer. Whether a market is *allowed* to read a derivative field is the field set's
decision (``factor_mining.dsl.fields_for``), so a market outside :data:`DERIVATIVES_MARKETS` gets
nothing from here instead of a substitute.

The two fields are fetched independently and share no state: one endpoint failing costs its own
series, not the other's.

Two of the venues keep only a window of history and refuse requests that reach past it, so a window
older than the edge answers with nothing even though the field exists: Gate's funding history is 180
days measured on *its* clock, and Binance's open interest is one month (an older ``startTime`` is
rejected outright with ``-1130``, which is why that endpoint is asked by its end alone).

Stamp semantics, measured against the venues on 2026-10-16 -- a factor reads these series
point-in-time, so what an index means is part of the contract:

* Every stamp is rounded to the minute it names (:func:`_on_the_minute`): settlements are scheduled
  on the minute grid and the venues miss it by seconds, so the index lands on the same grid as any
  bar a reader can align it to.
* Funding rates are indexed by their **settlement instant** (Gate settles BTC at 00:00, 08:00 and
  16:00 UTC). The rate at ``t`` is the one that crystallized at ``t``.
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

from app.config.data_sources import CryptoPublicConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

# The markets whose panel carries derivative fields. A market outside this set has no source here:
# admitting a field is the field set's call, and an empty series is this module's way of saying it
# has nothing to add rather than overruling that call.
DERIVATIVES_MARKETS = frozenset({"Crypto"})
# The floor for a default venue this module cannot read. The default itself is not a constant here:
# it is the app's configured public crypto venue, read at call time by :func:`_default_venue` -- a
# deployment pointed at another venue must not be handed Binance's funding and open interest under
# that venue's prices, which is a plausible-looking wrong number rather than a missing one.
_FALLBACK_VENUE = "binance"

# The venue name each market's instrument has to be spelled in.
_INSTRUMENT_NAME = {
    "binance": "{}{}",       # BTCUSDT
    "gate": "{}_{}",         # BTC_USDT
    "okx": "{}-{}-SWAP",     # BTC-USDT-SWAP
}
_QUOTE_ASSETS = ("USDT", "USDC", "USD")

# The product words a member key can carry where an exchange id would otherwise sit, because a key
# with no exchange id spells its market type alone (``Crypto:BTC/USDT@swap``). The vocabulary is
# the repository's own (``professional_report.features.crypto`` and ``data_sources.native_crypto``).
# Only a perpetual has funding and open interest: a key that names any other product (``@spot``, or
# a word this module cannot place) gets nothing rather than a perpetual's numbers.
_PERPETUAL_PRODUCTS = frozenset({
    "future", "futures", "linear", "perp", "perpetual", "perpetual_contract", "perpetual_future",
    "perpetual_futures", "perpetual_swap", "swap",
})
_SPOT_PRODUCTS = frozenset({"cash", "spot"})
_PRODUCT_WORDS = _PERPETUAL_PRODUCTS | _SPOT_PRODUCTS

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
_MINUTE_MS = 60_000
# Gate refuses a funding ``from`` older than 180 days by its own clock and keeps no deeper history.
# The clamp stays an hour inside that edge: the venue's clock is ahead of this one by the request's
# round trip, and a request a second past the line is refused rather than trimmed.
_GATE_FUNDING_LOOKBACK_SECONDS = 180 * 86_400 - 3600


def fetch_funding_history(market: str, symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Realized funding rates for one perpetual, one value per settlement, **in percent**.

    The index is each rate's settlement instant (tz-naive UTC, rounded to the minute). The value at
    ``t`` is the rate that crystallized at ``t``, which is also when it became known, so a
    point-in-time reader takes the last settlement at or before its bar and no earlier bar sees it.
    Points are spaced by the venue's own funding interval -- 8h on BTC, 4h or 1h on some contracts
    -- and are never annualised and never rescaled to the caller's bar size: a 4h settlement is the
    rate paid over those four hours, and a series mixing both intervals stays readable as such.

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


def _default_venue() -> str:
    """The app's configured public crypto venue, as of this call.

    ``CRYPTO_PUBLIC_DEFAULT_EXCHANGE`` is what the rest of the repository reads to decide where a
    bare crypto symbol trades, so this module reads the same setting at the same moment rather than
    a copy of its factory value: a deployment pointed at another venue would otherwise get that
    venue's prices next to Binance's funding rates.
    """
    return str(CryptoPublicConfig.DEFAULT_EXCHANGE or "").strip().lower() or _FALLBACK_VENUE


def _history(*, market: str, symbol: str, start: datetime, end: datetime, field: str) -> pd.Series:
    """The one path both entry points take: decide, fetch, filter, index."""
    if market not in DERIVATIVES_MARKETS:
        return _empty()
    parsed = _instrument(symbol, market)
    if parsed is None:
        logger.debug("%r names no perpetual base/quote pair", symbol)
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
    try:
        return _series(fetcher(template.format(base, quote), lower, upper), lower, upper)
    except Exception as exc:
        # Assembling the answer is part of answering. A window no calendar holds raises the moment
        # it is turned into milliseconds, and a caller promised an empty series must get one rather
        # than an OverflowError from three frames down.
        logger.warning("%s history for %r (%s) could not be assembled: %s", field, symbol, venue, exc)
        return _empty()


def _instrument(symbol: str, market: str) -> tuple[str, str, str] | None:
    """``Crypto:BTC/USDT@binance:swap`` -> ``("binance", "BTC", "USDT")``, or ``None``.

    A member key spells its venue as ``@exchange_id[:market_type]``, or the market type alone when
    the run recorded no exchange id (``Crypto:BTC/USDT@swap``, a key this repository really
    builds). So a suffix whose first word is a product word names a market type, not a venue, and
    such a key reads on the default venue -- the same rule a bare pair follows. A key whose product
    is not a perpetual names no funding rate and no open interest and returns ``None`` rather than a
    perpetual's numbers under a spot symbol. The default venue is read from the app's configuration
    per call (:func:`_default_venue`), not fixed here.
    """
    text = str(symbol or "").strip()
    if not text:
        return None
    head, _, suffix = text.partition("@")
    first, _, rest = suffix.partition(":")
    first, rest = first.strip().lower(), rest.strip().lower()
    if not first:
        venue, product = _default_venue(), ""
    elif first in _PRODUCT_WORDS:
        venue, product = _default_venue(), first
    else:
        venue, product = first, rest
    if product and product not in _PERPETUAL_PRODUCTS:
        return None
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


def _now() -> pd.Timestamp:
    """The wall clock, tz-naive UTC.

    The one clock read here, and it is not optional: Gate measures its 180-day funding limit
    against *its* clock, so a floor computed from the caller's window alone asks for a ``from``
    Gate refuses whenever that window ends in the past -- and a factor-mining window ends on the
    last closed bar, which is yesterday.
    """
    return _utc(pd.Timestamp.now(tz="UTC"))


def _on_the_minute(millis: int) -> int:
    """A stamp rounded to the minute it names.

    Settlements are scheduled on the minute grid, but the venues' stamps miss it by seconds: Gate's
    rows older than a month sit one to two seconds late (measured 2026-10-16: 40- and 178-day-old
    rows at ``t % 60 == 2``, 90- and 150-day-old rows at 1, recent rows exact), and Binance prints
    sub-second offsets. Left alone, a settlement stamped one second past midnight is not visible to
    a bar stamped midnight at all, so a forward-filled column lags one funding period -- and only
    over the stretch of history where the venue's stamps drifted, which no coverage gate can see.
    Rounding is bounded by thirty seconds, and a minute is the finest bar this repository trades.
    """
    return (millis + _MINUTE_MS // 2) // _MINUTE_MS * _MINUTE_MS


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
    """Ascending series over the requested window, one point per timestamp, on the minute grid.

    Stamps are rounded to the minute (see :func:`_on_the_minute`) before the window is applied, so
    the same window selects the same settlements on every venue.
    """
    lower_ms, upper_ms = _millis(lower), _millis(upper)
    frame = pd.DataFrame(list(rows), columns=["time", "value"])
    if frame.empty:
        return _empty()
    frame["time"] = frame["time"].map(_on_the_minute)
    frame = frame[(frame["time"] >= lower_ms) & (frame["time"] <= upper_ms)]
    # The venues page with exclusive cursors, so a repeated instant means a page was read twice and
    # the later page wins -- a rule about the provider's own row order, not about the values.
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
    refuses an older ``from`` against its own clock (measured 2026-10-16: ``now - 180d`` answers,
    ``now - 180d - 1h`` answers ``from time exceeds 180-day limit``), so the floor is the latest of
    the window's start, the window's own lookback, and the clock's -- without the clock's, every
    window ending in the past asks for a ``from`` Gate refuses and loses the tail it does hold. A
    window that ends before the venue's edge has no Gate funding to give and is not asked for.
    """
    rows: list[tuple[int, float]] = []
    floor = max(
        _seconds(lower),
        _seconds(upper) - _GATE_FUNDING_LOOKBACK_SECONDS,
        _seconds(_now()) - _GATE_FUNDING_LOOKBACK_SECONDS,
    )
    if floor > _seconds(upper):
        logger.debug("gate funding window for %s ends before the venue's %d-day edge",
                     instrument, _GATE_FUNDING_LOOKBACK_SECONDS // 86_400)
        return rows
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
            # A short page is the window's end (`limit` returns at most that many rows), and the
            # floor covers a window whose start the venue answered to the row.
            if len(page) < _GATE_FUNDING_PAGE or oldest <= floor or oldest >= cursor:
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

    UNVERIFIED, and the one thing here that would fail *silently*: OKX is unreachable from this
    host, so the column order comes from the endpoint's documented shape and ccxt's live-verified
    client rather than from a recorded body. A wrong order yields a plausible number of the wrong
    kind, which no coverage gate can catch. The verification owed is a networked smoke call that
    asserts the magnitude, not merely that the series is non-empty: the value must read as USD
    notional (``> 1e6``, and the same order as ``oi`` x price). If OKX answers mapping rows instead
    of lists, read the fields by name here rather than by position.
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
