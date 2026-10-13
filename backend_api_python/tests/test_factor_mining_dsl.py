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


def test_normalize_reports_fields_windows_and_lookback():
    result = normalize_expression({"op": "add", "args": [
        {"op": "ts_mean", "args": ["close"], "params": {"n": 20}},
        {"op": "ts_mean", "args": ["volume"], "params": {"n": 60}}], "params": {}})
    assert result.fields == ("close", "volume")
    assert result.windows == (20, 60)
    assert result.lookback == 60
    assert result.node_count == 5
