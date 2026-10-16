"""Constrained expression-tree mutation and crossover for the factor search.

These are the search operators of factor evolution: pure tree rewrites that read no data,
keep no state, and take every random draw from the ``random.Random`` a caller passes in, so
one (tree, seed) pair always reproduces the same offspring. Legality is not decided here --
every candidate goes through :func:`~app.services.factor_mining.dsl.normalize_expression`,
and ``None`` means "this draw was refused": either the DSL rejected the rewrite, or the
rewrite only re-spelled its parent. A refusal is not an error and says nothing about the
tree, so a caller simply draws again.

``mutate`` picks one of three modes by weight: perturb one windowed node's window (0.4),
substitute one node's operator with a same-arity one (0.3), or replace one whole subtree
with a fresh random one (0.3). A mode the tree gives no locus to drops out of the draw and
the remaining modes keep their relative weight, so a tree that is a bare field still gets
subtree replacement. Window perturbation re-samples out-of-range draws instead of clamping
them -- clamping would systematically over-sample ``WINDOW_MIN`` and ``WINDOW_MAX``.
``crossover`` grafts exactly one subtree of ``right`` into ``left``.

Inputs are assumed to be DSL-valid trees: both operators validate what they produce, not
what they are handed.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

from app.services.factor_mining.dsl import (
    OPERATOR_SPECS,
    PRICE_VOLUME_FIELDS,
    WINDOW_MAX,
    WINDOW_MIN,
    WINDOW_PARAM,
    ExpressionError,
    normalize_expression,
)

_MODE_WEIGHTS = {"window": 0.4, "operator": 0.3, "subtree": 0.3}

# Relative window jumps a perturbation may take. Out-of-range picks are re-sampled.
_WINDOW_STEPS = (-10, -5, -2, -1, 1, 2, 5, 10)

# Operators whose parameters are not just a window: ``const`` carries ``value`` and ``clip``
# carries ``lo``/``hi``, so a swap that only carries ``n`` over cannot reach either of them.
_PARAM_OPS = ("const", "clip")

# Fresh subtrees never build ``where``: its first operand has to be a condition, and a fresh
# leaf is a panel or a scalar, so a depth-2 ``where`` is unreadable by construction. A
# ``where`` that a parent already carries can still be rewritten or grafted onto.
_FRESH_EXCLUDED = (*_PARAM_OPS, "where")

_FRESH_OPERATORS = tuple(sorted(
    op for op, (arity, _windowed) in OPERATOR_SPECS.items() if arity > 0 and op not in _FRESH_EXCLUDED
))
_FRESH_CONSTANTS = (-1.0, 0.0, 0.5, 1.0)


def mutate(node: dict | str, rng: random.Random) -> dict | str | None:
    """Return one child of ``node`` rewritten at a single locus, or ``None`` if refused."""
    pool = _mutation_pool(node)
    cut = rng.random() * sum(_MODE_WEIGHTS[name] for name in pool)
    for name in pool:
        cut -= _MODE_WEIGHTS[name]
        if cut <= 0:
            break
    if name == "window":
        candidate = _perturb_window(node, rng)
    elif name == "operator":
        candidate = _substitute_operator(node, rng)
    else:
        candidate = _replace_subtree(node, rng)
    return _accept(candidate, node)


def crossover(left: dict | str, right: dict | str, rng: random.Random) -> dict | str | None:
    """Return ``left`` with one subtree of ``right`` grafted into it, or ``None`` if refused.

    One graft point, so the child stays one locus away from ``left`` and inherits the rest
    of its structure from it. ``None`` covers a graft the DSL rejects and a graft that would
    have handed back ``left`` unchanged.
    """
    _donor_path, donor = rng.choice(list(_walk(right)))
    graft_path = rng.choice(list(_walk(left)))[0]
    return _accept(_replace(left, graft_path, donor), left)


def _mutation_pool(node: dict | str) -> list[str]:
    """The modes ``node`` has a locus for, in a fixed order.

    Subtree replacement always applies: the root is a subtree like any other, so no tree --
    not even a bare field -- is out of reach for mutation.
    """
    pool = []
    if any(_is_windowed(current) for _path, current in _walk(node)):
        pool.append("window")
    if any(_alternatives(current) for _path, current in _walk(node)):
        pool.append("operator")
    pool.append("subtree")
    return pool


def _perturb_window(node: dict | str, rng: random.Random) -> dict | str:
    path, target = rng.choice([(path, current) for path, current in _walk(node) if _is_windowed(current)])
    window = _draw_window(target["params"][WINDOW_PARAM], rng)
    return _replace(node, path, {**target, "params": {**target["params"], WINDOW_PARAM: window}})


def _draw_window(current: int, rng: random.Random) -> int:
    """A window near ``current`` that is inside the bounds and differs from it."""
    while True:
        candidate = current + rng.choice(_WINDOW_STEPS)
        if candidate != current and WINDOW_MIN <= candidate <= WINDOW_MAX:
            return candidate


def _substitute_operator(node: dict | str, rng: random.Random) -> dict | str:
    path, target = rng.choice([(path, current) for path, current in _walk(node) if _alternatives(current)])
    op = rng.choice(_alternatives(target))
    previous = (target.get("params") or {}).get(WINDOW_PARAM)
    # A non-windowed target drops the window rather than carrying a stale ``n``: that number
    # would mean nothing, and it would split one expression into two fingerprints.
    params = {WINDOW_PARAM: previous if previous is not None else rng.randint(WINDOW_MIN, WINDOW_MAX)} \
        if OPERATOR_SPECS[op][1] else {}
    return _replace(node, path, {"op": op, "args": list(target.get("args") or []), "params": params})


def _replace_subtree(node: dict | str, rng: random.Random) -> dict | str:
    path = rng.choice(list(_walk(node)))[0]
    return _replace(node, path, _fresh_subtree(rng))


def _fresh_subtree(rng: random.Random) -> dict | str:
    """A small fresh expression: a field, a const, or one operator over fresh leaves.

    Depth-2 by construction, so grafting it can only break the node, depth, or lookback
    budget of its host -- and ``normalize_expression`` is what decides that.
    """
    roll = rng.random()
    if roll < 0.4:
        return rng.choice(PRICE_VOLUME_FIELDS)
    if roll < 0.5:
        return {"op": "const", "args": [], "params": {"value": rng.choice(_FRESH_CONSTANTS)}}
    op = rng.choice(_FRESH_OPERATORS)
    arity, windowed = OPERATOR_SPECS[op]
    params = {WINDOW_PARAM: rng.randint(WINDOW_MIN, WINDOW_MAX)} if windowed else {}
    return {"op": op, "args": [rng.choice(PRICE_VOLUME_FIELDS) for _ in range(arity)], "params": params}


def _accept(candidate: dict | str, parent: dict | str) -> dict | str | None:
    """``candidate`` if the DSL accepts it and it is more than its parent re-spelled."""
    try:
        accepted = normalize_expression(candidate)
    except ExpressionError:
        return None
    if accepted.fingerprint == normalize_expression(parent).fingerprint:
        return None
    return candidate


def _is_windowed(node: dict | str) -> bool:
    return isinstance(node, dict) and OPERATOR_SPECS[node["op"]][1]


def _alternatives(node: dict | str) -> tuple[str, ...]:
    """Same-arity operators this node could carry instead, in a stable order."""
    if not isinstance(node, dict):
        return ()
    arity = OPERATOR_SPECS[node["op"]][0]
    return tuple(sorted(op for op, (other_arity, _windowed) in OPERATOR_SPECS.items()
                        if other_arity == arity and op != node["op"] and op not in _PARAM_OPS))


def _walk(node: dict | str, path: tuple[int, ...] = ()) -> Iterator[tuple[tuple[int, ...], dict | str]]:
    """Every node of the tree with its path, in pre-order; a path is a tuple of arg indices."""
    yield path, node
    if isinstance(node, dict):
        for index, argument in enumerate(node.get("args") or []):
            yield from _walk(argument, path + (index,))


def _replace(node: dict | str, path: tuple[int, ...], replacement: dict | str) -> dict | str:
    """Copy ``node`` with the subtree at ``path`` swapped out; untouched branches are shared."""
    if not path:
        return replacement
    head, *rest = path
    args = list(node["args"])
    args[head] = _replace(args[head], tuple(rest), replacement)
    return {**node, "args": args}
