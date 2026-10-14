"""Expression evaluation: one validated DSL tree in, one factor panel out.

A panel is a ``DataFrame``: rows are bars in ascending ``DatetimeIndex`` order, columns are
symbols. Every result is pinned to a reference panel -- ``fields["close"]`` when the mapping
has one, otherwise its first panel -- so dates and symbols line up across candidates. Missing
data stays missing: a field the mapping does not carry, or a symbol a field never covered,
becomes NaN rather than an error, and the coverage gate decides what a gap means.

Subtrees are memoized on their canonical spelling (:func:`~app.services.factor_mining.dsl.
normalize_expression`), so a subexpression shared by several branches is computed once. ``inf``
is deliberately not filtered here: the cross-sectional layer in
:mod:`app.services.strategy_v2.factor_research` maps it to NA per cross-section, and a second
filter would silently change coverage. This is a pure function layer -- no clocks, database,
or network access.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from app.services.factor_mining.dsl import normalize_expression
from app.services.factor_mining.operators import apply_operator


class ExpressionEvaluator:
    """Evaluate DSL expressions against a fixed set of price-volume panels."""

    def __init__(self, fields: Mapping[str, pd.DataFrame]) -> None:
        self._fields = fields
        reference = fields.get("close")
        if reference is None:
            reference = next(iter(fields.values()), pd.DataFrame())
        self._reference = reference
        self._cache: dict[str, Any] = {}
        self._hits = 0

    def evaluate(self, node: Any) -> pd.DataFrame:
        """Evaluate ``node`` -- an operator mapping or a bare field-name leaf.

        The cache lives for one call, so memory stays bounded by one expression.
        """
        self._cache.clear()
        self._hits = 0
        return self._aligned(self._eval(node))

    def cache_info(self) -> dict[str, int]:
        """Distinct subtrees and cache reuses of the most recent :meth:`evaluate` call."""
        return {"subtrees": len(self._cache), "hits": self._hits}

    def _eval(self, node: Any) -> Any:
        """Return a panel, or a plain number for an all-scalar subtree (``const``)."""
        key = normalize_expression(node).canonical
        if key in self._cache:
            self._hits += 1
            return self._cache[key]
        if isinstance(node, str):
            value: Any = self._field(node)
        else:
            value = apply_operator(
                node["op"],
                [self._eval(argument) for argument in node.get("args") or []],
                node.get("params") or {},
            )
        self._cache[key] = value
        return value

    def _field(self, name: str) -> pd.DataFrame:
        frame = self._fields.get(name)
        if frame is None:
            return pd.DataFrame(np.nan, index=self._reference.index, columns=self._reference.columns)
        return frame.reindex(index=self._reference.index, columns=self._reference.columns)

    def _aligned(self, value: Any) -> pd.DataFrame:
        """Pin a result to the reference panel, broadcasting an all-scalar root."""
        if isinstance(value, pd.DataFrame):
            return value.reindex(index=self._reference.index, columns=self._reference.columns)
        return pd.DataFrame(value, index=self._reference.index, columns=self._reference.columns)
