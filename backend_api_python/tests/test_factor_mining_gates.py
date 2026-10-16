"""Factor mining screening gates: ladder order, boundaries, and selection-bias control."""

from dataclasses import FrozenInstanceError

import pandas as pd
import pytest

from app.services.factor_mining.gates import (GateThresholds, GateVerdict, net_return_series,
                                              screen_metrics, screen_out_of_sample,
                                              screen_redundancy, screen_stability)
from app.services.strategy_evolution.statistics import deflated_sharpe, sample_sharpe


def _metrics(**overrides):
    base = {"coverage": 0.9, "rankIc": 0.05, "icir": 1.2, "averageTurnover": 0.3,
            "icSeries": [{"time": f"2025-01-{day:02d}", "value": 0.05} for day in range(1, 31)],
            "stability": {"firstHalfIc": 0.04, "secondHalfIc": 0.06}}
    base.update(overrides)
    return base


def _ic_series(count):
    return [{"time": f"2025-01-{day:02d}", "value": 0.05} for day in range(1, count + 1)]


def test_screen_metrics_rejects_low_coverage():
    verdict = screen_metrics(_metrics(coverage=0.4), GateThresholds())
    assert verdict.passed is False and verdict.reason == "factorMining.reject.lowCoverage"


def test_screen_metrics_rejects_too_few_observations():
    verdict = screen_metrics(_metrics(icSeries=[{"time": "2025-01-01", "value": 0.05}]), GateThresholds())
    assert verdict.reason == "factorMining.reject.insufficientObservations"


def test_screen_metrics_rejects_weak_ic():
    assert screen_metrics(_metrics(rankIc=0.005), GateThresholds()).reason == "factorMining.reject.weakIc"


def test_screen_metrics_passes_strong_candidate():
    assert screen_metrics(_metrics(), GateThresholds()).passed is True


def test_screen_stability_rejects_high_turnover():
    verdict = screen_stability(_metrics(averageTurnover=0.95), GateThresholds())
    assert verdict.reason == "factorMining.reject.highTurnover"


def test_screen_stability_rejects_inconsistent_halves():
    verdict = screen_stability(_metrics(stability={"firstHalfIc": 0.06, "secondHalfIc": -0.04}), GateThresholds())
    assert verdict.reason == "factorMining.reject.unstableHalves"


def test_screen_redundancy_rejects_builtin_duplicate():
    verdict = screen_redundancy({"momentum_20": 0.92, "reversal_5": 0.1}, GateThresholds())
    assert verdict.reason == "factorMining.reject.duplicateBuiltin"


def test_screen_out_of_sample_rejects_sign_flip():
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=-0.04),
                                   returns=[0.01, -0.002, 0.004, 0.006], trials=10, thresholds=GateThresholds())
    assert verdict.reason == "factorMining.reject.weakOutOfSample"


def test_every_gate_reports_its_own_stage():
    assert screen_metrics(_metrics(), GateThresholds()).stage == "L2"
    assert screen_stability(_metrics(), GateThresholds()) == GateVerdict(passed=True, stage="L3", reason="")
    assert screen_redundancy({}, GateThresholds()).stage == "L4"
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                   returns=[0.007, -0.003] * 20, trials=1, thresholds=GateThresholds())
    assert verdict == GateVerdict(passed=True, stage="L5", reason="")


def test_screen_metrics_thresholds_are_inclusive_minimums():
    assert screen_metrics(_metrics(coverage=0.60), GateThresholds()).passed is True
    assert screen_metrics(_metrics(coverage=0.5999), GateThresholds()).reason == "factorMining.reject.lowCoverage"
    assert screen_metrics(_metrics(icSeries=_ic_series(30)), GateThresholds()).passed is True
    assert screen_metrics(_metrics(icSeries=_ic_series(29)), GateThresholds()).reason == (
        "factorMining.reject.insufficientObservations")
    assert screen_metrics(_metrics(rankIc=-0.02), GateThresholds()).passed is True
    assert screen_metrics(_metrics(rankIc=0.0199), GateThresholds()).reason == "factorMining.reject.weakIc"


def test_screen_metrics_reads_missing_metrics_as_unmet():
    # No evidence buys no pass on a strength threshold: an absent metric is read as 0.0.
    assert screen_metrics({}, GateThresholds()).reason == "factorMining.reject.lowCoverage"
    assert screen_metrics({"coverage": 0.9, "rankIc": 0.05}, GateThresholds()).reason == (
        "factorMining.reject.insufficientObservations")


def test_gates_fail_closed_on_unmeasurable_and_infinite_metrics():
    # NaN compares False against every bar, so left alone it would walk a poisoned candidate
    # through the whole ladder. An infinity is a real reading, and it fails the gate it faces.
    assert screen_metrics(_metrics(coverage=float("nan")), GateThresholds()).reason == (
        "factorMining.reject.lowCoverage")
    assert screen_metrics(_metrics(rankIc=float("nan")), GateThresholds()).reason == "factorMining.reject.weakIc"
    assert screen_stability(_metrics(icir=float("nan")), GateThresholds()).reason == "factorMining.reject.weakIcir"
    assert screen_stability(_metrics(averageTurnover=float("inf")), GateThresholds()).reason == (
        "factorMining.reject.highTurnover")
    # An unmeasurable turnover is not a low one -- L3 and L4 fail closed together. An absent
    # turnover still reads as no excess, which is the preserved "missing reads as 0.0" policy.
    assert screen_stability(_metrics(averageTurnover=float("nan")), GateThresholds()).reason == (
        "factorMining.reject.highTurnover")
    assert screen_stability(_metrics(averageTurnover=None), GateThresholds()).passed is True


def test_screen_stability_thresholds_are_inclusive():
    assert screen_stability(_metrics(icir=0.30), GateThresholds()).passed is True
    assert screen_stability(_metrics(icir=0.2999), GateThresholds()).reason == "factorMining.reject.weakIcir"
    assert screen_stability(_metrics(averageTurnover=0.70), GateThresholds()).passed is True
    assert screen_stability(_metrics(averageTurnover=0.7000001), GateThresholds()).reason == (
        "factorMining.reject.highTurnover")


def test_screen_stability_does_not_absolutize_icir():
    # The spec pins "ICIR >= 0.30" with no abs(), unlike L2's abs(rankIC): a mirrored factor is
    # generated as its own candidate, so a negative ICIR earns no credit here.
    assert screen_stability(_metrics(icir=-1.2), GateThresholds()).reason == "factorMining.reject.weakIcir"


def test_screen_stability_rejects_inconsistent_folds():
    assert screen_stability(_metrics(foldIcSigns=[1, 1, 1, -1]), GateThresholds()).passed is True
    assert screen_stability(_metrics(foldIcSigns=[1, 1, 1, -1, -1]), GateThresholds()).passed is True
    assert screen_stability(_metrics(foldIcSigns=[1, 1, -1, -1]), GateThresholds()).reason == (
        "factorMining.reject.unstableFolds")


def test_screen_stability_skips_checks_it_cannot_evaluate():
    # foldIcSigns is written by the search engine; an absent or single-fold series means "not
    # evaluated", never "failed". An absent stability block reads the same way.
    assert screen_stability(_metrics(), GateThresholds()).passed is True
    assert screen_stability(_metrics(foldIcSigns=[-1]), GateThresholds()).passed is True
    assert screen_stability(_metrics(stability={}), GateThresholds()).passed is True


def test_screen_redundancy_threshold_is_inclusive_and_two_sided():
    assert screen_redundancy({"momentum_20": 0.85}, GateThresholds()).reason == "factorMining.reject.duplicateBuiltin"
    assert screen_redundancy({"momentum_20": 0.8499}, GateThresholds()).passed is True
    assert screen_redundancy({"momentum_20": -0.92}, GateThresholds()).reason == "factorMining.reject.duplicateBuiltin"
    assert screen_redundancy({"momentum_20": 0.3, "reversal_5": -0.84}, GateThresholds()).passed is True
    assert screen_redundancy({}, GateThresholds()).reason == "factorMining.reject.unverifiableRedundancy"


def test_screen_redundancy_ignores_pairs_that_were_never_measured():
    # The fundamental factors carry no data on a price-volume run, so those pairs come back NaN
    # and are skipped -- but the pairs that were computed still decide the gate.
    assert screen_redundancy({"momentum_20": 0.3, "value": float("nan")}, GateThresholds()).passed is True
    assert screen_redundancy({"momentum_20": 0.92, "value": float("nan")}, GateThresholds()).reason == (
        "factorMining.reject.duplicateBuiltin")
    assert screen_redundancy({"value": float("nan"), "quality": None}, GateThresholds()).reason == (
        "factorMining.reject.unverifiableRedundancy")
    # An infinite correlation is not a finite measurement either, so it is no evidence.
    assert screen_redundancy({"momentum_20": float("inf")}, GateThresholds()).reason == (
        "factorMining.reject.unverifiableRedundancy")


def test_numeric_dedup_rejects_an_affine_rescaled_twin():
    """A pooled candidate's affine twin carries no new information, and L4 says so by name.

    ``2x + 3`` moves every value and preserves every rank, so the reading the engine takes -- the
    cross-sectional rank correlation of the two -- comes back at 1.0. The twin is a pool member,
    so it is the pool's own bar that rejects the candidate and the pool's own key that records
    why: a candidate duplicating what the pool already holds is not a candidate duplicating a
    built-in, and a ledger that says the second is unreadable.
    """
    values = pd.Series([0.4, -1.2, 0.05, 2.3, -0.7, 1.1])
    twin = values * 2.0 + 3.0
    # The reading ``FactorResearchEngine._factor_correlation`` takes: rank both sides, then
    # correlate the ranks. scipy's spearman spells the same number and is not a dependency here.
    correlation = values.rank(method="average").corr(twin.rank(method="average"))

    assert correlation == pytest.approx(1.0, abs=1e-9)
    verdict = screen_redundancy({"pool:aaaaaaaa": correlation}, GateThresholds())
    assert verdict.stage == "L4"
    assert verdict.reason == "factorMining.reject.duplicateInPool"


def test_numeric_dedup_keeps_a_distinct_factor_with_low_correlation():
    """A candidate unrelated to its pool and to the built-ins passes L4 -- and was measured.

    The second and third mappings are one notch above a bar the first mapping is below, with
    nothing else near its own: the rejection they draw is what keeps the first mapping's pass from
    being the pass of a gate that never ran. The last one pins the namespace as a prefix rather
    than a substring, so a built-in whose id merely contains the word is still a built-in.
    """
    assert screen_redundancy({"momentum_20": 0.3, "pool:aaaaaaaa": -0.3}, GateThresholds()).passed is True
    assert screen_redundancy({"pool:aaaaaaaa": 0.86}, GateThresholds()).reason == (
        "factorMining.reject.duplicateInPool")
    assert screen_redundancy({"poolside_momentum": 0.9}, GateThresholds(max_builtin_correlation=0.9)).reason == (
        "factorMining.reject.duplicateBuiltin")


def test_the_two_correlation_thresholds_are_independently_overridable():
    """One bar per section, each read from the run's own payload, neither moved by the other.

    Both readings sit between the two bars the overrides leave behind, so which key comes back
    says which bar moved: lowering the built-in bar must not reach the pool's section and the
    other way round. The defaults are equal, because both sections answer the same question --
    does this candidate carry anything the comparison set does not already hold.
    """
    reading = {"momentum_20": 0.5, "pool:aaaaaaaa": 0.5}
    assert screen_redundancy(reading, GateThresholds()).passed is True
    assert screen_redundancy(reading, GateThresholds(max_builtin_correlation=0.4)).reason == (
        "factorMining.reject.duplicateBuiltin")
    assert screen_redundancy(reading, GateThresholds(max_pool_correlation=0.4)).reason == (
        "factorMining.reject.duplicateInPool")

    assert GateThresholds().max_pool_correlation == GateThresholds().max_builtin_correlation == 0.85
    pooled = GateThresholds.from_payload({"max_pool_correlation": 0.5})
    assert (pooled.max_pool_correlation, pooled.max_builtin_correlation) == (0.5, 0.85)
    builtin = GateThresholds.from_payload({"max_builtin_correlation": 0.5})
    assert (builtin.max_pool_correlation, builtin.max_builtin_correlation) == (0.85, 0.5)


def test_pool_redundancy_stays_fail_closed_on_all_nan():
    """Nothing finite anywhere is still rejected as unverifiable, whichever section it came from.

    A pool member the engine could not measure is not a member the candidate cleared -- the rule
    the built-in section has always had, unchanged by the split. It is one rule for the whole
    comparison, not one per section: a NaN in either section while the other was measured is a
    comparison that ran, and its readings decide. Requiring each section to carry evidence of its
    own would fail closed on every run whose pool was empty or unmeasurable, which is a rejection
    the pool never asked for.
    """
    assert screen_redundancy({"pool:aaaaaaaa": float("nan")}, GateThresholds()).reason == (
        "factorMining.reject.unverifiableRedundancy")
    assert screen_redundancy({"pool:aaaaaaaa": float("inf"), "pool:bbbbbbbb": None}, GateThresholds()).reason == (
        "factorMining.reject.unverifiableRedundancy")
    assert screen_redundancy({"momentum_20": float("nan"), "pool:aaaaaaaa": 0.1}, GateThresholds()).passed is True
    assert screen_redundancy({"pool:aaaaaaaa": float("nan"), "momentum_20": 0.1}, GateThresholds()).passed is True


def test_screen_out_of_sample_rejects_thin_out_of_sample_ic():
    # Same sign as the search window, but below the frozen-window strength bar.
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.008),
                                   returns=[0.007, -0.003] * 20, trials=1, thresholds=GateThresholds())
    assert verdict.reason == "factorMining.reject.weakOutOfSample"


def test_screen_out_of_sample_hinges_on_the_exact_strength_bar():
    # An |out-of-sample rank IC| equal to the bar is met, not missed; a notch below is rejected.
    returns = [0.007, -0.003] * 20
    met = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.01),
                               returns=returns, trials=1, thresholds=GateThresholds())
    missed = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.009999),
                                  returns=returns, trials=1, thresholds=GateThresholds())
    assert met.passed is True
    assert missed.reason == "factorMining.reject.weakOutOfSample"


def test_screen_out_of_sample_compares_the_probability_inclusively():
    # The selection-bias bar is met at equality. It is read from the review itself so the check
    # stays pinned on the comparison rather than on the formula, and one notch above it rejects.
    returns = [0.007, -0.003] * 20
    observed = sample_sharpe(returns)
    for trials in (1, 250):
        probability = deflated_sharpe(returns, observed_sharpe=observed, trials=trials)["probability"]
        met = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                   returns=returns, trials=trials,
                                   thresholds=GateThresholds(min_deflated_sharpe_probability=probability))
        missed = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                      returns=returns, trials=trials,
                                      thresholds=GateThresholds(min_deflated_sharpe_probability=probability + 1e-6))
        assert met.passed is True
        assert missed.reason == "factorMining.reject.selectionBias"


def test_screen_out_of_sample_tightens_as_the_trial_ledger_grows():
    # One return series, two trial counts: the deflated-Sharpe benchmark rises with the number of
    # attempts, so a candidate that clears the bar on a single trial fails once 250 were tried.
    returns = [0.007, -0.003] * 20
    single = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                  returns=returns, trials=1, thresholds=GateThresholds())
    crowded = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                   returns=returns, trials=250, thresholds=GateThresholds())
    assert single.passed is True
    assert crowded.reason == "factorMining.reject.selectionBias"


def test_screen_out_of_sample_rejects_an_unavailable_deflated_sharpe():
    # A flat curve has no variation, so no deflated Sharpe can be computed; with no evidence of
    # skill after selection bias the candidate is rejected rather than passed by default -- even
    # when the probability bar itself is disabled.
    flat = [0.01, 0.01, 0.01]
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                   returns=flat, trials=250, thresholds=GateThresholds())
    assert verdict.reason == "factorMining.reject.selectionBias"
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.05), oos_metrics=_metrics(rankIc=0.04),
                                   returns=flat, trials=250,
                                   thresholds=GateThresholds(min_deflated_sharpe_probability=0.0))
    assert verdict.reason == "factorMining.reject.selectionBias"


def test_screen_out_of_sample_needs_an_in_sample_direction_to_confirm():
    # An in-sample IC of exactly zero carries no direction, so the frozen window has nothing to
    # confirm -- a zero is not sign agreement.
    verdict = screen_out_of_sample(is_metrics=_metrics(rankIc=0.0), oos_metrics=_metrics(rankIc=0.04),
                                   returns=[0.007, -0.003] * 20, trials=1, thresholds=GateThresholds())
    assert verdict.reason == "factorMining.reject.weakOutOfSample"


def test_net_return_series_skips_undefined_steps():
    # The steps it does take are plain point-to-point returns.
    assert net_return_series([{"net": 1.0}, {"net": 1.09}]) == pytest.approx([0.09])
    assert net_return_series([]) == []
    assert net_return_series([{"time": "2025-01-01", "net": 1.0}]) == []
    assert net_return_series([{"time": "2025-01-01", "net": 0.0}, {"time": "2025-01-02", "net": 1.0}]) == []


def test_gate_thresholds_from_payload_overrides_only_supplied_keys():
    defaults = GateThresholds()
    assert GateThresholds.from_payload(None) == defaults
    assert GateThresholds.from_payload({}) == defaults
    assert GateThresholds.from_payload({"min_coverage": 0.8, "min_ic_observations": 12}) == (
        GateThresholds(min_coverage=0.8, min_ic_observations=12))
    # A null or unrecognised key keeps its default instead of raising on a partial payload.
    assert GateThresholds.from_payload({"min_coverage": None, "minRankIc": 0.5}) == defaults
    # A value spelled in the field's own type, in either numeric spelling, is accepted as given.
    assert GateThresholds.from_payload({"min_ic_observations": 30.0, "min_icir": "0.4"}) == (
        GateThresholds(min_ic_observations=30, min_icir=0.4))


@pytest.mark.parametrize("payload", [[1], "min_coverage", 5], ids=["list", "string", "number"])
def test_gate_thresholds_refuse_a_payload_that_is_not_a_mapping(payload):
    """``payload.get`` on a list is an ``AttributeError``: a 500 with no key for the caller."""
    with pytest.raises(ValueError, match="factorMining.thresholdsInvalid"):
        GateThresholds.from_payload(payload)


@pytest.mark.parametrize("payload", [
    {"min_ic_observations": 0.5},       # int(0.5) is 0: the observation floor switches itself off
    {"min_ic_observations": True},      # int(True) is 1: the same floor, near enough
    {"min_coverage": True},             # float(True) is 1.0: perfect coverage demanded
    {"min_coverage": float("nan")},     # every comparison against NaN is False, so the bar passes
    {"min_coverage": float("inf")},
    {"min_icir": "strong"},
], ids=["fractional-count", "bool-count", "bool-rate", "nan", "inf", "unreadable"])
def test_gate_thresholds_refuse_values_that_would_be_a_different_bar(payload):
    """A coercion that silently changes the value is a bar the run never asked for."""
    with pytest.raises(ValueError, match="factorMining.thresholdsInvalid"):
        GateThresholds.from_payload(payload)


def test_screen_metrics_reads_the_observation_count_a_trimmed_trial_keeps():
    """A ledger row keeps the length of its IC series, not the series, and L2 still counts it."""
    assert screen_metrics(
        {"coverage": 0.9, "rankIc": 0.05, "icObservations": 30}, GateThresholds()).passed is True
    assert screen_metrics(
        {"coverage": 0.9, "rankIc": 0.05, "icObservations": 29}, GateThresholds()).reason == (
        "factorMining.reject.insufficientObservations")
    # A row with neither spelling has no observations to count, which is not a pass.
    assert screen_metrics({"coverage": 0.9, "rankIc": 0.05}, GateThresholds()).reason == (
        "factorMining.reject.insufficientObservations")


def test_gate_verdicts_and_thresholds_are_frozen():
    # One GateThresholds instance is shared as MiningConfig's default, so no run may rebind a
    # field on it and leak the change into the next run.
    with pytest.raises(FrozenInstanceError):
        GateThresholds().min_coverage = 0.1
    with pytest.raises(FrozenInstanceError):
        screen_metrics(_metrics(), GateThresholds()).reason = "factorMining.reject.weakIc"


@pytest.mark.parametrize("value", [10 ** 400, float("inf")], ids=["huge-int", "infinity"])
def test_a_threshold_too_large_to_convert_is_refused_by_key(value):
    """``float(10**400)`` overflows and ``inf`` fails the finite check, both by the same key.

    Without ``OverflowError`` in the handler the first escapes as an ``OverflowError``, which the
    route answers with a 500 rather than the key that names the malformed override.
    """
    with pytest.raises(ValueError, match="factorMining.thresholdsInvalid"):
        GateThresholds.from_payload({"min_coverage": value})
