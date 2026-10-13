"""Factor mining expression DSL: validation, canonical form, and fingerprints."""

import pytest

from app.services.factor_mining.dsl import ExpressionError, normalize_expression


def test_normalize_produces_stable_fingerprint_regardless_of_key_order():
    first = {"op": "ts_mean", "params": {"n": 20}, "args": ["close"]}
    second = {"args": ["close"], "op": "ts_mean", "params": {"n": 20}}
    assert normalize_expression(first).fingerprint == normalize_expression(second).fingerprint


def test_normalize_rejects_unknown_field():
    with pytest.raises(ExpressionError, match="unknownField"):
        normalize_expression({"op": "ts_mean", "args": ["order_book_depth"], "params": {"n": 5}})


def test_normalize_rejects_unknown_operator():
    with pytest.raises(ExpressionError, match="unknownOperator"):
        normalize_expression({"op": "future_return", "args": ["close"], "params": {"n": 5}})


@pytest.mark.parametrize("node,reason", [
    ({"op": "lag", "args": ["close"], "params": {"n": 0}}, "badWindow"),
    ({"op": "lag", "args": ["close"], "params": {"n": -5}}, "badWindow"),
    ({"op": "ts_mean", "args": ["close"], "params": {"n": 300}}, "badWindow"),
    ({"op": "ts_mean", "args": ["close"], "params": {}}, "badWindow"),
    ({"op": "add", "args": ["close"], "params": {}}, "badArity"),
])
def test_normalize_rejects_bad_windows_and_arity(node, reason):
    with pytest.raises(ExpressionError, match=reason):
        normalize_expression(node)


def test_normalize_rejects_depth_and_node_budget():
    deep = "close"
    for _ in range(8):
        deep = {"op": "neg", "args": [deep], "params": {}}
    with pytest.raises(ExpressionError, match="tooDeep"):
        normalize_expression(deep)


def test_normalize_rejects_node_count_over_budget():
    def build(levels):
        if levels == 0:
            return "close"
        return {"op": "add", "args": [build(levels - 1), build(levels - 1)], "params": {}}

    # 31 nodes within the depth budget, so only the node budget can reject it.
    with pytest.raises(ExpressionError, match="tooManyNodes"):
        normalize_expression(build(4))


@pytest.mark.parametrize("node", [42, None, {"op": "neg", "args": [3.5], "params": {}}])
def test_normalize_rejects_non_node_values(node):
    with pytest.raises(ExpressionError, match="badNode"):
        normalize_expression(node)


def test_normalize_rejects_lookback_over_budget():
    node = {"op": "ts_mean", "args": [
        {"op": "ts_mean", "args": [
            {"op": "ts_mean", "args": ["close"], "params": {"n": 252}}], "params": {"n": 252}}],
        "params": {"n": 252}}
    with pytest.raises(ExpressionError, match="lookbackExceeded"):
        normalize_expression(node)


def test_window_one_is_accepted_for_single_bar_return():
    result = normalize_expression({"op": "ts_pct_change", "args": ["close"], "params": {"n": 1}})
    assert result.windows == (1,)
    assert result.lookback == 1
    with pytest.raises(ExpressionError, match="badWindow"):
        normalize_expression({"op": "lag", "args": ["close"], "params": {"n": 0}})


def test_const_carries_a_finite_numeric_literal():
    result = normalize_expression({"op": "const", "args": [], "params": {"value": 0}})
    assert result.fields == ()
    assert result.windows == ()
    assert result.lookback == 0
    assert result.node_count == 1
    assert result.depth == 1
    for value in (-0.5, 1e3):
        normalize_expression({"op": "const", "args": [], "params": {"value": value}})
    # A zero-arity operator needs no args key; both spellings canonicalize alike.
    assert normalize_expression({"op": "const", "params": {"value": 0}}).fingerprint == result.fingerprint


@pytest.mark.parametrize("params", [
    {"value": True},
    {"value": "0"},
    {},
    {"value": float("nan")},
    {"value": float("inf")},
], ids=["bool", "string", "missing", "nan", "inf"])
def test_const_rejects_non_numeric_or_non_finite_value(params):
    with pytest.raises(ExpressionError, match="badNode"):
        normalize_expression({"op": "const", "args": [], "params": params})


def test_bare_numeric_leaf_is_still_rejected():
    with pytest.raises(ExpressionError, match="badNode"):
        normalize_expression({"op": "where", "args": [
            {"op": "gt", "args": ["close", "open"], "params": {}}, "close", 0], "params": {}})


def test_fingerprint_ignores_empty_params_spelling():
    without_params = {"op": "add", "args": [{"op": "neg", "args": ["close"]}, "volume"]}
    with_empty_params = {"op": "add", "args": [
        {"op": "neg", "args": ["close"], "params": {}}, "volume"], "params": {}}
    assert normalize_expression(without_params).canonical == normalize_expression(with_empty_params).canonical
    assert normalize_expression(without_params).fingerprint == normalize_expression(with_empty_params).fingerprint


def test_normalize_reports_fields_windows_and_lookback():
    result = normalize_expression({"op": "add", "args": [
        {"op": "ts_mean", "args": ["close"], "params": {"n": 20}},
        {"op": "ts_mean", "args": ["volume"], "params": {"n": 60}}], "params": {}})
    assert result.fields == ("close", "volume")
    assert result.windows == (20, 60)
    assert result.lookback == 60
    assert result.node_count == 5
