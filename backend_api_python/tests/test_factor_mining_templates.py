"""Factor mining templates: catalog metadata and budgeted deterministic expansion."""

import pytest

from app.services.factor_mining.dsl import MAX_DEPTH, ExpressionError, fields_for, normalize_expression
from app.services.factor_mining.templates import (
    MAX_BUDGET,
    TEMPLATES,
    FactorTemplate,
    expand_candidates,
    template_catalog,
)


def test_catalog_exposes_hypothesis_keys_and_grids():
    catalog = {item["templateId"]: item for item in template_catalog()}
    assert set(catalog) >= {"momentum", "reversal", "low_volatility", "liquidity",
                            "volume_price_confirm", "risk_adjusted_momentum", "conditional_filter"}
    assert catalog["momentum"]["hypothesisI18nKey"] == "factorMining.template.momentum"
    # One hypothesis per template: no non-window selector axis may creep back into a grid.
    assert set(catalog["liquidity"]["parameterGrid"]) == {"N", "M"}
    assert set(catalog["liquidity_level"]["parameterGrid"]) == {"N"}


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


def test_expansion_reads_a_candidate_against_the_given_field_set(monkeypatch):
    """The expansion fingerprints every grid point, so its field set has to reach the DSL.

    No shipped template names a derivatives field yet; this one stands in for the template that
    will. A set the caller widened admits it, and the default -- the price-volume alias -- still
    refuses it, which is what keeps the set a policy the caller states rather than one the
    expansion assumes.
    """
    monkeypatch.setitem(TEMPLATES, "funding_trend", FactorTemplate(
        template_id="funding_trend", hypothesis_i18n_key="factorMining.template.momentum",
        category="cross_market", parameter_grid={"N": (20,)},
        build=lambda params: {"op": "ts_mean", "args": ["funding_rate"],
                              "params": {"n": params["N"]}}))
    with pytest.raises(ExpressionError, match="unknownField"):
        expand_candidates(template_ids=["funding_trend"], smoothing=False)
    seeds = expand_candidates(template_ids=["funding_trend"], smoothing=False,
                              allowed_fields=fields_for("Crypto"))
    assert [seed.expression["args"] for seed in seeds] == [["funding_rate"]]
    assert seeds[0].fingerprint == normalize_expression(
        seeds[0].expression, allowed_fields=fields_for("Crypto")).fingerprint


def test_budget_above_hard_limit_is_rejected():
    with pytest.raises(ValueError, match="factorMining.budgetExceeded"):
        expand_candidates(budget=MAX_BUDGET + 1)


def test_unknown_template_id_is_rejected_with_a_stable_code():
    with pytest.raises(ValueError, match="factorMining.templateNotFound"):
        expand_candidates(template_ids=["nope"], budget=10)


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


def test_liquidity_splits_into_change_and_level_templates():
    change = TEMPLATES["liquidity"]
    level = TEMPLATES["liquidity_level"]
    assert change.parameter_grid == {"N": (5, 10, 20), "M": (20, 60)}
    assert level.parameter_grid == {"N": (5, 10, 20)}
    assert level.hypothesis_i18n_key == "factorMining.template.liquidity_level"
    assert change.build({"N": 5, "M": 20}) == {"op": "safe_div", "args": [
        {"op": "ts_mean", "args": ["volume"], "params": {"n": 5}},
        {"op": "ts_mean", "args": ["volume"], "params": {"n": 20}},
    ], "params": {}}
    assert level.build({"N": 5}) == {"op": "cs_rank", "args": [
        {"op": "ts_mean", "args": ["volume"], "params": {"n": 5}},
    ], "params": {}}


def test_conditional_filter_keeps_a_panel_truth_branch_and_constant_fallback():
    node = TEMPLATES["conditional_filter"].build({"N": 20, "M": 5})
    condition, truth_branch, fallback = node["args"]
    assert node["op"] == "where"
    assert condition["op"] == "gt"
    # A scalar truth branch would reach DataFrame.where as the receiver and crash.
    assert truth_branch["op"] == "ts_pct_change"
    assert fallback == {"op": "const", "args": [], "params": {"value": 0}}


def test_smoothed_template_seven_leaves_room_for_crossover():
    """The deepest variant the library expands still sits two levels below the depth bound.

    That room is what evolution needs: a crossover grafts a fresh depth-2 subtree onto a path,
    and a candidate already at the bound would have every such graft refused. The bound is what
    moved, not the template -- evening out the skeleton here would change ``expand_candidates``
    output, and the enumeration is a contract the plan keeps byte for byte.
    """
    deepest = max(
        normalize_expression(seed.expression).depth
        for seed in expand_candidates(template_ids=["conditional_filter"], budget=MAX_BUDGET)
    )
    assert MAX_DEPTH - deepest >= 2
