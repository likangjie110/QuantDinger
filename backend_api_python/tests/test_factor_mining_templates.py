"""Factor mining templates: catalog metadata and budgeted deterministic expansion."""

import pytest

from app.services.factor_mining.dsl import normalize_expression
from app.services.factor_mining.templates import MAX_BUDGET, TEMPLATES, expand_candidates, template_catalog


def test_catalog_exposes_hypothesis_keys_and_grids():
    catalog = {item["templateId"]: item for item in template_catalog()}
    assert set(catalog) >= {"momentum", "reversal", "low_volatility", "liquidity",
                            "volume_price_confirm", "risk_adjusted_momentum", "conditional_filter"}
    assert catalog["momentum"]["hypothesisI18nKey"] == "factorMining.template.momentum"


def test_every_expanded_candidate_passes_dsl_validation():
    for candidate in expand_candidates(budget=MAX_BUDGET):
        normalized = normalize_expression(candidate.expression)
        assert normalized.fingerprint == candidate.fingerprint
        assert normalized.lookback <= 504


def test_expansion_is_deterministic_for_same_seed():
    first = [item.fingerprint for item in expand_candidates(budget=120, seed=7)]
    second = [item.fingerprint for item in expand_candidates(budget=120, seed=7)]
    assert first == second


def test_budget_limits_candidate_count_and_dedupes():
    candidates = expand_candidates(budget=25)
    assert len(candidates) <= 25
    assert len({item.fingerprint for item in candidates}) == len(candidates)


def test_expansion_can_be_restricted_to_one_template():
    candidates = expand_candidates(template_ids=["reversal"], budget=MAX_BUDGET)
    assert {item.template_id for item in candidates} == {"reversal"}


def test_budget_above_hard_limit_is_rejected():
    with pytest.raises(ValueError, match="factorMining.budgetExceeded"):
        expand_candidates(budget=MAX_BUDGET + 1)


def test_over_budget_truncation_is_order_preserving_and_seed_stable():
    full = [item.fingerprint for item in expand_candidates(budget=MAX_BUDGET)]
    truncated = [item.fingerprint for item in expand_candidates(budget=25, seed=11)]
    assert truncated == [item.fingerprint for item in expand_candidates(budget=25, seed=11)]
    assert len(truncated) == 25
    # Reservoir truncation drops rows but never reorders the ones it keeps.
    positions = [full.index(fingerprint) for fingerprint in truncated]
    assert positions == sorted(positions)


def test_smoothing_adds_three_variants_per_grid_point():
    plain = expand_candidates(template_ids=["momentum"], budget=MAX_BUDGET, smoothing=False)
    smoothed = expand_candidates(template_ids=["momentum"], budget=MAX_BUDGET)
    assert len(plain) == len(TEMPLATES["momentum"].parameter_grid["N"]) == 5
    assert len(smoothed) == 3 * len(plain)


def test_conditional_filter_keeps_a_panel_truth_branch_and_constant_fallback():
    node = TEMPLATES["conditional_filter"].build({"N": 20, "M": 5})
    condition, truth_branch, fallback = node["args"]
    assert node["op"] == "where"
    assert condition["op"] == "gt"
    # A scalar truth branch would reach DataFrame.where as the receiver and crash.
    assert truth_branch["op"] == "ts_pct_change"
    assert fallback == {"op": "const", "args": [], "params": {"value": 0}}
