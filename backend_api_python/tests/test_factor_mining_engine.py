"""Search loop tests for factor mining.

Every data source is faked: no network call and no database runs here. The one real
dependency is ``FactorResearchEngine``, because the loop scores candidates through it.

Two properties of the shared fixture are load-bearing for what these tests can assert.

Its ``members`` carry the ``key`` handle that ``StrategyV2BacktestService.resolve_candidates``
always injects, which is what lets ``FactorResearchEngine._membership_panel`` resolve them and
run the point-in-time universe on every date. A member without one matches no symbol, reads as
inactive, and leaves every candidate unscorable -- a shape only a test double can produce.

Its prices are exact geometric curves with no idiosyncratic component, so every candidate
ranks the six symbols the same way and correlates with the ``reversal_5`` built-in almost
perfectly. The default redundancy bar rejects all twelve; only a permissive bar lets any of
them reach the candidate list. Several tests below therefore run with ``_PERMISSIVE`` and
assert the candidate list is non-empty, because a run that rejects everything cannot show
whether the search works.
"""

from datetime import datetime

import pandas as pd
import pytest

from app.services.factor_mining import engine as engine_module
from app.services.factor_mining.engine import (
    FactorMiningEngine,
    MiningConfig,
    PreparedPanel,
    _field_panels,
    _fold_ic_signs,
    _metrics_payload,
    _resolved_members,
    split_periods,
)
from app.services.factor_mining.gates import GateThresholds


class _FakeProvider:
    def __init__(self, frames, members):
        self.panel = type("Panel", (), {
            "frames": frames, "members": members, "fields": {},
            "snapshot_id": "b" * 64, "frequency": "1d", "warnings": [],
        })()

    def prepare(self, *, user_id, code, config):
        return self.panel


def _frames_and_members(periods=200):
    index = pd.date_range("2024-01-01", periods=periods, freq="B")
    frames, members = {}, []
    for offset, symbol in enumerate(["A", "B", "C", "D", "E", "F"]):
        drift = 0.002 * (offset + 1)
        close = [100 * (1 + drift) ** day for day in range(periods)]
        key = f"USStock:{symbol}"
        frames[key] = pd.DataFrame(
            {"open": close, "high": [value * 1.001 for value in close],
             "low": [value * 0.999 for value in close], "close": close,
             "volume": [1000.0 + day * (offset + 1) for day in range(periods)]}, index=index)
        # ``key`` is the handle the real ``resolve_candidates`` always injects and the one
        # ``_membership_panel`` matches on. Without it the panel reads every symbol as
        # inactive, the cross-section is empty, and the point-in-time universe never runs.
        members.append({"key": key, "market": "USStock", "symbol": symbol,
                        "effectiveFrom": None})
    return frames, members


def _config(**overrides):
    base = dict(market="USStock", timeframe="1d",
                start_date=datetime(2024, 1, 1), end_date=datetime(2024, 10, 31), source_id=1,
                budget=12, seed=3,
                thresholds=GateThresholds(min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
                                          min_fold_consistency=0.0, max_turnover=1.0,
                                          min_coverage=0.0, min_oos_abs_rank_ic=0.0,
                                          min_deflated_sharpe_probability=0.0))
    base.update(overrides)
    return MiningConfig(**base)


# The fixture ranks the symbols identically for every candidate, so L4 is the gate that
# decides this fixture's runs. A bar above 1.0 is what lets a candidate through on data whose
# only signal is the drift the built-ins already carry; every other gate is turned off.
_PERMISSIVE = GateThresholds(min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
                             min_fold_consistency=0.0, max_turnover=1.0, min_coverage=0.0,
                             max_builtin_correlation=2.0)


def _run(frames, members, **overrides):
    seen: list[dict] = []
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members), on_progress=seen.append)
    result = engine.run(user_id=1, code="PORTFOLIO = {}", config=_config(**overrides))
    return result, seen


# A search over this fixture costs seconds, and most tests below assert on one of a handful
# of them. Caching by object identity is safe because nothing in this module writes to the
# frames it hands in or to a result it gets back.
_FRAMES_CACHE: dict[tuple, tuple] = {}
_SEARCH_CACHE: dict[tuple, tuple] = {}


def _fixture(name="plain"):
    """The fixture frames and members, memoized so runs over them stay cacheable."""
    if name not in _FRAMES_CACHE:
        frames, members = _frames_and_members()
        if name == "frozen-tail-mutated":
            for frame in frames.values():
                frame.iloc[-40:] = frame.iloc[-40:] * 3.0
        _FRAMES_CACHE[name] = (frames, members)
    return _FRAMES_CACHE[name]


def _search(**overrides):
    """``_run`` over the plain fixture, memoized on the config overrides."""
    key = tuple(sorted((name, repr(value)) for name, value in overrides.items()))
    if key not in _SEARCH_CACHE:
        _SEARCH_CACHE[key] = _run(*_fixture(), **overrides)
    return _SEARCH_CACHE[key]


def _seeds(count):
    """``count`` distinct candidates over one expression, each with its own fingerprint.

    Distinct params are what separate the fingerprints: the DSL folds ``params`` into the
    canonical spelling, and the dedup key is the canonical form of the whole node.
    """
    from app.services.factor_mining.dsl import normalize_expression
    from app.services.factor_mining.templates import CandidateSeed

    seeds = []
    for index in range(count):
        expression = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 3, "N": index}}
        seeds.append(CandidateSeed(expression=expression,
                                   fingerprint=normalize_expression(expression).fingerprint,
                                   template_id="momentum", origin="template",
                                   params={"N": index}))
    return seeds


def _run_with_seeds(monkeypatch, seeds, **overrides):
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: seeds)
    return _run(*_fixture(), **overrides)


def test_search_records_one_trial_per_evaluated_candidate():
    """Run at the permissive bar, because the default one rejects all twelve on this fixture.

    Every candidate here tracks ``reversal_5`` almost perfectly, so the default redundancy bar
    rejects the whole run. That is a correct verdict on this data, but it would leave the
    assertions below comparing nothing, hence ``_PERMISSIVE`` plus an explicit check that
    something did clear the ladder.
    """
    result, _ = _search(thresholds=_PERMISSIVE)
    assert len(result["trials"]) == len({item["fingerprint"] for item in result["trials"]})
    assert len(result["trials"]) >= 1
    assert all(item["stage"] in {"L1", "L2", "L3", "L4", "L5"} for item in result["trials"])
    # A run where every candidate dies before the ladder is scored proves nothing about the
    # search, so at least one trial has to clear every gate with its metrics intact.
    passed = [item for item in result["trials"] if item["verdict"] == "pass"]
    assert passed, "no candidate cleared the ladder"
    for key in ("rankIc", "icir", "averageTurnover"):
        assert key in passed[0]["metrics"], key


def test_the_gate_ladder_actually_runs_on_this_fixture():
    """L4 must run on a real correlation measurement, and a candidate must clear it.

    The loop reads the candidate's measured correlations out of the correlation study, so a
    study that came back unreadable would leave L4 rejecting everything as unverifiable and
    the search would never produce a candidate -- green, but proving nothing.
    """
    result, _ = _search(thresholds=_PERMISSIVE)
    assert any(item["stage"] == "L4" for item in result["trials"]), \
        "no trial reached the redundancy gate"
    assert all(item["reason"] != "factorMining.reject.unverifiableRedundancy"
               for item in result["trials"]), "L4 was handed no measurement at all"
    assert result["candidates"], "no candidate cleared the ladder"


def test_rejected_candidates_still_record_trials():
    frames, members = _fixture()
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members))
    result = engine.run(user_id=1, code="PORTFOLIO = {}",
                        config=_config(thresholds=GateThresholds(min_abs_rank_ic=0.99)))
    assert result["candidates"] == []
    assert result["trials"] and all(item["verdict"] == "reject" for item in result["trials"])
    assert result["trials"][0]["reason"].startswith("factorMining.reject.")


def test_a_frozen_tail_that_cannot_be_seen_cannot_change_the_search():
    """The search scores the search window, so the frozen tail cannot reach its metrics.

    Both runs use one config -- same range, same split, same search window -- and differ only
    in the bars after ``split.search_end``. Identical candidates and metrics mean the tail was
    neither scored nor able to leak into scoring, which is what keeps the out-of-sample review
    honest. Both candidate lists are asserted non-empty: comparing two empty lists would hold
    no matter what the loop did.
    """
    first, _ = _search(thresholds=_PERMISSIVE)
    second, _ = _run(*_fixture("frozen-tail-mutated"), thresholds=_PERMISSIVE)
    assert first["trials"] and second["trials"], "the fixture must evaluate candidates"
    assert first["candidates"] and second["candidates"], "no candidate survived to compare"
    assert [item["fingerprint"] for item in first["candidates"]] == \
           [item["fingerprint"] for item in second["candidates"]]
    assert [item["metrics"]["rankIc"] for item in first["candidates"]] == \
           [item["metrics"]["rankIc"] for item in second["candidates"]]
    assert [item["metrics"]["averageTurnover"] for item in first["candidates"]] == \
           [item["metrics"]["averageTurnover"] for item in second["candidates"]]


def test_the_search_window_is_cut_from_the_panel_that_carries_warmup():
    """A candidate is scored over the search window, not over the fetched warmup too.

    ``PreparedPanel`` carries no split of its own and its frames start before the search
    window, so the loop hands ``split_periods`` to the research engine on every call. Running
    the research directly over that window must reproduce the trial's metrics exactly.
    """
    frames, members = _fixture()
    result, _ = _search(thresholds=_PERMISSIVE)
    trial = result["candidates"][0]
    config = _config(thresholds=_PERMISSIVE)
    panel = PreparedPanel(frames=frames, members=members, fields={}, snapshot_id="b" * 64,
                          frequency="1d", warnings=[])
    candidate_panel = _evaluator(frames, trial["expression"])
    direct = _metrics_payload(
        FactorMiningEngine(data_provider=_FakeProvider(frames, members))._research(
            config, split_periods(config), panel, _resolved_members(members), candidate_panel),
        config,
    )
    assert direct == trial["metrics"]
    # The window really is narrower than the fetched panel: the warmup is not scored.
    assert len(frames["USStock:A"]) > len(direct["icSeries"]) * config.holding_period


def _evaluator(frames, expression):
    from app.services.factor_mining.evaluator import ExpressionEvaluator

    return ExpressionEvaluator(_field_panels(frames)).evaluate(expression)


def test_metrics_carry_execution_assumptions():
    result, _ = _search(commission=0.001, slippage=0.002)
    assert result["summary"]["executionAssumptions"]["commission"] == 0.001
    assert result["summary"]["executionAssumptions"]["slippage"] == 0.002


def test_trials_repeat_the_costs_the_run_passed_in():
    """The ledger's own execution assumptions, not the research engine's defaults."""
    result, _ = _search(commission=0.001, slippage=0.002)
    assert result["trials"]
    for item in result["trials"]:
        assert item["metrics"]["executionAssumptions"]["commission"] == 0.001
        assert item["metrics"]["executionAssumptions"]["slippage"] == 0.002


def test_run_is_reproducible_with_same_seed():
    first, _ = _search(seed=11)
    second, _ = _search(seed=11)
    assert [item["fingerprint"] for item in first["trials"]] == [item["fingerprint"] for item in second["trials"]]


def test_run_is_reproducible_on_the_candidate_sequence_too():
    first, _ = _search(seed=11, thresholds=_PERMISSIVE)
    second, _ = _search(seed=11, thresholds=_PERMISSIVE)
    assert first["candidates"] and second["candidates"]
    assert [item["fingerprint"] for item in first["candidates"]] == \
           [item["fingerprint"] for item in second["candidates"]]


def test_trials_carry_the_ladder_verdict_shape():
    result, _ = _search(thresholds=_PERMISSIVE)
    assert result["candidates"], "a permissive fixture must clear the ladder"
    assert len(result["trials"]) == 12
    for item in result["trials"]:
        assert set(item) == {"fingerprint", "stage", "verdict", "reason", "metrics"}
        assert item["verdict"] in {"pass", "reject"}
        assert item["stage"] in {"L1", "L2", "L3", "L4", "L5"}
        assert (item["reason"] == "") == (item["verdict"] == "pass")
        if item["verdict"] == "reject":
            assert item["reason"].startswith("factorMining.reject.")


def test_candidates_carry_the_search_records_shape():
    result, _ = _search(thresholds=_PERMISSIVE)
    assert result["candidates"]
    for item in result["candidates"]:
        assert set(item) == {"expression", "fingerprint", "origin", "template_id", "status",
                             "reason", "metrics", "outOfSample"}
        assert item["origin"] == "template"
        assert item["template_id"]
        assert item["status"] == "search"
        assert item["reason"] == ""
        assert item["outOfSample"] == {}
        assert isinstance(item["expression"], dict)
        assert item["fingerprint"]
        for key in ("coverage", "icSeries", "rankIc", "icir", "averageTurnover", "stability",
                    "foldIcSigns"):
            assert key in item["metrics"], key


def test_gates_get_fold_ic_signs_from_the_ic_series():
    """L3's fold check reads ``foldIcSigns``; a run that omits it loses that check silently."""
    result, _ = _search(thresholds=_PERMISSIVE)
    signs = result["candidates"][0]["metrics"]["foldIcSigns"]
    assert len(signs) == 4, "the IC series is cut into four folds"
    assert all(sign in {-1.0, 0.0, 1.0} for sign in signs)


def test_fold_signs_are_the_four_equal_folds_of_the_series():
    series = [{"value": value} for value in (0.5, 0.5, -0.5, -0.5, 0.5, 0.5, 0.5, 0.5)]
    assert _fold_ic_signs(series) == [1.0, -1.0, 1.0, 1.0]


def test_fold_signs_of_an_absent_series_are_empty():
    assert _fold_ic_signs(None) == []
    assert _fold_ic_signs([]) == []


def test_redundancy_gate_rejects_a_builtin_duplicate(monkeypatch):
    """L4 is reached and handed real correlations, and rejects a duplicate as one.

    The fixture's candidates track the ``reversal_5`` built-in at a correlation of 1.0, so a
    bar below that rejects them as duplicates. A gate handed no measurement at all reports
    ``unverifiableRedundancy`` instead, which this pins apart -- a search that never actually
    compared a candidate against the built-in pool would look identical otherwise.
    """
    from app.services.factor_mining.templates import CandidateSeed

    baseline, _ = _search(thresholds=_PERMISSIVE)
    duplicate = baseline["candidates"][0]
    assert duplicate, "the fixture must have a candidate to re-run the redundancy gate on"
    seeds = [CandidateSeed(expression=duplicate["expression"],
                           fingerprint=duplicate["fingerprint"],
                           template_id=duplicate["template_id"], origin="template",
                           params={"N": 1})]
    result, _ = _run_with_seeds(monkeypatch, seeds, thresholds=GateThresholds(
        min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0, min_fold_consistency=0.0,
        max_turnover=1.0, min_coverage=0.0, max_builtin_correlation=0.5))
    assert [item["verdict"] for item in result["trials"]] == ["reject"]
    assert result["trials"][0]["stage"] == "L4"
    assert result["trials"][0]["reason"] == "factorMining.reject.duplicateBuiltin"
    assert result["candidates"] == []


def test_members_without_a_key_are_reported_as_no_membership():
    """Members the research engine cannot map to a symbol must not blank the cross-section.

    ``_membership_panel`` matches on ``member["key"]``; a member that has a ``symbol`` but no
    ``key`` is an entry that matches nothing, which reads as inactive on every date and turns
    every candidate into an inconclusive rejection.
    """
    assert _resolved_members([{"market": "USStock", "symbol": "A", "effectiveFrom": None}]) == ()
    keyed = [{"key": "USStock:A", "market": "USStock", "symbol": "A"}]
    assert _resolved_members(keyed) == keyed


def test_progress_is_reported_every_ten_candidates():
    _, seen = _search()
    assert seen == [{"stage": "search", "evaluated": 10, "total": 12}]


def test_progress_reports_one_line_per_ten_candidates(monkeypatch):
    """Eleven candidates is one full page plus one, so the second page is exercised."""
    _, seen = _run_with_seeds(monkeypatch, _seeds(11))
    assert seen == [{"stage": "search", "evaluated": 10, "total": 11}]


# One case per way a candidate can die outside ``ExpressionError``. Each must become a
# recorded ``factorMining.reject.invalid`` trial rather than an escape from ``run``. The
# windowed and cross-sectional cases stand for all fourteen operators that take a panel and
# receive a scalar: ``lag``, ``delta``, the eight ``ts_*`` windows, ``cs_rank``, ``cs_zscore``
# and ``cs_demean`` each raise a bare ``AttributeError`` on a scalar operand.
_DEGENERATE_EXPRESSIONS = {
    "scalar division by zero": {"op": "div", "args": [
        {"op": "const", "args": [], "params": {"value": 1}},
        {"op": "const", "args": [], "params": {"value": 0}},
    ], "params": {}},
    "windowed operator on a scalar": {"op": "ts_mean", "args": [
        {"op": "const", "args": [], "params": {"value": 1}},
    ], "params": {"n": 3}},
    "cross_sectional operator on a scalar": {"op": "cs_rank", "args": [
        {"op": "const", "args": [], "params": {"value": 1}},
    ], "params": {}},
    "scalar in where's true branch": {"op": "where", "args": [
        {"op": "gt", "args": [
            {"op": "ts_mean", "args": ["close"], "params": {"n": 3}},
            {"op": "const", "args": [], "params": {"value": 0}},
        ], "params": {}},
        {"op": "const", "args": [], "params": {"value": 1}},
        "close",
    ], "params": {}},
}


def _seed_for(expression):
    from app.services.factor_mining.dsl import normalize_expression
    from app.services.factor_mining.templates import CandidateSeed

    return CandidateSeed(expression=expression,
                         fingerprint=normalize_expression(expression).fingerprint,
                         template_id="momentum", origin="template", params={"N": 1})


@pytest.mark.parametrize("name", sorted(_DEGENERATE_EXPRESSIONS))
def test_degenerate_candidate_expression_is_an_invalid_rejection(monkeypatch, name):
    result, _ = _run_with_seeds(monkeypatch, [_seed_for(_DEGENERATE_EXPRESSIONS[name])])
    assert [item["verdict"] for item in result["trials"]] == ["reject"]
    assert result["trials"][0]["stage"] == "L1"
    assert result["trials"][0]["reason"] == "factorMining.reject.invalid"
    assert result["candidates"] == []


def test_oversized_integer_constant_is_an_invalid_rejection(monkeypatch):
    """A ``const`` whose ``float()`` overflows raises ``OverflowError``, not ``ExpressionError``.

    The literal has to reach the operator layer to overflow: ``dsl._is_finite_number`` accepts
    any integral ``value`` by design, and the conversion happens in ``operators._const``.
    """
    result, _ = _run_with_seeds(
        monkeypatch, [_seed_for({"op": "const", "args": [], "params": {"value": 10 ** 400}})])
    assert [item["verdict"] for item in result["trials"]] == ["reject"]
    assert result["trials"][0]["reason"] == "factorMining.reject.invalid"
    assert result["candidates"] == []


def test_a_failing_candidate_does_not_stop_the_candidates_after_it(monkeypatch):
    zero_division = {"op": "div", "args": [
        {"op": "const", "args": [], "params": {"value": 1}},
        {"op": "const", "args": [], "params": {"value": 0}},
    ], "params": {}}
    healthy = _seed_for({"op": "ts_pct_change", "args": ["close"], "params": {"n": 3}})
    result, _ = _run_with_seeds(monkeypatch, [_seed_for(zero_division), healthy],
                                thresholds=_PERMISSIVE)
    assert [item["verdict"] for item in result["trials"]] == ["reject", "pass"]
    assert result["trials"][0]["reason"] == "factorMining.reject.invalid"
    assert result["trials"][1]["reason"] == ""


def test_run_reports_the_config_split_and_summary():
    result, _ = _search()
    assert result["config"]["market"] == "USStock"
    assert result["config"]["budget"] == 12
    assert datetime.fromisoformat(result["split"]["searchStart"]) == datetime(2024, 1, 1)
    assert datetime.fromisoformat(result["split"]["searchStart"]) < \
           datetime.fromisoformat(result["split"]["searchEnd"])
    assert datetime.fromisoformat(result["split"]["freezeStart"]) > \
           datetime.fromisoformat(result["split"]["searchEnd"])
    assert datetime.fromisoformat(result["split"]["freezeEnd"]) == datetime(2024, 10, 31)
    assert result["summary"]["snapshotId"] == "b" * 64
    assert result["summary"]["trials"] == len(result["trials"])
    assert result["summary"]["candidates"] == len(result["candidates"])
