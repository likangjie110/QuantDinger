"""Graded screening gates: one metrics mapping in, one verdict out.

Four pure functions walk the L2-L5 ladder of the factor mining spec -- data quality and signal
strength (L2), stability (L3), redundancy against the built-in factor pool (L4), and the frozen
window out-of-sample review (L5). Each returns the first check a candidate fails, paired with a
closed reason key, so every rejection stays explainable; the caller owns the ordering and the
trial ledger.

A missing, NaN, or unreadable metric is read as 0.0 -- no evidence fails a minimum and passes a
maximum -- while an infinite reading is kept, so it fails exactly the gates where more of that
quantity is worse. Redundancy is the one place where no evidence cannot stand: a correlation the
engine never computed is neither a duplicate nor a clearance, so an L4 call with nothing finite to
compare is rejected as unverifiable. The other exception is fold consistency -- ``foldIcSigns`` is
written by the search engine, so an absent series means "not evaluated" and that check is skipped.

No database, clock, or network access here. ``deflated_sharpe`` is reused from
:mod:`app.services.strategy_evolution.statistics` as the spec requires; importing it also executes
that package's ``__init__`` and pulls in Flask, which is accepted because a second copy of the
Euler-Mascheroni benchmark formula would be a correctness risk.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

from app.services.strategy_evolution.statistics import deflated_sharpe, sample_sharpe


@dataclass(frozen=True)
class GateThresholds:
    """The L2-L5 bars. A run may override any of them from its payload."""

    min_coverage: float = 0.60
    min_ic_observations: int = 30
    min_abs_rank_ic: float = 0.02
    min_icir: float = 0.30
    min_fold_consistency: float = 0.60
    max_turnover: float = 0.70
    max_builtin_correlation: float = 0.85
    min_oos_abs_rank_ic: float = 0.01
    min_deflated_sharpe_probability: float = 0.95

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "GateThresholds":
        """Overlay a run payload on the defaults; null and unrecognised keys keep the default."""
        if not payload:
            return cls()
        defaults = {field.name: field.default for field in fields(cls)}
        return cls(**{
            name: type(default)(payload[name])
            for name, default in defaults.items()
            if payload.get(name) is not None
        })


@dataclass(frozen=True)
class GateVerdict:
    """One gate's decision. ``reason`` is empty when passed, else a closed reject key."""

    passed: bool
    stage: str
    reason: str


def _number(value: Any) -> float:
    """Read a metric as a float to compare against a bar.

    A missing or unreadable value becomes 0.0, which fails a minimum and passes a maximum. NaN
    joins it there on purpose: NaN compares False against every bar, so left alone it would walk
    a poisoned candidate through the whole ladder. Infinities are kept -- they compare correctly,
    failing exactly the gates where more of the quantity is worse.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def _finite(value: Any) -> float | None:
    """Read a correlation as evidence: None unless it is a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _passed(stage: str) -> GateVerdict:
    return GateVerdict(passed=True, stage=stage, reason="")


def _failed(stage: str, reason: str) -> GateVerdict:
    return GateVerdict(passed=False, stage=stage, reason=reason)


def screen_metrics(metrics: Mapping[str, Any], thresholds: GateThresholds) -> GateVerdict:
    """L2: coverage, IC observation count, then signal strength -- first failure wins."""
    if _number(metrics.get("coverage")) < thresholds.min_coverage:
        return _failed("L2", "factorMining.reject.lowCoverage")
    if len(metrics.get("icSeries") or ()) < thresholds.min_ic_observations:
        return _failed("L2", "factorMining.reject.insufficientObservations")
    if abs(_number(metrics.get("rankIc"))) < thresholds.min_abs_rank_ic:
        return _failed("L2", "factorMining.reject.weakIc")
    return _passed("L2")


def screen_stability(metrics: Mapping[str, Any], thresholds: GateThresholds) -> GateVerdict:
    """L3: ICIR, fold sign agreement, first/second half agreement, then turnover.

    Fold agreement is the larger side's share of the fold signs: three of four folds agreeing is
    0.75, a two-two split is 0.5. Fewer than two folds is not a measurement, so it is skipped.
    """
    if _number(metrics.get("icir")) < thresholds.min_icir:
        return _failed("L3", "factorMining.reject.weakIcir")
    signs = [_number(sign) for sign in metrics.get("foldIcSigns") or ()]
    if len(signs) >= 2:
        agreeing = max(sum(sign > 0 for sign in signs), sum(sign < 0 for sign in signs))
        if agreeing / len(signs) < thresholds.min_fold_consistency:
            return _failed("L3", "factorMining.reject.unstableFolds")
    stability = metrics.get("stability") or {}
    if _number(stability.get("firstHalfIc")) * _number(stability.get("secondHalfIc")) < 0:
        return _failed("L3", "factorMining.reject.unstableHalves")
    if _number(metrics.get("averageTurnover")) > thresholds.max_turnover:
        return _failed("L3", "factorMining.reject.highTurnover")
    return _passed("L3")


def screen_redundancy(correlations: Mapping[str, float], thresholds: GateThresholds) -> GateVerdict:
    """L4: reject when any built-in factor correlates at or above the bar, in either direction.

    Only finite correlations count as evidence. A NaN pair is one the engine could not compare --
    the fundamental factors carry no data on a price-volume run -- and skipping it is not the same
    as clearing the candidate. So when nothing finite was measured the gate cannot run at all, and
    an unrun de-duplication check rejects rather than waves a candidate past the ladder's only
    redundancy checkpoint.
    """
    measured = [abs(value) for value in (_finite(value) for value in correlations.values())
                if value is not None]
    if not measured:
        return _failed("L4", "factorMining.reject.unverifiableRedundancy")
    if max(measured) >= thresholds.max_builtin_correlation:
        return _failed("L4", "factorMining.reject.duplicateBuiltin")
    return _passed("L4")


def net_return_series(long_short_curve: Sequence[Mapping[str, Any]]) -> list[float]:
    """Point-to-point net returns of a long/short curve.

    Values stay at full precision: this series feeds :func:`deflated_sharpe` directly. A step
    taken from a non-positive NAV is dropped rather than divided through -- its return is
    undefined -- which is also what
    :func:`app.services.strategy_evolution.statistics.equity_returns` does.
    """
    values = [_number(point.get("net")) for point in long_short_curve]
    return [current / previous - 1.0
            for previous, current in zip(values, values[1:]) if previous > 0]


def screen_out_of_sample(*, is_metrics: Mapping[str, Any], oos_metrics: Mapping[str, Any],
                         returns: Sequence[float], trials: int,
                         thresholds: GateThresholds) -> GateVerdict:
    """L5: sign and strength on the frozen window, then the selection-bias haircut.

    ``trials`` is every candidate the run evaluated, rejected ones included: that count is the
    whole point of the deflated-Sharpe benchmark, so a caller must pass the run total rather than
    the number of survivors.
    """
    in_sample = _number(is_metrics.get("rankIc"))
    out_of_sample = _number(oos_metrics.get("rankIc"))
    if in_sample * out_of_sample <= 0 or abs(out_of_sample) < thresholds.min_oos_abs_rank_ic:
        return _failed("L5", "factorMining.reject.weakOutOfSample")
    sample = list(returns)
    review = deflated_sharpe(sample, observed_sharpe=sample_sharpe(sample), trials=trials)
    if not review.get("available") or _number(review.get("probability")) < thresholds.min_deflated_sharpe_probability:
        return _failed("L5", "factorMining.reject.selectionBias")
    return _passed("L5")
