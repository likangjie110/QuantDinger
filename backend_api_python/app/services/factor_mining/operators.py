"""Vectorized panel operators for the factor expression DSL.

A panel is a ``DataFrame``: rows are bars in ascending ``DatetimeIndex`` order, columns are
symbols. Operator names, arity, and window bounds are already enforced by
:mod:`app.services.factor_mining.dsl` -- this module only computes.

Every operator holds two invariants:

* Point-in-time: windows end at the current bar, so rewriting a later bar can never change
  an earlier output.
* Missing stays missing: NaN inputs propagate, and undefined results (division by zero,
  zero dispersion, ``log`` of a non-positive value) are NaN rather than ``inf``.

``const`` yields a plain number instead of a panel, so operands are ``Any``: arithmetic,
comparison, ``where``, and ``safe_div`` accept scalars and let pandas broadcast them.
Windowed and cross-sectional operators take panels. This is a pure function layer -- no
clocks, database, or network access.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from app.services.factor_mining.dsl import WINDOW_PARAM


def _is_panel(value: Any) -> bool:
    """``const`` yields a plain number, so any operand may be a panel or a scalar."""
    return isinstance(value, pd.DataFrame)


def _masked(value: Any, valid: Any) -> Any:
    """NaN outside ``valid`` -- the shared domain guard for ``log``/``sqrt``/``safe_div``."""
    return value.where(valid) if _is_panel(value) else (value if valid else np.nan)


def _rolling(value: pd.DataFrame, window: int) -> Any:
    """Trailing window that only reports once ``window`` bars are present."""
    return value.rolling(window, min_periods=window)


def _lag(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0].shift(params[WINDOW_PARAM])


def _delta(args: list[Any], params: Mapping[str, Any]) -> Any:
    value = args[0]
    return value - value.shift(params[WINDOW_PARAM])


def _ts_mean(args: list[Any], params: Mapping[str, Any]) -> Any:
    return _rolling(args[0], params[WINDOW_PARAM]).mean()


def _ts_sum(args: list[Any], params: Mapping[str, Any]) -> Any:
    return _rolling(args[0], params[WINDOW_PARAM]).sum()


def _ts_std(args: list[Any], params: Mapping[str, Any]) -> Any:
    return _rolling(args[0], params[WINDOW_PARAM]).std(ddof=1)


def _ts_min(args: list[Any], params: Mapping[str, Any]) -> Any:
    return _rolling(args[0], params[WINDOW_PARAM]).min()


def _ts_max(args: list[Any], params: Mapping[str, Any]) -> Any:
    return _rolling(args[0], params[WINDOW_PARAM]).max()


def _ts_rank(args: list[Any], params: Mapping[str, Any]) -> Any:
    """Percentile rank of the newest bar inside its own trailing window."""
    window = params[WINDOW_PARAM]
    return _rolling(args[0], window).apply(
        lambda values: pd.Series(values).rank(pct=True).iloc[-1],
        raw=False,
    )


def _ts_zscore(args: list[Any], params: Mapping[str, Any]) -> Any:
    value = args[0]
    window = params[WINDOW_PARAM]
    dispersion = _rolling(value, window).std(ddof=1)
    average = _rolling(value, window).mean()
    # A flat window has zero dispersion: NaN, not inf.
    return (value - average).div(dispersion.where(dispersion != 0))


def _ts_pct_change(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0].pct_change(params[WINDOW_PARAM], fill_method=None)


def _ts_corr(args: list[Any], params: Mapping[str, Any]) -> Any:
    window = params[WINDOW_PARAM]
    return args[0].rolling(window, min_periods=window).corr(args[1])


def _const(args: list[Any], params: Mapping[str, Any]) -> float:
    """Zero-arity numeric literal: the only way a number enters an expression."""
    return float(params["value"])


def _cs_rank(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0].rank(axis=1, pct=True)


def _cs_zscore(args: list[Any], params: Mapping[str, Any]) -> Any:
    value = args[0]
    spread = value.sub(value.mean(axis=1), axis=0)
    dispersion = value.std(axis=1, ddof=0)
    # A degenerate cross-section (every symbol equal) is neutral: 0, matching
    # factors/research.py:winsorize_zscore. Symbols without a value stay missing.
    return spread.div(dispersion.where(dispersion != 0), axis=0).fillna(0.0).where(spread.notna())


def _cs_demean(args: list[Any], params: Mapping[str, Any]) -> Any:
    value = args[0]
    return value.sub(value.mean(axis=1), axis=0)


def _neg(args: list[Any], params: Mapping[str, Any]) -> Any:
    return -args[0]


def _abs(args: list[Any], params: Mapping[str, Any]) -> Any:
    return abs(args[0])


def _sign(args: list[Any], params: Mapping[str, Any]) -> Any:
    return np.sign(args[0])


def _log(args: list[Any], params: Mapping[str, Any]) -> Any:
    value = args[0]
    return np.log(_masked(value, value > 0))


def _sqrt(args: list[Any], params: Mapping[str, Any]) -> Any:
    value = args[0]
    return np.sqrt(_masked(value, value >= 0))


def _add(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] + args[1]


def _sub(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] - args[1]


def _mul(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] * args[1]


def _div(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] / args[1]


def _safe_div(args: list[Any], params: Mapping[str, Any]) -> Any:
    left, right = args
    quotient = left / _masked(right, right != 0)
    return quotient.mask(~np.isfinite(quotient)) if _is_panel(quotient) else quotient


def _gt(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] > args[1]


def _lt(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] < args[1]


def _ge(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] >= args[1]


def _le(args: list[Any], params: Mapping[str, Any]) -> Any:
    return args[0] <= args[1]


def _where(args: list[Any], params: Mapping[str, Any]) -> Any:
    condition, when_true, when_false = args
    return when_true.where(condition, when_false)


def _clip(args: list[Any], params: Mapping[str, Any]) -> Any:
    lower, upper = params["lo"], params["hi"]
    value = args[0]
    return value.clip(lower=lower, upper=upper) if _is_panel(value) else np.clip(value, lower, upper)


# Mirrors dsl.OPERATOR_SPECS one-for-one; the registry test keeps the two in sync.
OPERATORS: dict[str, Callable[[list[Any], Mapping[str, Any]], pd.DataFrame | float]] = {
    "lag": _lag,
    "delta": _delta,
    "ts_mean": _ts_mean,
    "ts_sum": _ts_sum,
    "ts_std": _ts_std,
    "ts_min": _ts_min,
    "ts_max": _ts_max,
    "ts_rank": _ts_rank,
    "ts_zscore": _ts_zscore,
    "ts_pct_change": _ts_pct_change,
    "ts_corr": _ts_corr,
    "const": _const,
    "cs_rank": _cs_rank,
    "cs_zscore": _cs_zscore,
    "cs_demean": _cs_demean,
    "neg": _neg,
    "abs": _abs,
    "sign": _sign,
    "log": _log,
    "sqrt": _sqrt,
    "add": _add,
    "sub": _sub,
    "mul": _mul,
    "div": _div,
    "safe_div": _safe_div,
    "gt": _gt,
    "lt": _lt,
    "ge": _ge,
    "le": _le,
    "where": _where,
    "clip": _clip,
}


def apply_operator(name: str, args: list[Any], params: Mapping[str, Any]) -> pd.DataFrame | float:
    """Evaluate one DSL operator node. ``name`` must be a key of :data:`OPERATORS`."""
    return OPERATORS[name](args, params or {})
