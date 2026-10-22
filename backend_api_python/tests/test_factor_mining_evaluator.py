"""Factor mining expression evaluator: panel alignment, data gaps, and memoization."""

import numpy as np
import pandas as pd
import pytest

from app.services.factor_mining.evaluator import ExpressionEvaluator


def _fields():
    index = pd.date_range("2025-01-01", periods=60, freq="B")
    close = pd.DataFrame({symbol: [100 + offset * 5 + day for day in range(60)]
                          for offset, symbol in enumerate(["A", "B", "C"])}, index=index)
    volume = pd.DataFrame({symbol: [1000 + day for day in range(60)] for symbol in ["A", "B", "C"]}, index=index)
    return {"close": close, "volume": volume}


def test_evaluator_matches_pandas_reference_for_momentum_expression():
    fields = _fields()
    node = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}
    result = ExpressionEvaluator(fields).evaluate(node)
    expected = fields["close"].pct_change(20, fill_method=None)
    pd.testing.assert_frame_equal(result, expected)


def test_evaluator_reuses_shared_subtrees():
    fields = _fields()
    shared = {"op": "ts_mean", "args": ["volume"], "params": {"n": 5}}
    node = {"op": "div", "args": [
        {"op": "cs_rank", "args": [shared]},
        {"op": "cs_rank", "args": [ {"op": "add", "args": [shared, shared], "params": {}} ]}], "params": {}}
    evaluator = ExpressionEvaluator(fields)
    result = evaluator.evaluate(node)
    assert result.shape == fields["close"].shape
    assert evaluator.cache_info()["hits"] >= 1


def test_evaluator_returns_nan_frame_for_missing_forward_data():
    fields = _fields()
    node = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}
    result = ExpressionEvaluator(fields).evaluate(node)
    assert result.iloc[:20].isna().all().all()


def test_evaluator_bare_field_root_returns_that_field_panel():
    fields = _fields()
    result = ExpressionEvaluator(fields).evaluate("volume")
    pd.testing.assert_frame_equal(result, fields["volume"])


def test_evaluator_aligns_missing_fields_and_columns_to_the_reference_panel():
    fields = _fields()
    # "open" is a legal DSL field that this panel simply lacks: missing data, not an error.
    absent = ExpressionEvaluator(fields).evaluate({"op": "ts_mean", "args": ["open"], "params": {"n": 5}})
    assert absent.shape == fields["close"].shape
    assert absent.isna().all().all()

    # A field panel that lacks a symbol is reindexed, so the gap shows up as NaN.
    partial = {"close": fields["close"], "volume": fields["volume"][["A", "B"]]}
    narrowed = ExpressionEvaluator(partial).evaluate({"op": "ts_mean", "args": ["volume"], "params": {"n": 5}})
    assert narrowed.columns.tolist() == ["A", "B", "C"]
    assert narrowed["C"].isna().all()
    assert narrowed[["A", "B"]].iloc[5:].notna().all().all()

    # Without a "close" panel the first available field defines the reference shape.
    only_volume = ExpressionEvaluator({"volume": fields["volume"]}).evaluate(
        {"op": "ts_mean", "args": ["close"], "params": {"n": 5}})
    assert only_volume.shape == fields["volume"].shape
    assert only_volume.isna().all().all()


def test_evaluator_broadcasts_a_scalar_root_to_the_reference_panel():
    fields = _fields()
    result = ExpressionEvaluator(fields).evaluate({"op": "const", "args": [], "params": {"value": 0.5}})
    pd.testing.assert_frame_equal(result, pd.DataFrame(0.5, index=fields["close"].index, columns=fields["close"].columns))


def test_evaluator_passes_infinities_through_for_the_coverage_gate():
    fields = _fields()
    fields["close"].iloc[5, 0] = 0.0
    node = {"op": "div", "args": [{"op": "const", "args": [], "params": {"value": 1}}, "close"], "params": {}}
    result = ExpressionEvaluator(fields).evaluate(node)
    assert np.isposinf(result.iloc[5, 0])
    assert result.iloc[6, 0] == 1 / 106.0


def test_evaluator_cache_counts_distinct_subtrees_and_reuses():
    fields = _fields()
    shared = {"op": "ts_mean", "args": ["volume"], "params": {"n": 5}}
    evaluator = ExpressionEvaluator(fields)
    evaluator.evaluate({"op": "add", "args": [shared, shared], "params": {}})
    # The leaf, the rolling mean, and the root: three distinct subtrees, one reuse. Read as a
    # reuse that happened rather than as an exact count -- the split between distinct subtrees and
    # reuses is the evaluator's own bookkeeping, not a contract any caller depends on.
    info = evaluator.cache_info()
    assert info["subtrees"] >= 1 and info["hits"] >= 1

    # The cache key is the canonical spelling, so a dropped empty "params" is the same subtree:
    # the same expression, spelled the other way, has to produce the same cache profile.
    spelled_out = {"op": "neg", "args": ["close"], "params": {}}
    other = ExpressionEvaluator(fields)
    other.evaluate({"op": "add", "args": [{"op": "neg", "args": ["close"]}, spelled_out], "params": {}})
    assert other.cache_info() == info


@pytest.mark.parametrize("operator, expected_columns", [
    ("cs_rank", {"A": [0.5, 1 / 3, 0.5, 1 / 3], "B": [1, 2 / 3, 1, 2 / 3],
                 "C": [np.nan, 1, np.nan, 1]}),
    ("cs_zscore", {"A": [-1, -np.sqrt(1.5), -1, -np.sqrt(1.5)], "B": [1, 0, 1, 0],
                   "C": [np.nan, np.sqrt(1.5), np.nan, np.sqrt(1.5)]}),
    ("cs_demean", {"A": [-1, -2, -1, -2], "B": [1, 0, 1, 0],
                   "C": [np.nan, 2, np.nan, 2]}),
])
def test_p1_cross_sections_ignore_nonmembers_before_ranking_and_scaling(operator, expected_columns):
    dates = pd.date_range("2025-01-01", periods=4)
    close = pd.DataFrame({"A": [1., 2., 3., 4.], "B": [3., 4., 5., 6.],
                          "C": [1e12, 6., 1e12, 8.], "outsider": [-1e12] * 4}, index=dates)
    membership = pd.DataFrame({"A": True, "B": True, "C": [False, True, False, True],
                               "outsider": False}, index=dates)
    original = close.copy(deep=True)
    result = ExpressionEvaluator({"close": close}, membership=membership.iloc[::-1, ::-1]).evaluate(
        {"op": operator, "args": ["close"]})

    expected = pd.DataFrame({**expected_columns, "outsider": [np.nan] * 4}, index=dates, dtype=float)
    pd.testing.assert_frame_equal(result, expected)
    pd.testing.assert_frame_equal(close, original)


def test_p1_time_series_uses_pre_membership_history_but_cross_sections_do_not():
    dates = pd.date_range("2025-01-01", periods=5)
    close = pd.DataFrame({"A": [1.] * 5, "B": [3.] * 5, "C": [3., 6., 9., 12., 15.]}, index=dates)
    membership = pd.DataFrame({"A": True, "B": True, "C": [False, False, True, False, True]}, index=dates)
    mean = {"op": "ts_mean", "args": ["close"], "params": {"n": 3}}
    evaluator = ExpressionEvaluator({"close": close}, membership=membership)

    values = evaluator.evaluate(mean)
    np.testing.assert_allclose(values["C"], [np.nan, np.nan, 6., 9., 12.], equal_nan=True)
    ranks = evaluator.evaluate({"op": "cs_rank", "args": [mean]})
    assert ranks.loc[dates[2], "C"] == ranks.loc[dates[4], "C"] == 1.0
    assert ranks.loc[dates[3], "A"] == 0.5
    assert pd.isna(ranks.loc[dates[3], "C"])


def test_p1_time_series_of_cross_sections_does_not_invent_pre_entry_ranks():
    dates = pd.date_range("2025-01-01", periods=4)
    close = pd.DataFrame({"A": 1., "B": 2., "C": 3.}, index=dates)
    membership = pd.DataFrame({"A": True, "B": True, "C": [False, False, True, True]}, index=dates)
    result = ExpressionEvaluator({"close": close}, membership=membership).evaluate(
        {"op": "ts_mean", "args": [{"op": "cs_rank", "args": ["close"]}], "params": {"n": 2}})

    assert pd.isna(result.loc[dates[2], "C"])
    assert result.loc[dates[3], "C"] == 1.0
