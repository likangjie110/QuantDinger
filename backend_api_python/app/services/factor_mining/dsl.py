"""Canonical factor expression DSL: validation, normalization, and fingerprints.

A candidate factor is a JSON expression tree: leaves are field names and internal
nodes are ``{"op": ..., "args": [...], "params": {...}}``. Numbers never appear as
bare leaves -- a literal enters an expression through the zero-arity ``const``
operator. This module is a pure function layer -- it never reads clocks,
databases, or the network.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

DSL_VERSION = 1
# The depth bound is a validation policy, not part of what an expression means, so it is not
# folded into the fingerprint: raising it must not re-key the stored ``expression_hash`` rows.
MAX_DEPTH = 8
MAX_NODES = 24
WINDOW_MIN = 1
WINDOW_MAX = 252
MAX_LOOKBACK = 504
PRICE_VOLUME_FIELDS = ("open", "high", "low", "close", "volume")
WINDOW_PARAM = "n"

# op -> (arity, windowed). Windowed operators shift or aggregate over one symbol's
# history and carry the window parameter ``WINDOW_PARAM``.
OPERATOR_SPECS: dict[str, tuple[int, bool]] = {
    "lag": (1, True),
    "delta": (1, True),
    "ts_mean": (1, True),
    "ts_sum": (1, True),
    "ts_std": (1, True),
    "ts_min": (1, True),
    "ts_max": (1, True),
    "ts_rank": (1, True),
    "ts_zscore": (1, True),
    "ts_pct_change": (1, True),
    "ts_corr": (2, True),
    "const": (0, False),
    "cs_rank": (1, False),
    "cs_zscore": (1, False),
    "cs_demean": (1, False),
    "neg": (1, False),
    "abs": (1, False),
    "sign": (1, False),
    "log": (1, False),
    "sqrt": (1, False),
    "add": (2, False),
    "sub": (2, False),
    "mul": (2, False),
    "div": (2, False),
    "safe_div": (2, False),
    "gt": (2, False),
    "lt": (2, False),
    "ge": (2, False),
    "le": (2, False),
    "where": (3, False),
    "clip": (1, False),
}

_CLIP_PARAMS = ("lo", "hi")


class ExpressionError(ValueError):
    """Rejected expression. ``code`` is a stable i18n key, not a rendered message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject(reason: str) -> ExpressionError:
    return ExpressionError(f"factorMining.reject.invalid:{reason}")


def _is_finite_number(value: Any) -> bool:
    """Finite int/float. ``bool`` is excluded even though it subclasses ``int``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # int is always finite; routing it through math.isfinite would overflow.
    return not isinstance(value, float) or math.isfinite(value)


def _canonical_node(current: Any) -> Any:
    """Rebuild a validated node with stable spelling: sorted params, empty params dropped.

    ``json.dumps`` renders numbers through ``repr``, which keeps float spelling stable.
    """
    if not isinstance(current, dict):
        return current
    normalized: dict[str, Any] = {
        "op": current["op"],
        "args": [_canonical_node(argument) for argument in current.get("args") or []],
    }
    params = current.get("params") or {}
    if params:
        normalized["params"] = {key: params[key] for key in sorted(params)}
    return normalized


@dataclass(frozen=True)
class NormalizedExpression:
    node: dict
    canonical: str
    fingerprint: str
    fields: tuple[str, ...]
    windows: tuple[int, ...]
    lookback: int
    node_count: int
    depth: int


def normalize_expression(
    node: Any,
    *,
    allowed_fields: Collection[str] = PRICE_VOLUME_FIELDS,
) -> NormalizedExpression:
    """Validate ``node`` and report its canonical form, fingerprint, and budgets.

    ``canonical`` re-spells the tree with sorted keys and empty ``params`` dropped, so
    spelling variants of one expression share a fingerprint. ``lookback`` is the largest
    window sum along any root-to-leaf path; ``depth`` counts the root as 1 and
    ``node_count`` counts every leaf and operator node.
    """
    fields: set[str] = set()
    windows: set[int] = set()
    node_count = 0
    depth_reached = 0

    def walk(current: Any, depth: int, carried: int) -> int:
        nonlocal node_count, depth_reached
        node_count += 1
        if node_count > MAX_NODES:
            raise _reject("tooManyNodes")
        depth_reached = max(depth_reached, depth)
        if depth > MAX_DEPTH:
            raise _reject("tooDeep")
        if isinstance(current, str):
            if current not in allowed_fields:
                raise _reject("unknownField")
            fields.add(current)
            return carried
        if not isinstance(current, dict):
            raise _reject("badNode")
        op = current.get("op")
        spec = OPERATOR_SPECS.get(op) if isinstance(op, str) else None
        if spec is None:
            raise _reject("unknownOperator")
        arity, windowed = spec
        args = current.get("args")
        if args is None:
            args = []
        if not isinstance(args, (list, tuple)) or len(args) != arity:
            raise _reject("badArity")
        params = current.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise _reject("badNode")
        own = carried
        if windowed:
            window = params.get(WINDOW_PARAM)
            # bool is an int subclass; reject it explicitly.
            if isinstance(window, bool) or not isinstance(window, int):
                raise _reject("badWindow")
            if not WINDOW_MIN <= window <= WINDOW_MAX:
                raise _reject("badWindow")
            windows.add(window)
            own += window
        elif op == "const":
            if not _is_finite_number(params.get("value")):
                raise _reject("badNode")
        elif op == "clip":
            if not all(_is_finite_number(params.get(key)) for key in _CLIP_PARAMS):
                raise _reject("badNode")
        if own > MAX_LOOKBACK:
            raise _reject("lookbackExceeded")
        if not args:
            # Zero-arity operators (const) terminate the path at this node.
            return own
        return max(walk(argument, depth + 1, own) for argument in args)

    lookback = walk(node, 1, 0)
    canonical = json.dumps(_canonical_node(node), sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(f"{canonical}:v{DSL_VERSION}".encode("utf-8")).hexdigest()
    return NormalizedExpression(
        node=node,
        canonical=canonical,
        fingerprint=fingerprint,
        fields=tuple(sorted(fields)),
        windows=tuple(sorted(windows)),
        lookback=lookback,
        node_count=node_count,
        depth=depth_reached,
    )
