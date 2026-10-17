"""Cross-market field assembly: the panel's derivative columns and the run's own calendar.

Two questions about a market are answered here, both of them before a candidate is scored.

The first is what a market's non-price fields *hold*. ``dsl.fields_for`` decides which fields a
market may name; this module decides what those fields carry, by fetching one series per symbol
per field and aligning it to the bars the panel already has. The alignment is the contract, and
it is the sources' own stamp semantics that make it point-in-time: a series is indexed by the
instant its value became known -- a funding settlement, or the publication of an open-interest
observation (``data_sources.derivatives`` measures both) -- and a bar may read only the last
value published at or before it. ``reindex(..., method="ffill")`` is exactly that and no more: a
bar before the first publication stays NaN, which keeps a missing opening stretch missing rather
than filled with the first number that ever appeared, and no bar ever reads a value from its own
future -- including from a source that answers with a wider window than it was asked for.
The term-structure fields come from Deribit's dated-futures curve, where a daily close is
indexed one day after the candle tick, the instant it became known (``data_sources.futures_curve``).

The second is the market's session calendar. The search window and the frozen tail are cut by
the last ``freeze_ratio`` of a market's sessions, so a market counted on the wrong calendar gets
a tail whose boundary is not where its own share of the range falls -- on every run, in the one
segment a run is judged by -- and a session count that is not a count of anything the market
trades. A market that trades every calendar day is counted in calendar days; every other
supported market trades on weekdays and has no holiday calendar at this layer, so it is counted
in business days, which is what the whole repository counted before this module existed.

Nothing here reaches a database, and the derivative sources are imported rather than wired so a
test can stand in for them; the provider calls :func:`enrich_frames` and :func:`sessions` and owns
both decisions' windows.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.data_sources.derivatives import fetch_funding_history, fetch_open_interest_history
from app.data_sources.futures_curve import fetch_roll_cost, fetch_term_structure
from app.services.factor_mining.dsl import fields_for

# The derivative fields this module can fill. A field here that the field set does not admit is
# never fetched, and a field the field set admits and this set does not is a field with no
# source: the two sets' intersection is the whole answer, so there is no second place to look.
_SOURCE_FIELDS = frozenset({"funding_rate", "open_interest", "term_structure", "roll_cost"})

# The markets that trade every calendar day, spelled the way the repository's scheduler spells
# them (``market_schedule._is_continuous_market``). A market outside this set keeps the
# business-day calendar every caller counted before this module existed.
_DAILY_MARKETS = frozenset({"Crypto", "Cryptocurrency"})


def derivative_fields(market: str) -> tuple[str, ...]:
    """The fields of ``fields_for(market)`` this module can fill, in field-set order.

    The intersection is read this way round on purpose: a market whose field set does not admit
    a field is not asked for it, so the field set stays the only authority on what a run may
    name, and a field with no source cannot be admitted into a panel of nothing.
    """
    return tuple(field for field in fields_for(market) if field in _SOURCE_FIELDS)


def sessions(market: str, start: datetime, end: datetime) -> pd.DatetimeIndex:
    """The market's own sessions between ``start`` and ``end``, inclusive."""
    if market in _DAILY_MARKETS:
        return pd.date_range(start, end, freq="D")
    return pd.bdate_range(start, end)


def enrich_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    market: str,
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    """``frames`` with one column per derivative field this market's field set admits.

    Each frame is keyed by its member's ``key`` (``Crypto:BTC/USDT@binance:swap``), and that key
    is what the sources are handed, verbatim: it is the only place a venue and a product are
    spelled, and the suffix names a venue *or* a product alone (``@swap``) -- so parsing it here
    would read the venue-less shape as an exchange that does not exist. The sources parse it.

    The window is the panel's own fetched range, not the caller's requested range, so a panel cut
    to the search window cannot pick up a value published in the frozen tail -- neither in a
    column nor in the fingerprint that covers one.

    A symbol whose series comes back empty keeps its frame, with the field present as a column of
    NaN: this module fills what it can and never drops a symbol, which is the same rule the
    engine's ``_field_panels`` states for a column a frame never had.
    """
    wanted = derivative_fields(market)
    if not wanted:
        return dict(frames)
    enriched: dict[str, pd.DataFrame] = {}
    for key, frame in frames.items():
        panel = frame.copy()
        for field in wanted:
            panel[field] = _values(_series(market, str(key), field, start=start, end=end), frame.index)
        enriched[key] = panel
    return enriched


def _series(market: str, key: str, field: str, *, start: datetime, end: datetime) -> pd.Series:
    """One field's series for one member key, read from this module's own name for it.

    The name is read per call, never tabulated at import time: a table would hold the function
    objects themselves, and a caller standing in for a source by patching the name it is spelled
    under would go on reaching the network instead -- which is a test that hangs on a timeout
    rather than a test that fails.

    One branch per field, and an unknown field is refused rather than routed somewhere. A
    catch-all branch would answer a field added to :data:`_SOURCE_FIELDS` later with the wrong
    venue's numbers -- a plausible wrong value instead of a missing one, which is the one
    failure mode no coverage gate can see.
    """
    if field == "funding_rate":
        return fetch_funding_history(market, key, start=start, end=end)
    if field == "open_interest":
        return fetch_open_interest_history(market, key, start=start, end=end)
    if field == "term_structure":
        return fetch_term_structure(market, key, start=start, end=end)
    if field == "roll_cost":
        return fetch_roll_cost(market, key, start=start, end=end)
    raise KeyError(field)


def _values(series: pd.Series, index: pd.Index) -> np.ndarray:
    """One series read at each bar of ``index``: the last value published at or before it.

    Both sides are normalized to a tz-naive UTC index, the convention every panel here uses: a
    frame and a venue series that disagreed about the timezone would otherwise line up on
    nothing at all, which is a column of NaN no gate could tell from a symbol with no history.
    Values are coerced to numbers, and a repeated publication instant keeps the later row, which
    is the rule the sources apply to their own pages.
    """
    dates = _utc_index(index)
    values = pd.to_numeric(series, errors="coerce")
    values.index = _utc_index(values.index)
    values = values.sort_index()
    values = values[~values.index.duplicated(keep="last")]
    return values.reindex(dates, method="ffill").to_numpy(dtype="float64")


def _utc_index(index: Any) -> pd.DatetimeIndex:
    """A tz-naive UTC ``DatetimeIndex``, whatever timezone (or none) the caller's index carries."""
    return pd.DatetimeIndex(pd.to_datetime(index, utc=True)).tz_localize(None)
