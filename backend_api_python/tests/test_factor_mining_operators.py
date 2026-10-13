"""Factor mining panel operators: semantics, missing-data rules, and point-in-time safety."""

import numpy as np
import pandas as pd
import pytest

from app.services.factor_mining.dsl import OPERATOR_SPECS
from app.services.factor_mining.operators import OPERATORS, apply_operator

_WINDOWED_OPERATORS = [name for name, spec in OPERATOR_SPECS.items() if spec[1]]


def _panel(values):
    return pd.DataFrame(values, index=pd.date_range("2025-01-01", periods=len(values), freq="B"), columns=["A", "B"])


def test_operator_registry_matches_dsl_specs():
    assert set(OPERATORS) == set(OPERATOR_SPECS)


def test_ts_mean_uses_full_window_and_emits_nan_until_satisfied():
    panel = _panel([[1.0, 2.0], [2.0, 4.0], [3.0, 6.0]])
    result = apply_operator("ts_mean", [panel], {"n": 2})
    assert np.isnan(result.iloc[0]).all()
    assert result.iloc[1].tolist() == [1.5, 3.0]


def test_lag_shifts_forward_only():
    panel = _panel([[1.0, 2.0], [2.0, 4.0]])
    result = apply_operator("lag", [panel], {"n": 1})
    assert np.isnan(result.iloc[0]).all()
    assert result.iloc[1].tolist() == [1.0, 2.0]


def test_safe_div_returns_nan_on_zero_and_nan_denominator():
    numerator = _panel([[1.0, 1.0], [1.0, 1.0]])
    denominator = _panel([[0.0, 2.0], [np.nan, 4.0]])
    result = apply_operator("safe_div", [numerator, denominator], {})
    assert np.isnan(result.iloc[0, 0]) and np.isnan(result.iloc[1, 0])
    # Row 1 mixes a NaN denominator with a valid one: the guard is per element,
    # not per row or per column.
    assert result.iloc[0, 1] == 0.5
    assert result.iloc[1, 1] == 0.25


def test_cs_rank_is_cross_sectional_per_row():
    panel = _panel([[1.0, 3.0], [5.0, 1.0]])
    result = apply_operator("cs_rank", [panel], {})
    assert result.iloc[0].tolist() == [0.5, 1.0]
    assert result.iloc[1].tolist() == [1.0, 0.5]


def test_operators_do_not_leak_future_bars():
    base = _panel([[1.0, 2.0], [2.0, 3.0], [3.0, 5.0], [4.0, 8.0]])
    mutated = base.copy()
    mutated.iloc[-1] = [100.0, 200.0]
    for name, args, params in [
        ("ts_mean", [base], {"n": 2}),
        ("ts_std", [base], {"n": 2}),
        ("ts_rank", [base], {"n": 3}),
        ("ts_pct_change", [base], {"n": 1}),
    ]:
        before = apply_operator(name, args, params).iloc[:-1]
        after = apply_operator(name, [mutated if arg is base else arg for arg in args], params).iloc[:-1]
        pd.testing.assert_frame_equal(before, after)


@pytest.mark.parametrize("name", _WINDOWED_OPERATORS)
def test_every_windowed_operator_is_prefix_invariant(name):
    """Rewriting the newest bar must not move any earlier output."""
    assert _WINDOWED_OPERATORS, "windowed operators must exist for this boundary test to mean anything"
    base = _panel([[1.0, 2.0], [2.0, 3.0], [3.0, 5.0], [4.0, 8.0]])
    mutated = base.copy()
    mutated.iloc[-1] = [100.0, 200.0]
    arity = OPERATOR_SPECS[name][0]
    before = apply_operator(name, [base] * arity, {"n": 2}).iloc[:-1]
    after = apply_operator(name, [mutated] * arity, {"n": 2}).iloc[:-1]
    pd.testing.assert_frame_equal(before, after, obj=name)


def test_windowed_operators_follow_the_spec_semantics():
    panel = _panel([[1.0, 2.0], [2.0, 4.0], [3.0, 6.0], [4.0, 8.0]])
    for name, window, expected in [
        ("delta", 2, [np.nan, np.nan, 2.0, 2.0]),
        ("ts_sum", 3, [np.nan, np.nan, 6.0, 9.0]),
        ("ts_min", 3, [np.nan, np.nan, 1.0, 2.0]),
        ("ts_max", 3, [np.nan, np.nan, 3.0, 4.0]),
        ("ts_std", 3, [np.nan, np.nan, 1.0, 1.0]),
        ("ts_zscore", 3, [np.nan, np.nan, 1.0, 1.0]),
        ("ts_pct_change", 2, [np.nan, np.nan, 2.0, 1.0]),
    ]:
        actual = apply_operator(name, [panel], {"n": window})["A"].to_numpy()
        np.testing.assert_allclose(actual, expected, err_msg=name)


def test_ts_rank_and_ts_corr_read_the_trailing_window_only():
    panel = _panel([[3.0, 1.0], [1.0, 2.0], [2.0, 3.0]])
    other = _panel([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
    ranked = apply_operator("ts_rank", [panel], {"n": 3})
    assert ranked.iloc[:2].isna().all().all()
    np.testing.assert_allclose(ranked.iloc[2].to_numpy(), [2.0 / 3.0, 1.0])
    correlated = apply_operator("ts_corr", [panel, other], {"n": 3})
    assert correlated.iloc[:2].isna().all().all()
    np.testing.assert_allclose(correlated.iloc[2].to_numpy(), [-0.5, 1.0])


def test_degenerate_windows_yield_nan_rather_than_inf():
    panel = _panel([[1.0, 2.0], [2.0, 4.0], [3.0, 6.0]])
    flat = _panel([[5.0, 5.0], [5.0, 5.0], [5.0, 5.0]])
    # ddof=1 over a single bar has no dispersion to report.
    assert apply_operator("ts_std", [panel], {"n": 1}).isna().all().all()
    assert apply_operator("ts_zscore", [panel], {"n": 1}).isna().all().all()
    # A flat window has zero dispersion: NaN, not inf.
    assert apply_operator("ts_zscore", [flat], {"n": 3}).isna().all().all()


def test_cross_sectional_operators_follow_the_spec_semantics():
    panel = _panel([[1.0, 3.0], [5.0, 1.0]])
    np.testing.assert_allclose(apply_operator("cs_demean", [panel], {}).to_numpy(), [[-1.0, 1.0], [2.0, -2.0]])
    np.testing.assert_allclose(apply_operator("cs_zscore", [panel], {}).to_numpy(), [[-1.0, 1.0], [1.0, -1.0]])
    # A degenerate cross-section is neutral, matching factors/research.py winsorize_zscore.
    np.testing.assert_allclose(apply_operator("cs_zscore", [_panel([[2.0, 2.0]])], {}).to_numpy(), [[0.0, 0.0]])
    # ... but a symbol that had no value stays missing.
    sparse = apply_operator("cs_zscore", [_panel([[1.0, np.nan]])], {})
    assert sparse.iloc[0, 0] == 0.0 and np.isnan(sparse.iloc[0, 1])


def test_const_is_a_scalar_that_broadcasts_through_panels():
    panel = _panel([[1.0, 2.0]])
    zero = apply_operator("const", [], {"value": 0})
    assert zero == 0.0
    assert apply_operator("add", [panel, zero], {}).iloc[0].tolist() == [1.0, 2.0]
    assert apply_operator("safe_div", [panel, zero], {}).isna().all().all()
    # Template 7: where(cond, expr, const(0)) -- the fallback branch is a bare number.
    conditioned = apply_operator("where", [apply_operator("gt", [panel, 1.0], {}), panel, zero], {})
    assert conditioned.iloc[0].tolist() == [0.0, 2.0]


def test_unary_operators_guard_their_domains():
    panel = _panel([[-4.0, 0.0], [4.0, 9.0]])
    assert apply_operator("neg", [panel], {}).iloc[0].tolist() == [4.0, -0.0]
    assert apply_operator("abs", [panel], {}).iloc[0].tolist() == [4.0, 0.0]
    assert apply_operator("sign", [panel], {}).iloc[0].tolist() == [-1.0, 0.0]
    clipped = apply_operator("clip", [panel], {"lo": -1.0, "hi": 1.0})
    assert clipped.iloc[0].tolist() == [-1.0, 0.0]
    assert clipped.iloc[1].tolist() == [1.0, 1.0]
    logged = apply_operator("log", [panel], {})
    assert np.isnan(logged.iloc[0, 0]) and np.isnan(logged.iloc[0, 1])
    assert logged.iloc[1, 1] == pytest.approx(np.log(9.0))
    rooted = apply_operator("sqrt", [panel], {})
    assert np.isnan(rooted.iloc[0, 0])
    assert rooted.iloc[1, 1] == 3.0


def test_binary_operators_are_elementwise_and_comparisons_return_bools():
    left = _panel([[1.0, 2.0]])
    right = _panel([[4.0, 1.0]])
    assert apply_operator("add", [left, right], {}).iloc[0].tolist() == [5.0, 3.0]
    assert apply_operator("sub", [left, right], {}).iloc[0].tolist() == [-3.0, 1.0]
    assert apply_operator("mul", [left, right], {}).iloc[0].tolist() == [4.0, 2.0]
    assert apply_operator("div", [left, right], {}).iloc[0].tolist() == [0.25, 2.0]
    for name, expected in [("gt", [False, True]), ("lt", [True, False]), ("ge", [False, True]), ("le", [True, False])]:
        result = apply_operator(name, [left, right], {})
        assert result.iloc[0].tolist() == expected, name
        assert result.dtypes.tolist() == [bool, bool], name
