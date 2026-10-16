"""The candidate pool: what the search already mined, as comparison factors.

A pool is the caller's own ``(user_id, market, timeframe)`` library of **frozen** candidates --
the expressions that came through the out-of-sample review. Anything wider would compare factors
that are not comparable: a factor value is a cross-sectional signal, so a candidate mined on
another market or another timeframe is not something a new candidate may be measured against, and
another tenant's library is not readable at all. A candidate still in ``search`` is not a member
either: it has not been reviewed, and letting the in-flight state of someone else's job decide a
comparison set would make every verdict downstream drift with it.

Members are not stored. A member's factor panel is rebuilt from its ``expression_json`` on every
call, against the field panels the caller's own evaluation runs on -- the same boundary v1 drew
when it kept only a fingerprint of the searched panel and no replayable copy of it, and the reason
a rebuilt member is comparable at all, since the engine scores candidates on exactly those panels.
The cost is paid per evaluation: every member's expression is evaluated again, each time. Nothing
here caches, because a cached panel would outlive the data it was built from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.factor_mining.storage import FactorMiningRepository

# The status a candidate ends in only after the frozen review passed it.
FROZEN = "frozen"

# Members are re-evaluated on every call, so the cap is the cost bound: the pool adds one panel
# evaluation of its own per member to every candidate the search scores.
DEFAULT_POOL_LIMIT = 20


@dataclass(frozen=True)
class PoolMember:
    """One pool row: its id, its fingerprint, and the expression as stored.

    ``expression`` is unvalidated and may be ``None`` -- a row written by an older operator set, or
    one whose JSON no longer parses, is still a row here, and :func:`pool_panels` is where it turns
    out to be unusable.
    """

    candidate_id: int
    fingerprint: str
    expression: Mapping[str, Any] | None


def load_pool(
    user_id: int,
    market: str,
    timeframe: str,
    *,
    exclude_hash: str = "",
    limit: int = DEFAULT_POOL_LIMIT,
) -> list[PoolMember]:
    """The caller's frozen candidates for one market and timeframe, newest first, capped.

    ``exclude_hash`` drops one member by expression fingerprint -- the candidate under evaluation,
    which the caller knows by hash rather than by row id, and which would otherwise correlate with
    itself. The exclusion runs before the cap, so a candidate that already sits in the library
    does not cost the evaluation a comparison member.

    The store hands the library back newest first, and that order is the cap's: what was frozen
    most recently is what a new candidate is measured against. Re-sorting by row id here is what
    keeps membership independent of the order the driver returned its rows in; every member is
    ``frozen``, so the ``(status, id)`` key is the id alone.

    An absent market or timeframe is refused rather than passed on: the store drops an empty
    filter, so a missing one would read as "every market" and quietly turn the pool into a
    comparison set spanning markets and timeframes whose factor values are not comparable.
    """
    if not (market and timeframe):
        raise ValueError("factorMining.invalidParameter")
    cap = max(0, int(limit))
    rows = FactorMiningRepository().list_candidates(
        user_id=int(user_id),
        status=FROZEN,
        market=str(market),
        timeframe=str(timeframe),
        # One row past the cap, so excluding the candidate under evaluation still fills the pool.
        limit=cap + 1,
    )
    members = [
        PoolMember(
            candidate_id=int(row["id"]),
            fingerprint=str(row.get("expressionHash") or ""),
            expression=row.get("expression"),
        )
        for row in rows
    ]
    if exclude_hash:
        members = [member for member in members if member.fingerprint != str(exclude_hash)]
    members.sort(key=lambda member: member.candidate_id, reverse=True)
    return members[:cap]


def pool_panels(
    entries: Sequence[PoolMember],
    field_panels: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """Fingerprint -> rebuilt factor panel, for every member that could be evaluated.

    ``field_panels`` is the caller's own field mapping -- the one the engine scores candidates
    with -- so a member is aligned to the same dates, symbols, and field semantics as the
    candidate it is compared against. A member whose stored expression no longer evaluates is left
    out rather than raised: one unreadable old row must not end the caller's evaluation.
    """
    evaluator = ExpressionEvaluator(field_panels)
    panels: dict[str, pd.DataFrame] = {}
    for member in entries:
        try:
            panels[member.fingerprint] = evaluator.evaluate(member.expression)
        except Exception:
            # Dropped, exactly as the search drops a candidate it cannot score.
            continue
    return panels
