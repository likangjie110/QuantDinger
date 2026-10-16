"""Incremental value: what a candidate still predicts once the library's own factors are removed.

The L4 redundancy gate asks the pool one question at a time -- is this candidate a duplicate of a
member -- and a candidate can clear every one of those bars while being a linear combination of
three of them put together. This module asks the other question the spec's synergy reading
(section 13) turns on: with the *whole* pool taken out, is anything of the candidate left that
predicts?

The pool is orthogonalized away one cross-section at a time. On each session the candidate's
values are regressed against the members' values, the fitted part is subtracted, and what remains
is correlated with the forward returns of that session, Spearman style -- both sides ranked
(method ``average``, ``pct``) before a Pearson correlation, the arithmetic the research engine
scores ``rankIc`` with in ``strategy_v2/factor_research.py``. The reading is the mean of those
per-session ICs, and a candidate has incremental value when

    ``abs(residualIc) >= min_abs_rank_ic`` **and** the residual IC carries the raw IC's sign.

The sign rule is not decoration. A residual that predicts in the *opposite* direction to the
candidate means the candidate's signal reversed once the pool's part was removed -- the shape a
noise variable takes, not the shape information takes -- and it can clear the magnitude bar on
the wrong side of zero. Both sides of that comparison are computed **here**, from these panels,
over the very sessions the residual was measured on: a raw IC handed in by a caller could have
been measured on a different session set, and a sign compared across two session sets is not a
comparison at all.

The regression runs on ranks rather than raw values. The judgment is a rank judgment -- a
candidate of *any* monotone rescaling of a member is that member -- and ranks are what makes it
say so: a member carrying a price level, an ``exp`` of itself, or a percentile of itself is
explained identically. Raw values would leave the residue of a monotone transform on the table
(and, with the panel's own outliers in it, would let a handful of symbols set the fitted
direction for the session). Ranking first also keeps the two halves of the reading in one
currency: the criterion, the residual, and the returns are all read as cross-sectional orders.

Nothing here knows about the frozen tail. The increment is a search-period judgment and the
frozen window is not evidence for it, so the caller hands over the in-sample and validation rows
and this module -- which reads only the panels it is given -- has no way to reach the rest.

Three outcomes, and the caller has to tell them apart. ``measured`` and ``has_incremental_value``
both true is "adds information"; measured and false is "carries nothing the pool does not", the
only state a caller may retire a candidate on. ``measured`` false is "no answer": an empty pool
(the question was never asked) or too few usable cross-sections to average (nothing to answer
with). Both are reported as such rather than as a rejection, because a candidate is not weaker
for having had nothing to be compared against -- and both are reported rather than silently
passed, because a reader who cannot tell "measured and clear" from "never measured" reads the
second as the first.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

# The L2 bar, restated rather than imported: this module is pure computation and importing the
# thresholds module would drag the gate ladder (and, behind it, the statistics package) in for one
# number. Restating is why the equality needs a test -- see ``test_the_incremental_bar_is_the_l2_bar``.
DEFAULT_MIN_ABS_RANK_IC = 0.02

# The smallest cross-section a regression and a rank correlation can be read off. Three is the
# floor the research engine uses for its own IC (``factor_research``), and a fit needs observations
# to spare: every member subtracts a degree of freedom, and a session fitted to its own count of
# symbols leaves a residual that is zero by construction rather than by measurement.
MIN_CROSS_SECTION_SIZE = 3

# One session is not a series to average, and a verdict resting on it would be a verdict about
# that session. A caller wanting a stronger count has L2's ``min_ic_observations``, which every
# candidate reaching this stage has already cleared.
MIN_CROSS_SECTIONS = 2

# Residual spreads below this fraction of the candidate's own rank spread are numerically flat:
# the pool reproduces the candidate, and a flat residual carries no cross-sectional information.
_NUMERICAL_ZERO = 1e-9

REJECT_NO_INCREMENTAL_VALUE = "factorMining.reject.noIncrementalValue"
UNMEASURED_REASON = "factorMining.poolGainUnmeasured"


@dataclass(frozen=True)
class PoolGain:
    """One verdict on what the candidate adds to the pool, with the readings behind it.

    Read ``measured`` first. ``has_incremental_value`` is ``False`` whenever nothing was measured,
    so a caller that skips the flag retires candidates on a question that was never answered;
    ``reason`` is a closed i18n key -- empty when the candidate carries incremental value, a
    ``factorMining.reject.*`` key when it does not, and :data:`UNMEASURED_REASON` -- deliberately
    *not* a rejection -- when there is no verdict at all.

    ``residual_ic`` and ``raw_ic`` are the means over the sessions counted in
    ``used_cross_sections``, and are ``None`` when nothing was measured: a partial mean is not a
    reading, and a caller shown one would use it. The two counts are how a caller tells a pool
    that was measured from one that could not be -- a pool whose members do not rebuild leaves
    every session unmeasurable, and the count says so.
    """

    measured: bool
    has_incremental_value: bool
    residual_ic: float | None
    raw_ic: float | None
    used_cross_sections: int
    skipped_cross_sections: int
    reason: str


def residual_ic(
    candidate_panel: pd.DataFrame,
    pool_factor_panels: Mapping[str, pd.DataFrame],
    returns: pd.DataFrame,
    *,
    min_abs_rank_ic: float = DEFAULT_MIN_ABS_RANK_IC,
) -> PoolGain:
    """Judge what ``candidate_panel`` adds to the pool, one session at a time.

    ``pool_factor_panels`` is :func:`~app.services.factor_mining.pool.pool_panels`' rebuilt
    members (its keys -- the fingerprints -- are not read here; the pool is a span, and which
    member contributed which part of it is not what this answers). ``returns`` is the forward
    returns of the same window, aligned to the same symbols. All three are panels of the search
    window: dates on the index, symbols on the columns.

    Sessions where the pool cannot be measured -- a member with no values on that session, a
    session too small for the fit, a flat candidate or a flat return column -- are skipped and
    counted rather than allowed to stand for the whole judgment. An all-NaN member skips every
    session and comes back unmeasured: the pool could not be removed, so the candidate was not
    measured, and a candidate is never retired for a measurement that did not happen.
    """
    members = list(pool_factor_panels.values()) if pool_factor_panels else []
    if not members:
        # No pool, no question. Reporting a reading of zero here would read as "no value" and
        # report section counts for a comparison that was never drawn.
        return _unmeasured(0, 0)

    readings: list[tuple[float, float]] = []
    skipped = 0
    for timestamp in candidate_panel.index:
        section = _section(candidate_panel, members, returns, timestamp)
        if section is None or len(section) < max(MIN_CROSS_SECTION_SIZE, len(members) + 2):
            skipped += 1
            continue
        ranks = section.rank(method="average", pct=True)
        candidate_rank = ranks.iloc[:, 0]
        return_rank = ranks.iloc[:, 1]
        if candidate_rank.nunique() < 2 or return_rank.nunique() < 2:
            # A flat candidate or a flat return column holds no cross-sectional order to read, and
            # a correlation against one is undefined rather than zero.
            skipped += 1
            continue
        raw = float(candidate_rank.corr(return_rank, method="pearson"))
        member_ranks = ranks.iloc[:, 2:]
        design = member_ranks.sub(member_ranks.mean(), axis=1).to_numpy(dtype=float)
        target = (candidate_rank - candidate_rank.mean()).to_numpy(dtype=float)
        coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
        residual = target - design @ coefficients
        spread = float(candidate_rank.max() - candidate_rank.min())
        if residual.max() - residual.min() <= _NUMERICAL_ZERO * spread:
            # The pool reproduces the candidate: the residual is flat, and a flat residual carries
            # no order -- zero by construction here, not a correlation that failed to be measured.
            readings.append((raw, 0.0))
            continue
        residual_rank = pd.Series(residual, index=ranks.index).rank(method="average", pct=True)
        incremental = float(residual_rank.corr(return_rank, method="pearson"))
        if not (math.isfinite(raw) and math.isfinite(incremental)):
            skipped += 1
            continue
        readings.append((raw, incremental))

    if len(readings) < MIN_CROSS_SECTIONS:
        return _unmeasured(len(readings), skipped)
    raw_ic = float(pd.Series([raw for raw, _ in readings]).mean())
    incremental_ic = float(pd.Series([value for _, value in readings]).mean())
    has_incremental_value = abs(incremental_ic) >= min_abs_rank_ic and incremental_ic * raw_ic > 0
    return PoolGain(
        measured=True,
        has_incremental_value=has_incremental_value,
        residual_ic=incremental_ic,
        raw_ic=raw_ic,
        used_cross_sections=len(readings),
        skipped_cross_sections=skipped,
        reason="" if has_incremental_value else REJECT_NO_INCREMENTAL_VALUE,
    )


def _section(
    candidate_panel: pd.DataFrame,
    members: list[pd.DataFrame],
    returns: pd.DataFrame,
    timestamp: pd.Timestamp,
) -> pd.DataFrame | None:
    """One session as a frame: candidate, returns, then one column per pool member.

    Symbols are aligned across the panels and a symbol missing any of the values is dropped: the
    fit needs one row per symbol over every member at once, not a matrix with holes in it. A
    session the returns or any member does not cover at all, and a value that is infinite (a
    division by nothing, which no rank can order against the rest), are missing on the same terms.
    """
    rows = []
    for panel in (candidate_panel, returns, *members):
        if timestamp not in panel.index:
            return None
        rows.append(panel.loc[timestamp].astype(float))
    frame = pd.concat(rows, axis=1)
    return frame.where(np.isfinite(frame)).dropna()


def _unmeasured(used: int, skipped: int) -> PoolGain:
    """No answer: neither verdict, no readings, and a reason that is not a rejection."""
    return PoolGain(
        measured=False,
        has_incremental_value=False,
        residual_ic=None,
        raw_ic=None,
        used_cross_sections=used,
        skipped_cross_sections=skipped,
        reason=UNMEASURED_REASON,
    )
