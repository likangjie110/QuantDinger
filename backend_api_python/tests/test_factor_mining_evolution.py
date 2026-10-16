"""Factor mining evolution: mutation and crossover stay inside the DSL's bounds."""

import random

import pandas as pd

from app.services.factor_mining import evolution
from app.services.factor_mining.dsl import MAX_DEPTH, WINDOW_MAX, WINDOW_MIN, normalize_expression
from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.factor_mining.evolution import crossover, evolve, mutate
from app.services.factor_mining.templates import CandidateSeed


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


def _seed(expression, template_id="momentum"):
    """A candidate shaped like ``expand_candidates`` output, so the loop is fed what the engine feeds it."""
    return CandidateSeed(
        expression=expression,
        fingerprint=normalize_expression(expression).fingerprint,
        template_id=template_id,
        origin="template",
        params={},
    )


def _cohort():
    """A first generation with a plain window, a compound tree and a cross-sectional rank."""
    return [
        _seed({"op": "ts_pct_change", "args": ["close"], "params": {"n": 5}}),
        _seed({"op": "neg", "args": [{"op": "ts_std", "args": [{"op": "ts_pct_change", "args": ["close"],
                                                                   "params": {"n": 1}}], "params": {"n": 20}}],
               "params": {}}, template_id="low_volatility"),
        _seed({"op": "cs_rank", "args": [{"op": "ts_mean", "args": ["volume"], "params": {"n": 10}}],
               "params": {}}, template_id="liquidity_level"),
        _seed({"op": "safe_div", "args": ["volume", {"op": "ts_mean", "args": ["volume"], "params": {"n": 20}}],
               "params": {}}, template_id="volume_price_confirm"),
    ]


def _saturated_cohort():
    """Trees sitting on the DSL's budgets, where a graft into them rarely fits: refusal is the norm."""
    nested = {"op": "ts_mean", "args": [{"op": "ts_mean", "args": ["close"], "params": {"n": 252}}],
              "params": {"n": 252}}
    correlated = {"op": "ts_corr", "args": ["close", {"op": "ts_mean", "args": ["volume"], "params": {"n": 240}}],
                  "params": {"n": 240}}
    return [
        _seed(_chain(MAX_DEPTH)),
        _seed(nested),
        _seed({"op": "add", "args": [_chain(MAX_DEPTH - 1), correlated], "params": {}}),
        _seed({"op": "mul", "args": [_chain(MAX_DEPTH - 1), _chain(MAX_DEPTH - 1)], "params": {}}),
        _seed(correlated),
        _seed({"op": "neg", "args": [nested], "params": {}}),
    ]


def _tiny_cohort():
    """Small trees, where a rewrite's shortest way out is a leaf at the root."""
    return [
        _seed(_chain(2)),
        _seed({"op": "abs", "args": ["volume"], "params": {}}),
        _seed({"op": "neg", "args": ["open"], "params": {}}),
    ]


def _shorter_wins(expression):
    """A deterministic stand-in for the injected metric: the smaller tree scores higher."""
    return -float(normalize_expression(expression).node_count)


def _evolve(seeds, **overrides):
    """``evolve`` with the run parameters these tests hold fixed, so each test names its own."""
    settings = {"budget": 60, "seed": 17, "population": 4, "generations": 20, "elite_ratio": 0.25,
                "fitness": _shorter_wins}
    settings.update(overrides)
    return list(evolve(seeds, **settings))


def test_evolution_is_byte_identical_for_the_same_seed():
    def run():
        return _evolve(_cohort())

    first, second = run(), run()
    assert first, "no individual was produced, so the two runs agree about nothing"
    assert first == second
    spellings = [normalize_expression(row.expression).canonical for row in first]
    assert spellings == [normalize_expression(row.expression).canonical for row in second]


def test_evolution_respects_budget():
    cohort = _cohort()
    rows = _evolve(cohort, budget=30, seed=23, generations=1000)
    assert 0 < len(rows) <= 30
    for row in rows:
        assert row.origin == "evolution"
        assert row.template_id, "a child keeps the template of the lineage it grew out of"
        assert row.fingerprint == normalize_expression(row.expression).fingerprint
    # A smaller budget buys a prefix of a larger one, never a different search.
    longer = _evolve(cohort, budget=40, seed=23, generations=1000)
    assert len(longer) > len(rows), "the larger budget bought nothing, so the prefix would prove nothing"
    assert longer[:len(rows)] == rows


def test_rejected_children_are_counted_not_silently_dropped(monkeypatch):
    """A refused draw spends a unit of budget: the search pays for every attempt, not every child."""
    draws = []

    def counted(operator):
        def spy(*arguments):
            child = operator(*arguments)
            draws.append(child)
            return child
        return spy

    for name in ("mutate", "crossover"):
        monkeypatch.setattr(evolution, name, counted(getattr(evolution, name)))

    rows = _evolve(_saturated_cohort(), budget=120, seed=29, population=6, generations=1000)
    refused = [child for child in draws if child is None]
    assert refused, "no draw was refused, so nothing about refusals was tested"
    assert len(draws) <= 120, "a refused draw was retried for free instead of spending the budget"
    assert len(rows) < 120, "the run filled a quota of children, which is what refusals must prevent"


def test_a_duplicate_expression_is_never_produced_twice(monkeypatch):
    """One expression is one trial, so the caller is never handed the same candidate again.

    The spy counts the rewrites that came back as a mapping, which is the only thing
    deduplication decides on: each of those either reaches the caller or is dropped for
    repeating an expression the run already has. Nothing else can stand between the two, so
    the shortfall is the deduplication itself -- and it is budget the run still paid.
    """
    produced = []

    def counted(operator):
        def spy(*arguments):
            child = operator(*arguments)
            if isinstance(child, dict):
                produced.append(child)
            return child
        return spy

    for name in ("mutate", "crossover"):
        monkeypatch.setattr(evolution, name, counted(getattr(evolution, name)))

    rows = _evolve(_cohort(), budget=200, seed=41, generations=1000)
    fingerprints = [row.fingerprint for row in rows]
    assert fingerprints, "the run produced nothing, so it cannot have repeated anything"
    assert len(set(fingerprints)) == len(fingerprints), "the stream handed out one expression twice"
    assert len(produced) > len(rows), "no draw repeated an expression, so nothing was skipped"


def test_a_bare_field_child_is_never_produced(monkeypatch):
    """The DSL lets a leaf sit at the root; a candidate this pipeline passes on is a mapping.

    The spy is what keeps this honest: it holds the rewrites that came back as a bare field
    name, the exact answer that must not reach the caller.
    """
    drawn = []

    def counted(operator):
        def spy(*arguments):
            child = operator(*arguments)
            drawn.append(child)
            return child
        return spy

    for name in ("mutate", "crossover"):
        monkeypatch.setattr(evolution, name, counted(getattr(evolution, name)))

    rows = _evolve(_tiny_cohort(), budget=120, seed=43, generations=1000)
    assert any(isinstance(child, str) for child in drawn), (
        "no rewrite ever returned a leaf, so the refusal was never exercised")
    assert rows, "the run produced nothing"
    assert all(isinstance(row.expression, dict) for row in rows), "a bare field name reached the caller"


def test_elite_survives_across_generations():
    """The best individual of one generation sits in the next, where the ranking asks about it again.

    The metric is the observation point: ranking a generation consults ``fitness`` about every
    member of it, so an expression the caller is asked about twice is one that lived through
    two generations. An elite the shuffle was allowed to drop would be asked about once.
    """
    cohort = _cohort()
    champion = cohort[-1].fingerprint
    ranked = []

    def fitness(expression):
        fingerprint = normalize_expression(expression).fingerprint
        ranked.append(fingerprint)
        return 1.0 if fingerprint == champion else 0.0

    rows = _evolve(cohort, budget=60, seed=31, generations=6, fitness=fitness)
    assert rows, "the run produced nothing, so no generation after the first was ever ranked"
    assert ranked.count(champion) >= 2, (
        "the best individual was ranked in one generation only, so it was not kept")


def test_an_unscored_individual_is_never_an_elite():
    """``None`` is "no opinion", not "worst": such a member breeds, but it is never kept."""
    cohort = _cohort()
    # The highest fingerprint wins every tie, so a ranking that reads ``None`` as a score
    # would hand the elite slot to exactly this member.
    abstains = max(cohort, key=lambda member: member.fingerprint).fingerprint
    ranked = []

    def fitness(expression):
        fingerprint = normalize_expression(expression).fingerprint
        ranked.append(fingerprint)
        return None if fingerprint == abstains else 0.0

    rows = _evolve(cohort, budget=60, seed=37, generations=4, fitness=fitness)
    assert rows
    assert ranked.count(abstains) == 1, "an unscored individual was kept as an elite"


def test_degenerate_runs_still_terminate():
    """No cohort, no budget, no generations, one member: each of them ends, none of them crashes."""
    assert _evolve([]) == []
    assert _evolve(_cohort(), budget=0) == []
    assert _evolve(_cohort(), generations=0) == []
    # A budget under the size of the first generation is a hard stop, not an escape hatch.
    assert len(_evolve(_cohort(), budget=2, seed=3)) <= 2
    # One member: the pool is the individual itself, drawn against itself.
    assert len(_evolve(_cohort()[:1], budget=5, seed=3, population=1, elite_ratio=0.0)) <= 5
