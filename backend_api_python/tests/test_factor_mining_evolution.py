"""Factor mining evolution: mutation and crossover stay inside the DSL's bounds."""

import random

import pandas as pd

from app.services.factor_mining.dsl import MAX_DEPTH, WINDOW_MAX, WINDOW_MIN, normalize_expression
from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.factor_mining.evolution import crossover, mutate


def _parent():
    """A tree with a boundary window (n=1), a wider window, and a non-windowed operator."""
    return {"op": "safe_div", "args": [
        {"op": "ts_pct_change", "args": ["close"], "params": {"n": 1}},
        {"op": "ts_mean", "args": [{"op": "abs", "args": ["volume"], "params": {}}], "params": {"n": 20}},
    ], "params": {}}


def _chain(depth):
    """A chain of ``depth`` unary nodes: the deepest tree the DSL accepts, one leaf at the end."""
    node = "close"
    for _ in range(depth - 1):
        node = {"op": "neg", "args": [node], "params": {}}
    return node


def _windows(node):
    """Every raw ``n`` in a tree -- read from the tree itself, not from the DSL's verdict."""
    if not isinstance(node, dict):
        return []
    found = [value for key, value in (node.get("params") or {}).items() if key == "n"]
    for argument in node.get("args") or []:
        found += _windows(argument)
    return found


def _rewrite_locus(left, right, path=()):
    """Paths where ``right`` rewrote ``left``; a single path means a single rewrite.

    A rewritten node swallows its descendants, because a replacement subtree that happens to
    share the rewritten node's operator leaves exactly the same differences behind as a
    rewrite of that node -- two reads no tree pair can tell apart.
    """
    if left == right:
        return set()
    if not (isinstance(left, dict) and isinstance(right, dict)) or left.get("op") != right.get("op"):
        return {path}
    left_args, right_args = left.get("args") or [], right.get("args") or []
    if len(left_args) != len(right_args) or (left.get("params") or {}) != (right.get("params") or {}):
        return {path}
    loci = set()
    for index, (before, after) in enumerate(zip(left_args, right_args)):
        loci |= _rewrite_locus(before, after, (*path, index))
    return loci or {path}


def _panels(periods=80):
    """Synthetic panels covering every DSL field, so a mutated child is really evaluated."""
    index = pd.date_range("2025-01-01", periods=periods, freq="B")

    def series(base, step):
        return pd.DataFrame(
            {symbol: [base + step * (day % 7) + offset for day in range(periods)]
             for offset, symbol in enumerate(["A", "B", "C"])},
            index=index,
        )

    close = series(100.0, 2.0)
    return {"open": close - 1.0, "high": close + 1.0, "low": close - 2.0,
            "close": close, "volume": series(1000.0, 50.0)}


def test_mutation_never_emits_a_nonpositive_window():
    parent = _parent()
    rng = random.Random(1)
    produced = 0
    for _ in range(200):
        child = mutate(parent, rng)
        if child is None:
            continue
        produced += 1
        for window in _windows(child):
            assert WINDOW_MIN <= window <= WINDOW_MAX, f"window {window} escaped the legal range"
        normalize_expression(child)
    assert produced > 0, "every draw was refused, so no window was ever checked"


def test_mutation_changes_exactly_one_locus():
    parent = _parent()
    rng = random.Random(2)
    produced = 0
    for _ in range(200):
        child = mutate(parent, rng)
        if child is None:
            continue
        produced += 1
        loci = _rewrite_locus(parent, child)
        assert len(loci) == 1, f"expected one rewritten locus, got {sorted(loci)}"
        assert normalize_expression(child).fingerprint != normalize_expression(parent).fingerprint
    assert produced > 100, f"only {produced} of 200 draws produced a child"


def test_mutation_at_the_depth_budget_is_legal_or_none():
    """A graft that would push the tree past the DSL's budgets comes back as ``None``.

    Refusing is the whole point: handing the caller a tree the DSL rejects would be the one
    outcome these operators promise never to produce.
    """
    parent = _chain(MAX_DEPTH)
    rng = random.Random(8)
    outcomes = [mutate(parent, rng) for _ in range(120)]
    assert any(child is None for child in outcomes), "nothing was refused, so the budget was never hit"
    for child in outcomes:
        if child is not None:
            normalize_expression(child)
            assert len(_rewrite_locus(parent, child)) == 1


def test_the_locus_oracle_notices_a_second_rewrite():
    """Two tests above are only as sharp as this helper, so it gets checked itself."""
    parent = {"op": "add", "args": [{"op": "neg", "args": ["close"], "params": {}}, "close"], "params": {}}
    one = {"op": "add", "args": [{"op": "abs", "args": ["close"], "params": {}}, "close"], "params": {}}
    two = {"op": "add", "args": [{"op": "abs", "args": ["close"], "params": {}}, "volume"], "params": {}}
    assert _rewrite_locus(parent, parent) == set()
    assert _rewrite_locus(parent, one) == {(0,)}
    assert sorted(_rewrite_locus(parent, two)) == [(0,), (1,)]


def test_crossover_children_are_legal_or_none():
    parents = [
        _parent(),
        {"op": "ts_mean", "args": ["close"], "params": {"n": 5}},
        {"op": "add", "args": [{"op": "cs_rank", "args": ["volume"], "params": {}}, "open"], "params": {}},
        # A tree at the depth budget: donors grafted low in it cannot fit, so the refusal
        # path is exercised rather than assumed.
        _chain(MAX_DEPTH),
        "close",
    ]
    rng = random.Random(4)
    produced = refused = 0
    for left in parents:
        for right in parents:
            for _ in range(3):
                child = crossover(left, right, rng)
                if child is None:
                    refused += 1
                    continue
                produced += 1
                normalize_expression(child)
                assert len(_rewrite_locus(left, child)) == 1, "a child must be one grafted locus"
    assert produced > 0, "every graft was refused, so no child was ever validated"
    assert refused > 0, "nothing was refused, so the illegal-graft path was never reached"


def test_operators_do_not_leak_future_bars_after_mutation():
    fields = _panels()
    rewritten = {name: panel.copy() for name, panel in fields.items()}
    for panel in rewritten.values():
        panel.iloc[-1] = panel.iloc[-1] * 3.0
    parent = _parent()
    rng = random.Random(6)
    checked = informative = 0
    for _ in range(60):
        child = mutate(parent, rng)
        if child is None:
            continue
        try:
            before = ExpressionEvaluator(fields).evaluate(child).iloc[:-1]
            after = ExpressionEvaluator(rewritten).evaluate(child).iloc[:-1]
        except Exception:
            # The DSL bounds shape and ranges, not values: a panel-only operator handed a
            # ``const`` is legal and unreadable. The engine rejects such a candidate as
            # invalid; here it simply cannot be asked about prefix invariance.
            continue
        checked += 1
        pd.testing.assert_frame_equal(before, after)
        if before.notna().any().any():
            informative += 1
    assert checked > 0, "every draw was refused or unreadable, so no descendant was ever evaluated"
    assert informative > 0, "every compared panel was all NaN, so the comparison proved nothing"


def test_mutation_is_reproducible_for_the_same_seed():
    parent = _parent()

    def draw():
        rng = random.Random(9)
        children = [mutate(parent, rng) for _ in range(20)]
        return [None if child is None else normalize_expression(child).fingerprint for child in children]

    assert draw() == draw()


def test_a_bare_field_leaf_can_still_be_mutated():
    rng = random.Random(13)
    children = [mutate("close", rng) for _ in range(20)]
    produced = [child for child in children if child is not None]
    assert produced, "a leaf has no window and no operator, so subtree replacement is its only mode"
    for child in produced:
        normalize_expression(child)
        assert _rewrite_locus("close", child) == {()}, "the root is the only locus a leaf has"
