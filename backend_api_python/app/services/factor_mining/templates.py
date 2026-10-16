"""Financial hypothesis template library and budgeted, deterministic candidate expansion.

A template pairs a hypothesis with a parameterized expression skeleton and an integer
parameter grid; expanding a grid point yields its base expression plus "denoised" variants.
Expansion is byte-for-byte reproducible for a given ``(template_ids, budget, seed)``. This
is a pure computation layer -- no clocks, database, or network access.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass

from app.services.factor_mining.dsl import PRICE_VOLUME_FIELDS, normalize_expression

MAX_BUDGET = 2000
DEFAULT_BUDGET = 400
SMOOTHING_WINDOWS = (3, 5)


@dataclass(frozen=True)
class FactorTemplate:
    template_id: str
    hypothesis_i18n_key: str
    category: str
    parameter_grid: Mapping[str, tuple[int, ...]]
    build: Callable[[Mapping[str, int]], dict]


@dataclass(frozen=True)
class CandidateSeed:
    expression: dict
    fingerprint: str
    template_id: str
    origin: str
    params: dict


def _windowed(op: str, window: int, *args: object) -> dict:
    """Windowed operator node. The window parameter is always spelled ``n``."""
    return {"op": op, "args": list(args), "params": {"n": window}}


def _const(value: float) -> dict:
    return {"op": "const", "args": [], "params": {"value": value}}


def _momentum(params: Mapping[str, int]) -> dict:
    return _windowed("ts_pct_change", params["N"], "close")


def _reversal(params: Mapping[str, int]) -> dict:
    return {"op": "neg", "args": [_windowed("ts_pct_change", params["N"], "close")], "params": {}}


def _low_volatility(params: Mapping[str, int]) -> dict:
    # Volatility of one-bar returns, negated so calm names rank high.
    one_bar_return = _windowed("ts_pct_change", 1, "close")
    return {"op": "neg", "args": [_windowed("ts_std", params["N"], one_bar_return)], "params": {}}


def _liquidity(params: Mapping[str, int]) -> dict:
    """Volume change: recent average volume against a longer baseline."""
    return {"op": "safe_div", "args": [
        _windowed("ts_mean", params["N"], "volume"),
        _windowed("ts_mean", params["M"], "volume"),
    ], "params": {}}


def _liquidity_level(params: Mapping[str, int]) -> dict:
    """Volume level: recent average volume, ranked across symbols."""
    return {"op": "cs_rank", "args": [_windowed("ts_mean", params["N"], "volume")], "params": {}}


def _volume_price_confirm(params: Mapping[str, int]) -> dict:
    momentum_rank = {"op": "cs_rank", "args": [_windowed("ts_pct_change", params["N"], "close")], "params": {}}
    relative_volume = {"op": "safe_div", "args": ["volume", _windowed("ts_mean", params["M"], "volume")], "params": {}}
    volume_rank = {"op": "cs_rank", "args": [relative_volume], "params": {}}
    return {"op": "mul", "args": [momentum_rank, volume_rank], "params": {}}


def _risk_adjusted_momentum(params: Mapping[str, int]) -> dict:
    volatility = _windowed("ts_std", params["N"], _windowed("ts_pct_change", 1, "close"))
    return {"op": "safe_div", "args": [_windowed("ts_pct_change", params["N"], "close"), volatility], "params": {}}


def _conditional_filter(params: Mapping[str, int]) -> dict:
    """Momentum, but only while recent volume sits in the top of its yearly window.

    The fallback is a ``const`` node -- a literal number is not a legal leaf. It must sit
    in the last slot: ``where`` calls ``when_true.where(...)``, so a scalar truth branch
    would not be a panel.
    """
    volume_regime = {"op": "gt", "args": [
        _windowed("ts_rank", 120, _windowed("ts_mean", params["M"], "volume")),
        _const(0.8),
    ], "params": {}}
    return {"op": "where", "args": [
        volume_regime,
        _windowed("ts_pct_change", params["N"], "close"),
        _const(0),
    ], "params": {}}


# Category groups templates by the data family their hypothesis leans on.
TEMPLATES: dict[str, FactorTemplate] = {
    "momentum": FactorTemplate(
        template_id="momentum",
        hypothesis_i18n_key="factorMining.template.momentum",
        category="price",
        parameter_grid={"N": (5, 10, 20, 60, 120)},
        build=_momentum,
    ),
    "reversal": FactorTemplate(
        template_id="reversal",
        hypothesis_i18n_key="factorMining.template.reversal",
        category="price",
        parameter_grid={"N": (3, 5, 10, 20)},
        build=_reversal,
    ),
    "low_volatility": FactorTemplate(
        template_id="low_volatility",
        hypothesis_i18n_key="factorMining.template.low_volatility",
        category="price",
        parameter_grid={"N": (10, 20, 60)},
        build=_low_volatility,
    ),
    "liquidity": FactorTemplate(
        template_id="liquidity",
        hypothesis_i18n_key="factorMining.template.liquidity",
        category="volume",
        parameter_grid={"N": (5, 10, 20), "M": (20, 60)},
        build=_liquidity,
    ),
    "liquidity_level": FactorTemplate(
        template_id="liquidity_level",
        hypothesis_i18n_key="factorMining.template.liquidity_level",
        category="volume",
        parameter_grid={"N": (5, 10, 20)},
        build=_liquidity_level,
    ),
    "volume_price_confirm": FactorTemplate(
        template_id="volume_price_confirm",
        hypothesis_i18n_key="factorMining.template.volume_price_confirm",
        category="price_volume",
        parameter_grid={"N": (5, 20, 60), "M": (5, 20)},
        build=_volume_price_confirm,
    ),
    "risk_adjusted_momentum": FactorTemplate(
        template_id="risk_adjusted_momentum",
        hypothesis_i18n_key="factorMining.template.risk_adjusted_momentum",
        category="price",
        parameter_grid={"N": (20, 60, 120)},
        build=_risk_adjusted_momentum,
    ),
    "conditional_filter": FactorTemplate(
        template_id="conditional_filter",
        hypothesis_i18n_key="factorMining.template.conditional_filter",
        category="price_volume",
        parameter_grid={"N": (5, 20), "M": (5, 20)},
        build=_conditional_filter,
    ),
}


def template_catalog() -> list[dict]:
    """Template metadata for the HTTP catalog. ``build`` callables never leave this module."""
    return [
        {
            "templateId": template.template_id,
            "hypothesisI18nKey": template.hypothesis_i18n_key,
            "category": template.category,
            "parameterGrid": {name: list(values) for name, values in template.parameter_grid.items()},
        }
        for template in TEMPLATES.values()
    ]


def _grid_points(grid: Mapping[str, tuple[int, ...]]) -> Iterator[dict[str, int]]:
    """Cartesian product of a parameter grid, axes in declaration order."""
    names = list(grid)
    for values in itertools.product(*(grid[name] for name in names)):
        yield dict(zip(names, values))


def _smoothed(node: dict, window: int) -> dict:
    return _windowed("ts_mean", window, node)


def _truncate(seeds: list[CandidateSeed], budget: int, seed: int) -> list[CandidateSeed]:
    """Reservoir-truncate to ``budget`` rows, mirroring ``ParameterSampler.grid``.

    The duplication is deliberate. That sampler cannot be reused here: it samples a
    ``SearchParameter`` space, not a deduped candidate list, and importing it executes
    ``strategy_evolution/__init__.py``, which pulls pandas and psycopg2 into this pure
    computation layer. Its reservoir loop is copied line for line -- keep the first
    ``budget`` rows, let each later row replace a random slot, then restore row order.
    """
    if len(seeds) <= budget:
        return seeds
    random_source = random.Random(seed)
    kept = list(range(budget))
    for index in range(budget, len(seeds)):
        slot = random_source.randint(0, index)
        if slot < budget:
            kept[slot] = index
    return [seeds[index] for index in sorted(kept)]


def expand_candidates(
    *,
    template_ids: Sequence[str] | None = None,
    budget: int = DEFAULT_BUDGET,
    seed: int = 42,
    smoothing: bool = True,
    allowed_fields: Collection[str] = PRICE_VOLUME_FIELDS,
) -> list[CandidateSeed]:
    """Expand templates into at most ``budget`` distinct candidates, in a stable order.

    Templates run in :data:`TEMPLATES` order and every grid point contributes its base
    expression plus, when ``smoothing``, one ``ts_mean(expr, k)`` variant per entry of
    :data:`SMOOTHING_WINDOWS`. Candidates are deduped by expression fingerprint (first
    spelling wins) before truncation, so a truncated run is reproducible. An unknown
    template id and a budget above :data:`MAX_BUDGET` both raise ``ValueError`` carrying a
    stable ``factorMining.*`` key, so callers never translate a bare ``KeyError``.

    ``allowed_fields`` is the field set the expansion fingerprints each candidate against --
    ``fields_for(market)`` for a run. It is a parameter rather than a module constant because
    which fields a market can offer is the caller's run to state.
    """
    if budget > MAX_BUDGET:
        raise ValueError("factorMining.budgetExceeded")
    seeds: list[CandidateSeed] = []
    seen: set[str] = set()
    for template_id in list(TEMPLATES) if template_ids is None else list(template_ids):
        template = TEMPLATES.get(template_id)
        if template is None:
            raise ValueError(f"factorMining.templateNotFound:{template_id}")
        for params in _grid_points(template.parameter_grid):
            base = template.build(params)
            variants = [base]
            if smoothing:
                variants.extend(_smoothed(base, window) for window in SMOOTHING_WINDOWS)
            for node in variants:
                fingerprint = normalize_expression(node, allowed_fields=allowed_fields).fingerprint
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                seeds.append(CandidateSeed(
                    expression=node,
                    fingerprint=fingerprint,
                    template_id=template_id,
                    origin="template",
                    params=params,
                ))
    return _truncate(seeds, budget, seed)
