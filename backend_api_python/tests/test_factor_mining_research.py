"""Research identity must count parameter searches together and isolate data windows."""

import importlib
from dataclasses import replace

from app.services.factor_mining.engine import MiningConfig


def _config(**changes):
    return MiningConfig.from_payload({
        "sourceId": 3, "market": "USStock", "timeframe": "1d",
        "startDate": "2024-01-01", "endDate": "2024-10-31", **changes,
    }, default_commission=.0005, default_slippage=.0005)


def test_research_identity_counts_repeated_search_choices_together():
    research = importlib.import_module("app.services.factor_mining.research")
    config = _config()
    context = research.research_context(config)
    changed = replace(config, seed=99, holding_period=10, commission=.002,
                      search_mode="ai", budget=100)
    assert research.research_context(changed) == context
    assert research.context_hash(context) == research.context_hash(dict(reversed(list(context.items()))))


def test_research_identity_separates_source_window_and_split():
    research = importlib.import_module("app.services.factor_mining.research")
    context = research.research_context(_config())
    for overrides in ({"sourceId": 4}, {"startDate": "2024-02-01"},
                      {"freezeRatio": .25}, {"market": "Crypto"},
                      {"timeframe": "1h"}):
        assert research.research_context(_config(**overrides)) != context


def test_feedback_identity_matches_holding_period_and_universe_semantics():
    research = importlib.import_module("app.services.factor_mining.research")
    config = _config()
    manifest = {"universe": {"kind": "static", "instruments": [{"symbol": "AAPL"}]}}
    context = research.feedback_context(config, manifest)
    assert research.feedback_context(replace(config, holding_period=10), manifest) != context
    assert research.feedback_context(config, {"universe": {"kind": "dynamic", "reference": "SP500"}}) != context
    assert research.feedback_context(replace(config, seed=12), manifest) == context
