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

:func:`evolve` is the loop those operators are drawn by: a deterministic, budgeted search
over ``CandidateSeed`` rows that keeps the best of each generation and yields each new
individual it breeds.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator, Sequence

from app.services.factor_mining.dsl import (
    OPERATOR_SPECS,
    PRICE_VOLUME_FIELDS,
    WINDOW_MAX,
    WINDOW_MIN,
    WINDOW_PARAM,
    ExpressionError,
    normalize_expression,
)
from app.services.factor_mining.templates import CandidateSeed

_MODE_WEIGHTS = {"window": 0.4, "operator": 0.3, "subtree": 0.3}

# Half of a generation is bred by grafting two parents together and half by rewriting one.
_CROSSOVER_SHARE = 0.5

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


def mutate(node: dict | str, rng: random.Random, *,
           allowed_fields: Sequence[str] = PRICE_VOLUME_FIELDS) -> dict | str | None:
    """Return one child of ``node`` rewritten at a single locus, or ``None`` if refused.

    ``allowed_fields`` is the field set the child is validated and its leaves drawn against --
    ``fields_for(market)`` for a run. A set that refuses the parent's own leaves refuses every
    draw that keeps one, so the default is only correct for a run that has none of the wider
    market's fields.
    """
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
        candidate = _replace_subtree(node, rng, allowed_fields)
    return _accept(candidate, node, allowed_fields)


def crossover(left: dict | str, right: dict | str, rng: random.Random, *,
              allowed_fields: Sequence[str] = PRICE_VOLUME_FIELDS) -> dict | str | None:
    """Return ``left`` with one subtree of ``right`` grafted into it, or ``None`` if refused.

    One graft point, so the child stays one locus away from ``left`` and inherits the rest
    of its structure from it. ``None`` covers a graft the DSL rejects and a graft that would
    have handed back ``left`` unchanged. ``allowed_fields`` is the set both sides are read
    against, as in :func:`mutate`.
    """
    _donor_path, donor = rng.choice(list(_walk(right)))
    graft_path = rng.choice(list(_walk(left)))[0]
    return _accept(_replace(left, graft_path, donor), left, allowed_fields)


def evolve(
    seeds: Sequence[CandidateSeed],
    *,
    budget: int,
    seed: int,
    population: int,
    generations: int,
    elite_ratio: float,
    fitness: Callable[[dict], float | None],
    allowed_fields: Sequence[str] = PRICE_VOLUME_FIELDS,
) -> Iterator[CandidateSeed]:
    """Breed ``seeds`` for up to ``generations`` generations, yielding each new individual.

    The seeds are the first generation. Every generation after it is the best ``elite_ratio``
    share of its predecessor, kept as it is, plus children drawn from the whole of it, so a
    generation never outgrows ``population`` (the first one may start larger and settle into
    it). A child is one tournament-selected parent rewritten by :func:`mutate`, or two of
    them grafted by :func:`crossover`, and it carries the source template of the parent whose
    structure it grew out of. ``origin`` is ``"evolution"``, so a caller can tell a bred
    individual from a template expansion without reading the expression.

    ``fitness`` is the caller's own metric, consulted about every member of a generation and
    never cached -- the caller's opinion, not a remembered one, is what ranks a generation.
    ``None`` on an expression means the caller has no opinion about it: such a member can
    never be an elite, because a generation's elites are the head of a ranking and an
    unranked expression has no place in one, but it stays eligible to breed.

    ``allowed_fields`` is the run's field set, in the order fresh leaves are drawn from. It
    travels to every rewrite (``mutate``, ``crossover``) and to the fingerprint of each child.

    ``budget`` counts draws, not survivors. A draw that comes back refused -- an illegal
    rewrite, one that only re-spelled its parent, or one whose root is a bare field name --
    yields nothing and still spends one unit, and so does a draw that reproduces an expression
    the run already has: one expression is one trial, so a caller must not be handed the same
    candidate twice, while asking for a quota of distinct children instead would spin once the
    neighbourhood of a population is used up and would take the budget's job of ending the
    search. The stream ends when the budget is spent, when the generations run out, or when a
    generation produces no legal child at all; the last one ends the search rather than
    repeating a generation that has nothing to draw from.

    Determinism is the whole point: the only source of randomness is ``seed``, every
    collection is ordered before it is walked, and the same
    ``(seeds, budget, seed, population, generations, elite_ratio, allowed_fields)`` therefore
    breeds the same individuals, in the same order, spelled the same way.
    """
    if budget <= 0 or generations <= 0 or population <= 0 or not seeds:
        return
    rng = random.Random(seed)
    elite_slots = int(population * elite_ratio)
    members = list(seeds)
    # Fingerprints this run carries: the ones it was seeded with and the ones it has already
    # bred. Deduplication is a filter on what is yielded and nothing else -- a skipped draw
    # spends budget like a refused one and never joins a generation.
    known = {member.fingerprint for member in members}
    for _ in range(generations):
        if budget <= 0:
            return
        scored = _scored(members, fitness)
        elites = [member for score, member in scored if score is not None][:elite_slots]
        children: list[CandidateSeed] = []
        while len(children) < population - len(elites) and budget > 0:
            budget -= 1
            child = _child(scored, rng, allowed_fields)
            if child is None or child.fingerprint in known:
                continue
            known.add(child.fingerprint)
            children.append(child)
            yield child
        if not children:
            return
        members = elites + children


def _scored(
    members: Sequence[CandidateSeed],
    fitness: Callable[[dict], float | None],
) -> list[tuple[float | None, CandidateSeed]]:
    """Each member with its score, best first.

    The order is total, so no draw ever depends on the order the caller handed the members
    in: a scored member beats an unscored one, a higher score beats a lower one, and the
    fingerprint breaks what is left.
    """
    scored = [(fitness(member.expression), member) for member in members]
    scored.sort(key=lambda pair: _rank_key(*pair), reverse=True)
    return scored


def _rank_key(score: float | None, member: CandidateSeed) -> tuple[bool, float, str]:
    """Best first: scored before unscored, then the score, then the fingerprint."""
    return (score is not None, score if score is not None else 0.0, member.fingerprint)


def _tournament(
    scored: Sequence[tuple[float | None, CandidateSeed]],
    rng: random.Random,
) -> CandidateSeed:
    """One parent: the better of two distinct members drawn from a generation, best first.

    ``scored`` is already ranked best first, so the lower index wins -- the tournament reads
    the ranking itself instead of asking the caller's metric a second question about the same
    expression, which could answer differently.
    """
    return scored[min(rng.sample(range(len(scored)), min(2, len(scored))))][1]


def _child(
    scored: Sequence[tuple[float | None, CandidateSeed]],
    rng: random.Random,
    allowed_fields: Sequence[str],
) -> CandidateSeed | None:
    """One draw against the budget: an individual, or ``None`` if nothing usable came back."""
    if rng.random() < _CROSSOVER_SHARE:
        left = _tournament(scored, rng)
        right = _tournament(scored, rng)
        expression = crossover(left.expression, right.expression, rng,
                               allowed_fields=allowed_fields)
        template_id = left.template_id
    else:
        parent = _tournament(scored, rng)
        expression = mutate(parent.expression, rng, allowed_fields=allowed_fields)
        template_id = parent.template_id
    if not isinstance(expression, dict):
        # A refused draw -- and a bare field name is refused with it. The DSL allows a leaf at
        # the root, but every candidate this pipeline passes on is a mapping (the templates
        # never build one), and a candidate that is sometimes a name and sometimes a mapping
        # would push that difference onto everything downstream.
        return None
    return CandidateSeed(
        expression=expression,
        fingerprint=normalize_expression(expression, allowed_fields=allowed_fields).fingerprint,
        template_id=template_id,
        origin="evolution",
        params={},
    )


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
    if OPERATOR_SPECS[op][1]:
        window = previous if previous is not None else rng.randint(WINDOW_MIN, WINDOW_MAX)
        params = {WINDOW_PARAM: window}
    else:
        # A non-windowed target drops the window rather than carrying a stale ``n``: that
        # number would mean nothing, and it would split one expression into two fingerprints.
        params = {}
    return _replace(node, path, {"op": op, "args": list(target.get("args") or []), "params": params})


def _replace_subtree(node: dict | str, rng: random.Random,
                     allowed_fields: Sequence[str]) -> dict | str:
    path = rng.choice(list(_walk(node)))[0]
    return _replace(node, path, _fresh_subtree(rng, allowed_fields))


def _fresh_subtree(rng: random.Random, allowed_fields: Sequence[str]) -> dict | str:
    """A small fresh expression: a field, a const, or one operator over fresh leaves.

    Depth-2 by construction, so grafting it can only break the node, depth, or lookback
    budget of its host -- and ``normalize_expression`` is what decides that. Its leaves are
    drawn from ``allowed_fields``, so a run only ever proposes fields its own market carries.
    """
    roll = rng.random()
    if roll < 0.4:
        return rng.choice(allowed_fields)
    if roll < 0.5:
        return {"op": "const", "args": [], "params": {"value": rng.choice(_FRESH_CONSTANTS)}}
    op = rng.choice(_FRESH_OPERATORS)
    arity, windowed = OPERATOR_SPECS[op]
    params = {WINDOW_PARAM: rng.randint(WINDOW_MIN, WINDOW_MAX)} if windowed else {}
    return {"op": op, "args": [rng.choice(allowed_fields) for _ in range(arity)], "params": params}


def _accept(candidate: dict | str, parent: dict | str,
            allowed_fields: Sequence[str]) -> dict | str | None:
    """``candidate`` if the DSL accepts it and it is more than its parent re-spelled."""
    try:
        accepted = normalize_expression(candidate, allowed_fields=allowed_fields)
    except ExpressionError:
        return None
    if accepted.fingerprint == normalize_expression(parent, allowed_fields=allowed_fields).fingerprint:
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
