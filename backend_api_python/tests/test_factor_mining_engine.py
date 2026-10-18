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
them reach the candidate list. Several tests below therefore run with ``_NO_DEDUPLICATION`` and
assert the candidate list is non-empty, because a run that rejects everything cannot show
whether the search works.
"""

import json
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest

from app.services.factor_mining import engine as engine_module
from app.services.factor_mining import pool
from app.services.factor_mining.dsl import MAX_LOOKBACK, fields_for
from app.services.factor_mining.engine import (
    DEFAULT_BUILTIN_COMPARISON_LIMIT,
    SEARCH_MODE_AI,
    AISearchOptions,
    ComparisonSet,
    FactorMiningEngine,
    MiningConfig,
    MiningDataProvider,
    PreparedPanel,
    _comparison_set,
    _field_panels,
    _fold_ic_signs,
    _metrics_payload,
    _resolved_members,
    _warmup_bars,
    split_periods,
)
from app.services.factor_mining.evaluator import ExpressionEvaluator
from app.services.factor_mining.gates import GateThresholds
from app.services.factor_mining.templates import CandidateSeed, expand_candidates
from app.services.strategy_v2.factor_research import FactorResearchEngine


class _FakeProvider:
    """A stand-in for ``MiningDataProvider`` that shapes its panel the way the real one does.

    ``fields`` is built by the same ``_field_panels`` the provider uses rather than left empty:
    the fake stands in for the provider, so a field mapping it never filled would hide a real
    provider bug behind a candidate panel of nothing. ``search_modes`` records what the engine
    asked for, which is the only way to see that the warmup rule traveled with the run.
    """

    def __init__(self, frames, members):
        self.panel = PreparedPanel(frames=frames, members=members, fields=_field_panels(frames),
                                   snapshot_id="b" * 64, frequency="1d", warnings=[])
        self.search_modes: list[str | None] = []
        # What this run compares its candidates against. Empty by default: the fake stands in
        # for a provider that found no registry factor and no pooled candidate to measure, which
        # is a real run shape and the one every pre-existing test in this module was written for.
        self.comparison = ComparisonSet()

    def prepare(self, *, user_id, code, config, search_mode=None):
        self.search_modes.append(search_mode)
        return self.panel

    def prepare_freeze(self, *, user_id, code, config, search_mode=None):
        # The real provider fetches the frozen tail on this call. The fake holds one panel and
        # serves it for both, so a test that needs a disagreeing tail hands in different frames.
        self.search_modes.append(search_mode)
        return self.panel

    def comparison_set(self, *, panel, config, user_id, start_date, end_date):
        return self.comparison


def _frames_and_members(periods=200):
    """One frame per symbol plus the members a real ``resolve_candidates`` would return.

    ``key`` is the handle ``_membership_panel`` matches on. ``membership_periods`` is the
    point-in-time part: ``USStock:F`` only joins the universe from May, so a search that
    really applies the membership panel sees a five-symbol cross-section before that and a
    six-symbol one after. Without periods every member is active on every date and the
    universe filter is never exercised.
    """
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
        member = {"key": key, "market": "USStock", "symbol": symbol, "effectiveFrom": None}
        if symbol == "F":
            member["membership_periods"] = [
                {"valid_from": datetime(2024, 5, 1), "valid_to": None},
            ]
        members.append(member)
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
# decides this fixture's runs. A bar of 1.1 is above every possible correlation, so the
# de-duplication gate can never fire and the run has a genuine end-to-end pass path; every
# other gate is turned off for the same reason. Both L4 bars are raised: the fixture's pool
# panels are the candidates' own twins, so a run that compares against one is a duplicate of it.
_NO_DEDUPLICATION = GateThresholds(min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
                                   min_fold_consistency=0.0, max_turnover=1.0, min_coverage=0.0,
                                   max_builtin_correlation=1.1, max_pool_correlation=1.1)


def _run(frames, members, **overrides):
    """One run over ``frames``. ``search_mode`` picks the engine's source; the rest is config."""
    search_mode = overrides.pop("search_mode", "enumeration")
    seen: list[dict] = []
    engine = FactorMiningEngine(data_provider=_FakeProvider(frames, members), on_progress=seen.append)
    result = engine.run(user_id=1, code="PORTFOLIO = {}", config=_config(**overrides),
                        search_mode=search_mode)
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
    """``_run`` over the plain fixture, memoized on the config overrides.

    A run is memoized only while the real template expansion is in place. A test that patches
    ``expand_candidates`` and calls this would otherwise store its result under the key the
    template search owns, and every later test asking for that key -- in a subset run, where
    nothing repopulated it first -- would read that run instead of a template search.
    """
    key = tuple(sorted((name, repr(value)) for name, value in overrides.items()))
    if engine_module.expand_candidates is not expand_candidates:
        return _run(*_fixture(), **overrides)
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


@contextmanager
def _patched_expansion(monkeypatch, seeds):
    """Expand ``seeds`` instead of the templates for the duration of the block.

    Entries the block added are dropped on the way out -- in ``finally``, so an aborted block
    cannot leave one behind either. Only those entries go, never the whole memo: the cross-test
    reuse is what keeps this suite's runtime down, and a template search memoized by an earlier
    test is still valid. ``_search`` already refuses to memoize a run made under a patch, so with
    that check in place this drops nothing; it is the second lock on the same door, and it is
    what keeps the memo honest if that check is ever changed.
    """
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: seeds)
    memoized = set(_SEARCH_CACHE)
    try:
        yield
    finally:
        for key in set(_SEARCH_CACHE) - memoized:
            del _SEARCH_CACHE[key]


def _run_with_seeds(monkeypatch, seeds, **overrides):
    with _patched_expansion(monkeypatch, seeds):
        return _run(*_fixture(), **overrides)


def test_search_records_one_trial_per_evaluated_candidate():
    result, _ = _search()
    assert len(result["trials"]) == len({item["fingerprint"] for item in result["trials"]})
    assert len(result["trials"]) >= 1
    assert all(item["stage"] in {"L1", "L2", "L3", "L4", "L5"} for item in result["trials"])


def test_a_ledger_row_keeps_only_the_metrics_a_gate_reads():
    """The trial ledger is a run's size bound: one row per evaluation, so no row carries a curve.

    ``icSeries`` is kept as its length under ``icObservations`` -- L2 counts the observations and
    never reads their values, while the series itself is what makes one row 0.36 MB at 40 symbols.
    The candidate keeps the whole measurement, and that is where the detail view reads it.
    """
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
    assert result["trials"] and result["candidates"]
    heaviest = {"icSeries", "groupCurves", "longShortCurve", "groupObservations",
                "factorCorrelation"}
    for item in result["trials"]:
        assert not (set(item["metrics"]) & heaviest), "a ledger row dragged a curve along"
    for item in result["candidates"]:
        assert heaviest <= set(item["metrics"]), "the candidate lost the measurement"
    # The count a ledger row keeps is the length of the series the candidate holds.
    series_length = {item["fingerprint"]: len(item["metrics"]["icSeries"])
                     for item in result["candidates"]}
    assert series_length, "no candidate to compare against"
    for item in result["trials"]:
        if item["fingerprint"] in series_length:
            assert item["metrics"]["icObservations"] == series_length[item["fingerprint"]]


def test_the_pipeline_runs_past_the_observation_gate_on_real_correlations():
    """L3 is reached, and L4 rejects the fixture's candidates as duplicates, not as unverifiable.

    Together those say the run really walked the ladder on a measurement. A run that stalled at
    L2 would mean the cross-section never resolved; a run whose L4 rejected everything as
    ``unverifiableRedundancy`` would mean the correlation study came back unreadable and the
    de-duplication check never actually compared anything.
    """
    result, _ = _search()
    assert any(item["stage"] == "L3" for item in result["trials"]), \
        "every candidate stopped at or before the observation gate"
    l4 = [item for item in result["trials"] if item["stage"] == "L4"]
    assert l4, "no trial reached the redundancy gate"
    assert {item["reason"] for item in l4} == {"factorMining.reject.duplicateBuiltin"}


def test_a_run_with_no_deduplication_produces_candidates():
    """The end-to-end pass path, with the de-duplication gate unable to fire at all.

    This is the only configuration in which this fixture can produce a survivor: every
    candidate here tracks ``reversal_5`` almost perfectly, so the default bar rejects all
    twelve as duplicates -- a correct verdict, but one that would leave the search's pass path
    untested.
    """
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
    assert result["candidates"], "no candidate cleared the ladder"
    assert any(item["verdict"] == "pass" for item in result["trials"])
    for key in ("rankIc", "icir", "averageTurnover", "foldIcSigns"):
        assert key in result["candidates"][0]["metrics"], key


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
    first, _ = _search(thresholds=_NO_DEDUPLICATION)
    second, _ = _run(*_fixture("frozen-tail-mutated"), thresholds=_NO_DEDUPLICATION)
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
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
    trial = result["candidates"][0]
    config = _config(thresholds=_NO_DEDUPLICATION)
    panel = PreparedPanel(frames=frames, members=members, fields={}, snapshot_id="b" * 64,
                          frequency="1d", warnings=[])
    candidate_panel = _evaluator(frames, trial["expression"])
    direct = _metrics_payload(
        FactorMiningEngine(data_provider=_FakeProvider(frames, members))._research(
            config, split_periods(config), panel, _resolved_members(members), candidate_panel,
            ComparisonSet()),
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


def test_trials_declare_the_measurement_they_are_reading():
    """The engine's own execution assumptions have to survive into the ledger row.

    They are spread under this run's costs rather than replaced by them, and they are the only
    place a stored trial says what its numbers mean -- which point-in-time price the signal is
    read from, which bar the trade is entered on, and which bar it exits on. A payload that
    carried the costs alone would still pass every other assertion in this suite.
    """
    result, _ = _search()
    assert result["trials"]
    assumptions = result["trials"][0]["metrics"]["executionAssumptions"]
    assert assumptions["signal"] == "close_point_in_time"
    assert assumptions["entry"] == "next_bar_open"
    assert assumptions["exit"] == "open_after_5_bars"
    assert assumptions["commission"] == 0.0005
    assert assumptions["slippage"] == 0.0005


def test_run_is_reproducible_trial_sequence_including_order():
    """Two independent expansions produce the same trial sequence, position by position.

    Comparing fingerprints as sets, or sorted, cannot see an ordering difference -- which is
    the half of determinism that matters, because the trial ledger's order is the expansion
    order the budget truncated. The final assertion fails if the sequence is ever a constant.

    The second run goes through ``_run`` rather than ``_search``: ``_search`` is memoized, so
    asking it twice hands back one and the same record and every equality below would hold for
    free. The identity assertion keeps that trap from coming back.
    """
    first, _ = _search(seed=11)
    second, _ = _run(*_fixture(), seed=11)
    assert first is not second, "a memoized second run would compare a record with itself"
    first_trials = [(item["fingerprint"], item["stage"], item["verdict"], item["reason"])
                    for item in first["trials"]]
    second_trials = [(item["fingerprint"], item["stage"], item["verdict"], item["reason"])
                     for item in second["trials"]]
    assert first_trials == second_trials
    assert len(set(first_trials)) > 1, "a single repeated trial would satisfy any equality"


def test_run_is_reproducible_candidate_sequence_including_order():
    """The candidate sequence, likewise over two records rather than one memoized record."""
    first, _ = _search(seed=11, thresholds=_NO_DEDUPLICATION)
    second, _ = _run(*_fixture(), seed=11, thresholds=_NO_DEDUPLICATION)
    assert first is not second, "a memoized second run would compare a record with itself"
    first_candidates = [item["fingerprint"] for item in first["candidates"]]
    second_candidates = [item["fingerprint"] for item in second["candidates"]]
    assert first_candidates == second_candidates
    assert len(set(first_candidates)) > 1


def test_trials_carry_the_ladder_verdict_shape():
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
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
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
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
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
    signs = result["candidates"][0]["metrics"]["foldIcSigns"]
    assert len(signs) == 4, "the IC series is cut into four folds"
    assert all(sign in {-1.0, 0.0, 1.0} for sign in signs)


def test_fold_signs_are_the_four_equal_folds_of_the_series():
    series = [{"value": value} for value in (0.5, 0.5, -0.5, -0.5, 0.5, 0.5, 0.5, 0.5)]
    assert _fold_ic_signs(series) == [1.0, -1.0, 1.0, 1.0]


def test_fold_signs_of_an_absent_series_raise_rather_than_skip_the_check():
    """An unreadable IC series must not leave L3 quietly missing its fold-consistency check."""
    with pytest.raises(ValueError, match="factorMining.missingIcSeries"):
        _fold_ic_signs(None)
    with pytest.raises(ValueError, match="factorMining.missingIcSeries"):
        _fold_ic_signs([])


_MOMENTUM_5 = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 5}}
_NEGATED_MOMENTUM_5 = {"op": "neg", "args": [dict(_MOMENTUM_5)], "params": {}}


def _candidate(expression):
    from app.services.factor_mining.dsl import normalize_expression
    from app.services.factor_mining.templates import CandidateSeed

    return CandidateSeed(expression=expression,
                         fingerprint=normalize_expression(expression).fingerprint,
                         template_id="handmade", origin="template", params={"N": 1})


def _redundancy_views(monkeypatch, expressions):
    """The L4 mapping the loop derives for each expression, read off the surviving candidates.

    Not the trials: a ledger row keeps only the metrics its gates read, and the correlation study
    is not one of them -- the candidate is where the whole measurement lives.
    """
    result, _ = _run_with_seeds(monkeypatch, [_candidate(item) for item in expressions],
                                thresholds=_NO_DEDUPLICATION)
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    return [engine._correlations(item["metrics"]) for item in result["candidates"]]


def test_the_redundancy_mapping_refuses_a_study_that_is_not_this_candidates():
    """The first study row must be the candidate's own, or nothing is mapped at all.

    ``factor_research`` only lists a factor it could compare over enough overlapping
    observations, so the candidate's own row can be missing while built-ins remain. Reading the
    matrix by position would then hand L4 a built-in's correlations -- a candidate measured
    against a pool one factor short, with nothing to say so. An empty mapping is the honest
    answer: ``screen_redundancy`` rejects it as unverifiable.
    """
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    shifted = {"factors": ["momentum_20", "reversal_5"], "matrix": [[1.0, 0.2], [0.2, 1.0]]}
    assert engine._correlations({"factorCorrelation": shifted}) == {}

    own = {"factors": ["expression", "reversal_5"], "matrix": [[1.0, 0.2], [0.2, 1.0]]}
    assert engine._correlations({"factorCorrelation": own}) == {"reversal_5": 0.2}


def test_the_redundancy_gate_measures_the_candidate_not_a_builtin(monkeypatch):
    """Two candidates with opposite signals must get two opposite L4 mappings.

    The fixture's prices are geometric curves, so every expression ranks the symbols the same
    way and a candidate can only differ from another by sign -- which is exactly the difference
    asserted here. When ``_factor_correlation`` regenerates the selected factor instead of
    using the injected panel, both candidates come back with the built-in ``momentum_20``'s
    correlations and this equality holds for any pair, which is what makes the gate decorative.
    """
    momentum, negated = _redundancy_views(monkeypatch, [_MOMENTUM_5, _NEGATED_MOMENTUM_5])
    assert momentum and negated, "a gate with no measurement at all proves nothing"
    assert momentum != negated
    assert momentum["reversal_5"] == pytest.approx(-1.0, abs=1e-9)
    assert negated["reversal_5"] == pytest.approx(1.0, abs=1e-9)


def test_redundancy_gate_rejects_the_candidate_that_duplicates_a_builtin(monkeypatch):
    """Of two candidates, only the built-in's duplicate is rejected -- and for that reason.

    ``ts_pct_change(close, 5)`` is the exact reverse of the ``reversal_5`` built-in, so their
    cross-sectional ranks agree at a correlation of 1.0 and the de-duplication gate must fire.
    Its negation correlates at the same magnitude but the opposite sign, and the gate compares
    magnitude, so it passes. Two candidates, two different verdicts, both from the same gate:
    that is only possible if the gate is looking at each candidate's own values.
    """
    result, _ = _run_with_seeds(monkeypatch, [_candidate(_MOMENTUM_5), _candidate(_NEGATED_MOMENTUM_5)],
                                thresholds=GateThresholds(
                                    min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
                                    min_fold_consistency=0.0, max_turnover=1.0, min_coverage=0.0,
                                    max_builtin_correlation=1.1))
    assert [item["verdict"] for item in result["trials"]] == ["pass", "pass"]

    rejected, _ = _run_with_seeds(monkeypatch, [_candidate(_MOMENTUM_5)],
                                  thresholds=GateThresholds(
                                      min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
                                      min_fold_consistency=0.0, max_turnover=1.0, min_coverage=0.0,
                                      max_builtin_correlation=0.85))
    assert rejected["trials"][0]["stage"] == "L4"
    assert rejected["trials"][0]["reason"] == "factorMining.reject.duplicateBuiltin"


def test_an_injected_panel_is_never_studied_under_a_builtin_name():
    """A caller's factor id must not become the label of an injected panel.

    ``_factor_correlation`` de-duplicates its name list before building the matrix, so a panel
    handed in under ``momentum_20`` merges with that built-in's row: the comparison silently
    loses the built-in and the candidate is measured against a pool one factor smaller. The
    engine therefore labels every injected panel neutrally, whatever id the caller passed. This
    is the mining path's own invariant -- it passes an empty id -- stated where it can break.
    """
    frames, members = _fixture()
    result = FactorResearchEngine().run(
        frames=frames, factor_panel=_evaluator(frames, _MOMENTUM_5), factor_id="momentum_20",
        start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 28), members=members)
    assert result["factorId"] not in FactorResearchEngine._CORRELATION_FACTORS
    assert result["factorCorrelation"]["factors"][0] == result["factorId"]


def test_the_correlation_study_reads_the_injected_panel_not_a_lookalike():
    """The L4 measurement must come from the panel handed in, not from one that ranks alike.

    Correlation values cannot tell the injected panel apart from any positive monotone transform
    of it: every cross-section ranks the same way, so every coefficient comes out equal. The
    observation counts can. Blanking the first half of the search window leaves every value the
    panel still carries exactly as it was, so a study that read the injected panel scores one
    cross-section fewer per blanked date, while one that read any rank-equivalent panel scores
    the same as before -- which is the failure this pins, without inferring it from the signs.
    """
    frames, members = _fixture()
    split = split_periods(_config(thresholds=_NO_DEDUPLICATION))
    sessions = pd.bdate_range(split.search_start, split.search_end)
    cutoff = sessions[len(sessions) // 2]
    whole_panel = _evaluator(frames, _MOMENTUM_5)
    assert int((~whole_panel.loc[whole_panel.index < cutoff].isna()).sum().sum()) > 0, \
        "the half about to be blanked must carry values for their loss to mean anything"

    def study(panel):
        return FactorResearchEngine().run(
            frames=frames, factor_panel=panel, factor_id="", start_date=split.search_start,
            end_date=split.search_end, members=members)

    whole = study(whole_panel)
    partial_panel = whole_panel.copy()
    partial_panel.loc[partial_panel.index < cutoff] = float("nan")
    partial = study(partial_panel)

    # Same comparison pool, so the two counts below are like for like.
    assert whole["factorCorrelation"]["factors"] == partial["factorCorrelation"]["factors"]
    whole_observed = whole["factorCorrelation"]["observations"][0][0]
    partial_observed = partial["factorCorrelation"]["observations"][0][0]
    blanked = [item for item in whole["icSeries"]
               if datetime.fromisoformat(item["time"]) < cutoff]
    assert blanked, "the blanked half must have scored something before it was blanked"
    assert whole_observed - partial_observed == len(blanked)


# --- what a candidate is compared against: the registry's factors plus the caller's pool ---

_POOL_MEMBER = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 10}}


def _empty_pool(user_id, market, timeframe, **kwargs):
    return []


def _recording_pool(members, calls):
    def loader(user_id, market, timeframe, **kwargs):
        calls.append((user_id, market, timeframe, kwargs))
        return list(members)

    return loader


def _unrelated_panel(frames):
    """A comparison panel that is measured but unrelated to the fixture's candidates.

    The fixture's prices are exact geometric curves, so every candidate ranks the six symbols in
    symbol order. A panel holding one fixed cross-section outside that order is a real
    cross-sectional measurement the de-duplication gate does not fire on, which is what a test
    needs when a rejection must be attributable to the pool half rather than to the built-in one.
    """
    panel = _evaluator(frames, _MOMENTUM_5)
    for column, value in zip(panel.columns, [3.0, 6.0, 1.0, 5.0, 2.0, 4.0]):
        panel[column] = value
    return panel


def _computable_factor_ids(frames):
    """Registry ids a panel carrying these columns can measure, derived outside the module.

    Mirrors the registry's own ``required_fields`` contract rather than a hand-kept list: the
    fixture's panels carry price and volume only, so every technical factor is measurable and
    no fundamental one is.
    """
    from app.services.factors import list_factors

    columns = set().union(*(set(frame.columns) for frame in frames.values()))
    return [item["factor_id"] for item in list_factors()
            if set(item["required_fields"]) <= columns]


def _assembly(frames, members, *, pool_loader, factor_limit=DEFAULT_BUILTIN_COMPARISON_LIMIT,
              pool_limit=pool.DEFAULT_POOL_LIMIT, config=None):
    """The comparison set one run assembles for the fixture's search panel."""
    config = config or _config()
    split = split_periods(config)
    return _comparison_set(
        panel=_FakeProvider(frames, members).panel, config=config, user_id=1,
        start_date=split.search_start, end_date=split.search_end,
        pool_loader=pool_loader, factor_limit=factor_limit, pool_limit=pool_limit,
    )


def test_the_comparison_set_defaults_bound_the_registry_and_the_pool():
    """The two caps are the run's cost bound, and they are the sizes they claim to be.

    Every measured name costs one point-in-time panel rebuilt once per run and one
    cross-sectional Spearman per candidate, so the caps are what keeps a run's cost proportional
    to something a caller chose. The registry's cap is its own size: a smaller number would
    silently drop factors from the comparison, and a bigger one would only ever be slack.
    """
    assert DEFAULT_BUILTIN_COMPARISON_LIMIT == 61
    assert pool.DEFAULT_POOL_LIMIT == 20


def test_the_comparison_set_covers_the_registry_factors_the_panel_can_measure():
    """Built-in coverage comes from the registry, filtered by the fields the panel carries.

    That filter is what "computable" means here: the registry computes each factor from a
    frame's fields, so a factor whose fields the panel does not carry has no point-in-time
    values to correlate with, and listing it would only report a comparison that never ran.
    """
    frames, members = _frames_and_members(periods=80)

    assembled = _assembly(frames, members, pool_loader=_empty_pool)

    assert {"rsi", "macd", "atr"} <= set(assembled.factors)
    assert "earnings_yield" not in assembled.factors
    assert set(assembled.factors) <= set(_computable_factor_ids(frames))
    assert set(assembled.panels) == set(assembled.factors)
    for name in ("rsi", "macd"):
        assert int(assembled.panels[name].notna().sum().sum()) >= 3


def test_the_registry_comparison_panel_is_point_in_time():
    """Each value is computed from the bars up to its own date, never from the whole window.

    Correlating a candidate against a factor whose values were computed from data the candidate
    never saw would decide L4 with the future. Rewriting the last bars leaves every earlier
    value untouched -- and moves the later ones, which is what keeps the first half from being
    vacuously equal to a panel that is a constant.
    """
    frames, members = _frames_and_members(periods=80)
    name = _computable_factor_ids(frames)[0]

    before = _assembly(frames, members, pool_loader=_empty_pool, factor_limit=1)
    assert before.factors == (name,)
    mutated = {key: frame.copy() for key, frame in frames.items()}
    for frame in mutated.values():
        frame.iloc[-20:] = frame.iloc[-20:] * 3.0
    after = _assembly(mutated, members, pool_loader=_empty_pool, factor_limit=1)

    panel = before.panels[name]
    assert len(panel) > 8, "the window must reach past the bars the mutation touches"
    pd.testing.assert_frame_equal(panel.iloc[:3], after.panels[name].iloc[:3])
    assert not panel.iloc[-3:].equals(after.panels[name].iloc[-3:])


def test_the_registry_panel_is_built_on_the_studys_own_index():
    """A comparison panel must be aligned with the candidate's, whatever the frames arrived as.

    The study normalizes the frames it reads, so a panel built from the raw ones would carry a
    different index: every date lookup would miss, the panel would be empty on every date, and
    the factor would drop out of the study without a word -- a de-duplication gate running
    against one comparison factor fewer, and reporting nothing about it.
    """
    frames, members = _frames_and_members(periods=80)
    localized = {key: frame.set_axis(frame.index.tz_localize("UTC"))
                 for key, frame in frames.items()}

    assembled = _assembly(localized, members, pool_loader=_empty_pool, factor_limit=1)

    panel = assembled.panels[assembled.factors[0]]
    assert int(panel.notna().sum().sum()) >= 3
    assert panel.index.equals(pd.Index(_assembly(frames, members, pool_loader=_empty_pool,
                                                factor_limit=1).panels[assembled.factors[0]].index))


def test_the_comparison_set_is_capped_in_registry_order():
    """The cap truncates the tail of a deterministic order, so a run's comparison set repeats.

    An unstable order would not only move the truncation point between runs: the correlation
    matrix's columns would follow it, so two runs over the same panel would report the same
    candidate against a differently ordered set of factors.
    """
    frames, members = _frames_and_members(periods=80)

    limited = _assembly(frames, members, pool_loader=_empty_pool, factor_limit=2)

    assert limited.factors == tuple(_computable_factor_ids(frames)[:2])
    assert set(limited.panels) == set(limited.factors)


def test_pool_members_are_namespaced_and_their_skips_are_reported():
    """The pool half of the comparison set: scoped, prefixed, capped, and never silently short.

    A pooled candidate is a fingerprint, which is not a name a reader can tell apart from a
    registry factor id, so every member is namespaced. A member whose stored expression no
    longer evaluates is handed back as a skip rather than dropped: a comparison set that is one
    member shorter than the pool looks exactly like a pool that never had it.
    """
    from app.services.factor_mining.pool import PoolMember

    frames, members = _frames_and_members(periods=80)
    calls: list[tuple] = []
    pooled = [
        PoolMember(candidate_id=7, fingerprint="a" * 64, expression=_POOL_MEMBER),
        PoolMember(candidate_id=8, fingerprint="b" * 64,
                   expression={"op": "nope", "args": [], "params": {}}),
    ]

    assembled = _assembly(frames, members, pool_loader=_recording_pool(pooled, calls),
                          factor_limit=0, pool_limit=5)

    assert calls == [(1, "USStock", "1d", {"limit": 5})]
    assert assembled.factors == ()
    assert assembled.pool == ("pool:aaaaaaaa",)
    assert set(assembled.panels) == {"pool:aaaaaaaa"}
    assert assembled.skipped == (
        {"fingerprint": "b" * 64, "reason": "factorMining.reject.invalid"},
    )


def test_the_pool_section_follows_the_order_the_pool_loader_returned():
    """The pool half keeps the loader's order; nothing here re-sorts or de-duplicates by name.

    ``load_pool`` hands its members back newest first, and that order is what the cap keeps, so
    it is the order the comparison set is built in. The study's correlation matrix follows the
    name list it was handed, and L4 reads its columns by name: an assembly that sorted the pool
    -- or dropped it through a set -- would move those columns between two runs over the same
    pool, and a stored run's ``factorCorrelation`` would no longer line up with one taken again.
    The fingerprints are picked so id-descending and fingerprint-ascending disagree, which is
    what makes a ``sorted()`` visible here rather than a coincidence.
    """
    from app.services.factor_mining.pool import PoolMember

    frames, members = _frames_and_members(periods=80)
    pooled = [
        PoolMember(candidate_id=9, fingerprint="f" * 64, expression=_POOL_MEMBER),
        PoolMember(candidate_id=4, fingerprint="a" * 64, expression=_POOL_MEMBER),
        PoolMember(candidate_id=2, fingerprint="b" * 64, expression=_POOL_MEMBER),
    ]

    assembled = _assembly(frames, members, pool_loader=_recording_pool(pooled, []),
                          factor_limit=0, pool_limit=5)

    assert assembled.pool == ("pool:ffffffff", "pool:aaaaaaaa", "pool:bbbbbbbb")
    assert set(assembled.panels) == set(assembled.pool)


def test_a_pool_members_twin_is_rejected_under_the_pool_key():
    """The pool half reaches the ledger: a candidate duplicating a frozen member says so.

    The injected built-in is measured and clear, so the only reading above its bar is the pooled
    one -- and the row that records the rejection names the pool, not the registry. This is the
    seam the second threshold exists for: the same candidate measured against the registry alone
    would have passed, and a ledger that called this a built-in duplicate would send a reader
    looking for a factor that is not there.
    """
    frames, members = _fixture()
    provider = _FakeProvider(frames, members)
    provider.comparison = ComparisonSet(
        factors=("rsi",),
        pool=("pool:deadbeef",),
        panels={"rsi": _unrelated_panel(frames),
                "pool:deadbeef": _evaluator(frames, _NEGATED_MOMENTUM_5)},
    )

    result = FactorMiningEngine(data_provider=provider).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(),
    )

    assert result["trials"][0]["stage"] == "L4"
    assert result["trials"][0]["reason"] == "factorMining.reject.duplicateInPool"


def test_a_pool_member_that_never_rebuilt_rejects_the_candidate():
    """A member the pool could not rebuild is asked about too, and it is not a clearance.

    ``pool_panels`` hands back a member whose stored expression no longer evaluates as a skip
    rather than a panel, and the assembly keeps it out of the comparison names -- so the only way
    the gate hears about it at all is the skip list, carried as the pool name the member would
    have had and no reading. The comparison set here is a real assembly over a pool of one
    unreadable member, the shape a stored pool takes after an operator set moves on: with the
    skip dropped, the run compares against a set one member short of the pool, reports nothing,
    and accepts candidates the pool may already hold.
    """
    from app.services.factor_mining.pool import PoolMember

    frames, members = _fixture()
    unreadable = [PoolMember(candidate_id=7, fingerprint="a" * 64,
                             expression={"op": "nope", "args": [], "params": {}})]
    provider = _FakeProvider(frames, members)
    provider.comparison = _assembly(frames, members, pool_loader=_recording_pool(unreadable, []),
                                    factor_limit=0, pool_limit=5)
    assert provider.comparison.pool == () and provider.comparison.factors == ()
    assert [item["fingerprint"] for item in provider.comparison.skipped] == ["a" * 64]

    result = FactorMiningEngine(data_provider=provider).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(),
    )

    assert result["trials"][0]["stage"] == "L4"
    assert result["trials"][0]["reason"] == "factorMining.reject.unverifiableRedundancy"


def test_a_pool_member_that_could_not_be_measured_rejects_the_candidate():
    """A member the study could not compare still reaches the gate -- as no evidence.

    The member was asked about and is absent from the study, which looks exactly like a pool that
    never held it unless the engine carries the name back with a NaN. Without that the gate sees
    a comparison set that was fully measured and clears a candidate whose own library holds a
    member nothing could be measured against -- the skipped member read as a distinction it never
    earned. The built-in section here measured cleanly, which is the point: one section's reading
    does not stand in for the other's.
    """
    frames, members = _fixture()
    provider = _FakeProvider(frames, members)
    provider.comparison = ComparisonSet(
        factors=("rsi",),
        pool=("pool:deadbeef",),
        panels={"rsi": _unrelated_panel(frames),
                "pool:deadbeef": _evaluator(frames, _MOMENTUM_5) * float("nan")},
    )

    result = FactorMiningEngine(data_provider=provider).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(),
    )

    assert result["trials"][0]["stage"] == "L4"
    assert result["trials"][0]["reason"] == "factorMining.reject.unverifiableRedundancy"


def test_a_pool_member_with_no_correlatable_cross_section_rejects_the_candidate():
    """A member the study could name but never *pair* is not a clearance either.

    The member's panel is not empty -- it holds a full column of values, so the study's
    observation filter keeps its name and reports it as measured -- but every one of its cells
    sits on a symbol outside the run's universe, so no cross-section reaches the three paired
    names a correlation needs. The study writes what it has to write there: a ``0.0`` placeholder
    with a zero observation count, "not measured" spelled as a number. Read as a reading, that
    placeholder clears the candidate -- exactly the plausible-but-wrong zero the per-section
    fail-closed rule exists to stop, reached by a name that never left the mapping. The built-in
    half measured cleanly, so the rejection can only come from the pool's own section.
    """
    frames, members = _fixture()
    # One real panel's worth of rows and dates, carried under a symbol the universe does not hold.
    unrelated = _unrelated_panel(frames)
    orphan = unrelated[[unrelated.columns[0]]].rename(columns={unrelated.columns[0]: "ZZ"})
    assert list(orphan.columns) == ["ZZ"] and orphan.notna().to_numpy().sum() >= 3
    provider = _FakeProvider(frames, members)
    provider.comparison = ComparisonSet(
        factors=("rsi",),
        pool=("pool:deadbeef",),
        panels={"rsi": unrelated, "pool:deadbeef": orphan},
    )

    result = FactorMiningEngine(data_provider=provider).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(),
    )

    assert result["trials"][0]["stage"] == "L4"
    assert result["trials"][0]["reason"] == "factorMining.reject.unverifiableRedundancy"


def test_the_candidate_is_compared_against_registry_factors():
    """A mined candidate is measured against registry factors, not only the six proxies.

    The set the engine assembled is what the study used: the names it reports participating are
    exactly the assembled ones, and the proxies are absent rather than quietly re-added -- an
    injected set replaces the default pool instead of joining it.
    """
    frames, members = _frames_and_members(periods=80)
    provider = _FakeProvider(frames, members)
    config = _config(thresholds=_NO_DEDUPLICATION)
    split = split_periods(config)
    provider.comparison = _comparison_set(
        panel=provider.panel, config=config, user_id=1,
        start_date=split.search_start, end_date=split.search_end,
        pool_loader=_empty_pool, factor_limit=3,
    )

    result = FactorMiningEngine(data_provider=provider).run(
        user_id=1, code="PORTFOLIO = {}", config=config,
    )

    assert result["candidates"], "a comparison set with no measurement would reject everything"
    names = result["candidates"][0]["metrics"]["factorCorrelation"]["factors"]
    assert set(provider.comparison.factors) <= set(names)
    assert "reversal_5" not in names
    assert result["summary"]["comparisonFactors"]["builtin"] == list(provider.comparison.factors)


def test_the_provider_hands_its_own_caps_to_the_comparison_set():
    """The caps and the pool reader are the provider's, so a caller can bound the cost of a run.

    The comparison set is assembled by the provider because it is panel-shaped data, and its two
    bounds are the provider's own settings -- a run's cost is symbols x dates x comparison
    factors, and a deployment that finds that too much has to be able to lower it without
    touching the search. Nothing here reaches the panel's own fetcher: only the pool loader is
    called, which is why a provider built without a backtest service is enough.
    """
    frames, members = _frames_and_members(periods=80)
    calls: list[tuple] = []
    provider = MiningDataProvider(pool_loader=_recording_pool([], calls),
                                  comparison_factor_limit=2, pool_limit=3)
    config = _config()
    split = split_periods(config)

    assembled = provider.comparison_set(
        panel=_FakeProvider(frames, members).panel, config=config, user_id=9,
        start_date=split.search_start, end_date=split.search_end,
    )

    assert assembled.factors == tuple(_computable_factor_ids(frames)[:2])
    assert calls == [(9, "USStock", "1d", {"limit": 3})]


def test_the_run_reports_the_names_it_compared_and_those_it_could_not():
    """What a stored run compared against is readable afterwards, skips included.

    The candidate's own metrics carry the names that were really measured; the run's summary
    carries what was asked for, which is the pair a reader needs to tell "measured as unrelated"
    from "never measured" -- the distinction the de-duplication gate refuses to blur.
    """
    provider = _FakeProvider(*_fixture())
    provider.comparison = ComparisonSet(
        factors=("rsi",),
        pool=("pool:deadbeef",),
        panels={
            "rsi": _evaluator(_fixture()[0], _MOMENTUM_5),
            "pool:deadbeef": _evaluator(_fixture()[0], _NEGATED_MOMENTUM_5),
        },
        skipped=({"fingerprint": "f" * 64, "reason": "factorMining.reject.invalid"},),
    )
    engine = FactorMiningEngine(data_provider=provider)

    result = engine.run(user_id=1, code="PORTFOLIO = {}",
                        config=_config(thresholds=_NO_DEDUPLICATION))

    assert result["summary"]["comparisonFactors"] == {
        "builtin": ["rsi"],
        "pool": ["pool:deadbeef"],
        "skipped": [{"fingerprint": "f" * 64, "reason": "factorMining.reject.invalid"}],
    }
    names = result["candidates"][0]["metrics"]["factorCorrelation"]["factors"]
    assert names == ["expression", "rsi", "pool:deadbeef"]


def test_a_run_keeps_the_increment_inputs_of_its_own_search_window():
    """L6's inputs are assembled by the search, out of the search window, out of the pool's half.

    The increment is a search-period judgment -- what this candidate adds to the library the run
    compared it against -- so the run assembles the evidence while it holds both: the search
    panel's own field panels, the search window's research dates, and the returns over them. The
    registry's factors are left out (they are the comparison L4 already made, not the library a
    candidate is added to), and the frozen tail is not in the panel to begin with, because the
    search's own fetch stops at ``search_end``. A member the comparison could not rebuild travels
    as a skip, under the name L4 would have asked about it by.
    """
    frames, members = _fixture()
    config = _config(budget=1)
    split = split_periods(config)
    provider = _FakeProvider(frames, members)
    provider.comparison = ComparisonSet(
        factors=("rsi",),
        pool=("pool:deadbeef",),
        panels={
            "rsi": _evaluator(frames, _MOMENTUM_5),
            "pool:deadbeef": _unrelated_panel(frames),
        },
        skipped=({"fingerprint": "f" * 64, "reason": "factorMining.reject.invalid"},),
    )

    engine = FactorMiningEngine(data_provider=provider)
    engine.run(user_id=1, code="PORTFOLIO = {}", config=config)

    evidence = engine._pool_increment
    assert evidence is not None, "the run is what assembles L6's inputs; the review reads them"
    assert set(evidence.members) == {"pool:deadbeef"}, "the registry's factors are not the library"
    assert evidence.skipped == ("pool:ffffffff",)
    assert evidence.fields is provider.panel.fields, "the candidates' own panels, not a copy"
    # One grid for all three sides of the regression, inside the search window and nowhere else.
    assert evidence.members["pool:deadbeef"].index.equals(evidence.returns.index)
    assert evidence.returns.index.min() >= pd.Timestamp(split.search_start)
    assert evidence.returns.index.max() <= pd.Timestamp(split.search_end)


def test_an_empty_comparison_set_keeps_the_builtin_proxies():
    """Nothing assembled is not the same as comparing against nothing.

    An empty set handed to the study as an empty mapping would leave the de-duplication gate
    with no finite measurement at all, and its fail-closed rule would reject every candidate of
    the run -- a search with no pool and no registry hit would find nothing, ever.
    """
    result, _ = _search(thresholds=_NO_DEDUPLICATION)

    names = result["candidates"][0]["metrics"]["factorCorrelation"]["factors"]
    assert "reversal_5" in names


def test_a_run_made_under_a_patched_expansion_is_never_memoized(monkeypatch):
    """A patched expansion's run must not answer the template search's memoized key.

    Writing it there leaves the wrong run behind for every later test that asks for the same
    key -- which only shows up in a subset run, where no earlier test repopulated the entry
    first. That is the shape of the false red this suite already had once, so the memo refuses
    to store a run it cannot attribute to the real expansion.

    This patches by hand rather than through ``_patched_expansion``: that helper's ``finally``
    drops every entry its block added, which would hide the very write being asserted here.
    """
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: _seeds(1))
    patched, _ = _search(thresholds=_NO_DEDUPLICATION)
    assert len(patched["trials"]) == 1, "the patch must be in effect for this test to mean anything"

    monkeypatch.undo()
    unpatched, _ = _search(thresholds=_NO_DEDUPLICATION)
    assert unpatched is not patched
    assert len(unpatched["trials"]) == 12, "the template search's own run was overwritten"


def test_members_without_a_key_are_reported_as_no_membership():
    """Members the research engine cannot map to a symbol must not blank the cross-section.

    ``_membership_panel`` matches on ``member["key"]``. A member that has a ``symbol`` but no
    ``key`` is an entry that matches nothing, which reads as inactive on every date and turns
    every candidate into an inconclusive rejection -- a worse failure than having no
    membership information at all, because it is silent. So they are reported as the empty
    sequence, which the research engine documents as "no membership restrictions".
    """
    assert _resolved_members([{"market": "USStock", "symbol": "A", "effectiveFrom": None}]) == ()
    keyed = [{"key": "USStock:A", "market": "USStock", "symbol": "A"}]
    assert _resolved_members(keyed) == keyed


def test_the_search_does_not_write_into_the_shared_candidate_expansion():
    """The expanded seeds are shared, so the loop must not touch their expression or params.

    A template's base expression is the same object as its two smoothing variants' inner
    argument, and ``params`` feeds the fingerprint -- writing a field into either would
    silently rewrite siblings that share the object, and the run would keep going with a
    different candidate set than the one it recorded. Comparing against a freshly expanded
    copy catches both an added key and a changed value.
    """
    fresh = {seed.fingerprint: seed for seed in expand_candidates(
        template_ids=None, budget=12, seed=3)}
    result, _ = _search(thresholds=_NO_DEDUPLICATION)
    assert result["candidates"]
    for item in result["candidates"]:
        expected = fresh[item["fingerprint"]]
        assert item["expression"] == expected.expression
        assert item["expression"].keys() == expected.expression.keys()
        assert repr(item["expression"]) == repr(expected.expression)


def test_the_point_in_time_universe_is_applied_per_date(monkeypatch):
    """A member that joins the universe mid-window never appears in an earlier cross-section.

    ``USStock:F`` carries a ``membership_periods`` starting in May. The research engine records
    the symbols it grouped on every date, so the test reads that ledger directly: F is held
    after it joins, and absent before -- which is what says the point-in-time universe is
    actually being evaluated rather than waved through.
    """
    frames, members = _fixture()
    with _patched_expansion(monkeypatch, _seeds(1)):
        result, _ = _run(frames, members, thresholds=_NO_DEDUPLICATION)
    groups = result["candidates"][0]["metrics"]["groupObservations"]
    assert groups, "no cross-section was grouped"
    joined = datetime(2024, 5, 1)
    before = [item for item in groups if datetime.fromisoformat(item["time"]) < joined]
    after = [item for item in groups if datetime.fromisoformat(item["time"]) >= joined]
    assert before and after, "the window must span both sides of F's membership start"
    assert not any("USStock:F" in item["members"] for item in before)
    assert any("USStock:F" in item["members"] for item in after)
    assert all(item["crossSectionSize"] == 5 for item in before)
    assert any(item["crossSectionSize"] == 6 for item in after)


def test_keyless_members_leave_the_search_running_rather_than_blanking_it(monkeypatch):
    """The keyless branch observed end to end: the run still scores its candidates.

    Passing those members through unchanged would give ``_membership_panel`` an entry matching
    no symbol, so every date would have an empty cross-section and every candidate would die
    at ``insufficientObservations``. The assertions below fail on that outcome, so the branch
    is exercised rather than merely asserted in isolation.
    """
    frames, _ = _fixture()
    keyless = [{"market": "USStock", "symbol": symbol, "effectiveFrom": None}
               for symbol in ["A", "B", "C", "D", "E", "F"]]
    with _patched_expansion(monkeypatch, _seeds(1)):
        result, _ = _run(frames, keyless, thresholds=_NO_DEDUPLICATION)
    assert [item["verdict"] for item in result["trials"]] == ["pass"]
    assert result["trials"][0]["stage"] != "L1"
    assert result["candidates"], "a keyless run must still produce candidates"


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
                                thresholds=_NO_DEDUPLICATION)
    assert [item["verdict"] for item in result["trials"]] == ["reject", "pass"]
    assert result["trials"][0]["reason"] == "factorMining.reject.invalid"
    assert result["trials"][1]["reason"] == ""


# --- the field set the run's market allows, at every point that validates a candidate ----------

# A candidate that *names* a derivatives leaf without depending on its values: the condition holds
# on every bar of the fixture, so its panel is the momentum leg alone. What these tests read is
# which leaf the run's field set admits -- a candidate whose panel was a column of nothing would
# be refused by a later gate, for a reason this fixture cannot tell apart from the one it means.
_FUNDING_LEG = {
    "op": "where",
    "args": [
        {"op": "gt", "args": ["close", {"op": "const", "args": [], "params": {"value": 0}}],
         "params": {}},
        {"op": "ts_pct_change", "args": ["close"], "params": {"n": 3}},
        "funding_rate",
    ],
    "params": {},
}


def _funding_seed(expression, market="Crypto"):
    """A seed for an expression only ``market``'s field set admits.

    ``_seed_for`` cannot build this one -- it fingerprints through the DSL, which refuses the leaf
    under the default set -- so the fingerprint is read the way the engine reads it.
    """
    from app.services.factor_mining.dsl import normalize_expression
    from app.services.factor_mining.templates import CandidateSeed

    return CandidateSeed(
        expression=expression,
        fingerprint=normalize_expression(expression, allowed_fields=fields_for(market)).fingerprint,
        template_id="momentum", origin="template", params={},
    )


def test_a_derivatives_field_is_accepted_after_the_field_set_is_threaded_through(monkeypatch):
    """A Crypto run may name a derivatives field, and the whole ladder has to agree it may.

    L1 is not the only validation point in the path: the evaluator normalizes every subtree it
    memoizes, so a field set that stopped at the DSL call would raise inside the candidate the
    whitelist had just admitted.
    """
    result, _ = _run_with_seeds(monkeypatch, [_funding_seed(_FUNDING_LEG)], market="Crypto",
                                thresholds=_NO_DEDUPLICATION)
    assert [item["verdict"] for item in result["trials"]] == ["pass"]
    assert result["candidates"], "a candidate that cleared L1-L4 is kept, not merely tolerated"


def test_a_derivatives_field_is_rejected_when_the_market_has_none(monkeypatch):
    """On a market with no funding history the leaf is an unknown field, not a missing column.

    This is the direction the whitelist must not lose: a USStock run reads the same expression
    against the price-volume group alone and refuses it at L1, with the field key.
    """
    result, _ = _run_with_seeds(monkeypatch, [_funding_seed(_FUNDING_LEG)])
    assert [item["verdict"] for item in result["trials"]] == ["reject"]
    assert result["trials"][0]["stage"] == "L1"
    assert result["trials"][0]["reason"] == "factorMining.reject.invalid"
    assert result["candidates"] == []


@pytest.mark.parametrize("market,refused", [("Crypto", False), ("USStock", True)])
def test_the_frozen_review_reads_the_survivor_under_the_runs_field_set(market, refused):
    """L5 re-reads the survivor on the tail, so it has to be handed the field set L1 was.

    A review that fell back to the default would reject a derivatives survivor as invalid -- the
    one verdict that says "the expression is broken" about an expression that is not.
    """
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    survivor = {"expression": _FUNDING_LEG, "fingerprint": "f" * 64, "origin": "template",
                "template_id": "momentum", "status": "search", "reason": "",
                "metrics": {}, "outOfSample": {}}
    reviewed = engine.review_frozen(user_id=1, code="PORTFOLIO = {}", config=_config(market=market),
                                    survivors=[survivor], trials_count=1)
    assert (reviewed[0]["reason"] == "factorMining.reject.invalid") is refused


def test_the_warmup_scan_reads_the_candidates_under_the_runs_field_set(monkeypatch):
    """The warmup pre-scan normalizes every template candidate before a bar is fetched.

    A leaf the set refuses raises out of the scan, which ends the run before a single candidate
    was scored -- the same rejection as L1's, only louder. The window is longer than the fixture's
    templates so the answer cannot hide behind the warmup floor.
    """
    seed = _funding_seed({"op": "ts_mean", "args": ["funding_rate"], "params": {"n": 60}})
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: [seed])
    assert _warmup_bars(_config(market="Crypto"), "enumeration") == 60 + engine_module.WARMUP_MARGIN_BARS


def test_the_fitness_lookup_reads_an_expression_under_the_runs_field_set(monkeypatch):
    """Evolution ranks a generation by what this run measured for each member.

    ``fitness`` fingerprints the member to find that measurement, and a member it cannot name is
    "unmeasured" rather than "bad": it can never be an elite. A default field set here would rank
    every derivatives individual last while the run reported nothing wrong.
    """
    seed = _funding_seed(_FUNDING_LEG)
    answers: list = []

    def stream(source, *, fitness, **_kwargs):
        # A generator, so the body runs on the first pull -- after the generation chained ahead of
        # it was scored, which is the only point at which the lookup has an answer to give.
        yield from ()
        answers.append(fitness(seed.expression))

    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: [seed])
    monkeypatch.setattr(engine_module, "evolve", stream)
    _run(*_fixture(), search_mode="evolution", market="Crypto", thresholds=_NO_DEDUPLICATION)
    assert answers and answers[0] is not None, \
        "the individual was scored, so the lookup has no excuse for not naming it"


def test_run_reports_the_config_split_and_summary():
    result, _ = _search()
    assert result["config"]["market"] == "USStock"
    assert result["config"]["budget"] == 12
    # The split's exact edges: the frozen tail starts the day after the last search session, so
    # an off-by-one in either direction would move a bar from one window into the other.
    assert result["split"] == {
        "searchStart": "2024-01-01T00:00:00",
        "searchEnd": "2024-07-31T00:00:00",
        "freezeStart": "2024-08-01T00:00:00",
        "freezeEnd": "2024-10-31T00:00:00",
    }
    # The config's dates leave in that same spelling. ``asdict`` keeps native datetimes, and the
    # durable job store renders those with ``str()`` -- "2024-01-01 00:00:00", which a browser's
    # ``new Date()`` reads as Invalid Date -- beside an ISO ``split`` in the same response.
    assert result["config"]["start_date"] == "2024-01-01T00:00:00"
    assert result["config"]["end_date"] == "2024-10-31T00:00:00"
    assert result["summary"]["snapshotId"] == "b" * 64
    assert result["summary"]["trials"] == len(result["trials"])
    assert result["summary"]["candidates"] == len(result["candidates"])


def test_the_research_engine_is_told_the_runs_own_frequency(monkeypatch):
    """ICIR is annualized from the run's timeframe and markets, not from the engine's default.

    ICIR = ``ic_mean / ic_std * sqrt(annualization / holding_period)``, so a weekly run left on
    the engine's 252-bar default would report an ICIR about 2.2x its own and clear ``min_icir``
    on a number measured in another unit. The crypto case is what says the markets travel with
    it: a daily US-equity run's 252 is indistinguishable from the default.
    """
    calls: list[dict] = []

    class _Recording(FactorResearchEngine):
        def run(self, **kwargs):
            calls.append(kwargs)
            return super().run(**kwargs)

    monkeypatch.setattr(engine_module, "FactorResearchEngine", _Recording)

    _run(*_fixture(), timeframe="1w")
    assert calls, "no evaluation reached the research engine"
    assert {call["annualization_periods"] for call in calls} == {52.0}

    crypto = [{**member, "market": "Crypto"} for member in _fixture()[1]]
    calls.clear()
    _run(_fixture()[0], crypto)
    assert {call["annualization_periods"] for call in calls} == {365.25}


# --- the candidate source: the template expansion, or a stream the engine breeds -----------


def test_evolution_warmup_uses_the_dsl_lookback_budget(monkeypatch):
    """Evolution's warmup is the DSL's ceiling, taken without expanding anything first.

    An evolved individual's longest window is not knowable before it has been bred -- a crossover
    may graft a 252-bar window onto any path -- so the ceiling is the only bound that stays
    correct for a run that breeds. Enumeration keeps the candidate-derived warmup instead; that
    half is pinned where the fetch itself is (`test_enumeration_warmup_is_unchanged_from_v1`).
    """
    config = _config(template_ids=("reversal",), budget=200, seed=3)
    monkeypatch.setattr(engine_module, "expand_candidates",
                        lambda **kwargs: pytest.fail("evolution must not pre-scan candidates"))
    assert _warmup_bars(config, "evolution") == MAX_LOOKBACK == 504


def _stream_run(monkeypatch, individuals, seeds=(), **overrides):
    """One evolution pass over ``seeds`` and then exactly ``individuals``.

    Both halves are patched so a test decides the whole stream: the template generation the
    engine scores first, and what evolution yields after it. The empty default leaves the bred
    half alone, which is what a test about the engine's own handling of an individual wants.
    """
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: list(seeds))
    monkeypatch.setattr(engine_module, "evolve", lambda source, **kwargs: iter(individuals))
    return _run(*_fixture(), search_mode="evolution", **overrides)


def test_search_consumes_a_generator_source(monkeypatch):
    """A stream source is pulled one individual at a time, and its progress is priced in budget.

    Two properties in one run. The engine has to score what it already holds before the stream
    is resumed: evolution ranks a generation by the caller's answer about its members, and that
    answer exists only once the engine has evaluated them -- draining the stream first would
    rank every generation on nothing. And the progress total is the generation plus the budget,
    not the length of what the stream produced: draws that yield nothing still spend budget, so
    the output is an outcome, never a length a reporter may announce in advance.
    """
    generation = _seeds(2)
    bred = _seeds(10)
    scored_between_draws: list[float | None] = []

    def stream(source, *, fitness, **_kwargs):
        for index, seed in enumerate(bred):
            if index:
                # Asked from inside the stream: the individual yielded before this point is only
                # answerable if the consumer evaluated it before asking for the next one.
                scored_between_draws.append(fitness(bred[index - 1].expression))
            yield seed

    monkeypatch.setattr(engine_module, "evolve", stream)
    with _patched_expansion(monkeypatch, generation):
        result, seen = _run(*_fixture(), search_mode="evolution", budget=25,
                            thresholds=_NO_DEDUPLICATION)
    assert scored_between_draws and all(value is not None for value in scored_between_draws), \
        "the stream was drained before its individuals were scored"
    assert len(result["trials"]) == 2 + 10
    assert seen == [{"stage": "search", "evaluated": 10, "total": 2 + 25}]


def test_every_scored_individual_cleared_l1_first(monkeypatch):
    """An individual reaches the evaluator only after L1 validated it, whoever produced it.

    A window perturbation can build a tree no template could -- three nested 252-bar windows,
    whose lookback is 756 -- and every layer below L1 would compute it without complaint, so L1
    is what keeps an evolved individual inside the lookback bound. The individual it refuses is
    a rejected trial rather than a measurement, and the run carries on to the next one.
    """
    over_budget = {"op": "ts_mean", "args": [
        {"op": "ts_mean", "args": [
            {"op": "ts_mean", "args": ["close"], "params": {"n": 252}}], "params": {"n": 252}}],
        "params": {"n": 252}}
    healthy = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 3}}
    reached_the_evaluator: list = []
    real_evaluate = ExpressionEvaluator.evaluate

    def recording(self, node):
        reached_the_evaluator.append(node)
        return real_evaluate(self, node)

    monkeypatch.setattr(ExpressionEvaluator, "evaluate", recording)
    result, _ = _stream_run(monkeypatch, [_unvalidated_seed(over_budget), _seed_for(healthy)],
                            thresholds=_NO_DEDUPLICATION)
    invalid, scored = result["trials"]
    assert (invalid["stage"], invalid["verdict"]) == ("L1", "reject")
    assert invalid["reason"] == "factorMining.reject.invalid"
    assert reached_the_evaluator == [healthy], "the individual L1 refused was evaluated anyway"
    assert scored["verdict"] == "pass"


def _unvalidated_seed(expression):
    """A seed the DSL refuses, which ``_seed_for`` cannot build: it fingerprints through the DSL.

    The fingerprint is spelled here because an individual L1 rejects never produces one -- which
    is half of what the test above is about.
    """
    from app.services.factor_mining.templates import CandidateSeed

    return CandidateSeed(expression=expression, fingerprint="0" * 64, template_id="evolution",
                         origin="evolution", params={})


def test_the_template_seeds_are_evaluated_in_evolution_mode():
    """The seeds evolution breeds from are candidates of this run, and are scored like any other.

    The template library is the only place this feature's hypotheses come from, so an evolution
    run that bred without evaluating its generation would throw away every hypothesis-backed
    candidate -- and the ledger, which prices the search, would be missing exactly the candidates
    R6's fingerprint set assumes it holds.
    """
    generation = expand_candidates(budget=12, seed=3)
    result, _ = _run(*_fixture(), search_mode="evolution", thresholds=_NO_DEDUPLICATION)
    scored = [item["fingerprint"] for item in result["trials"]]
    assert [seed.fingerprint for seed in generation] == scored[:len(generation)]
    assert len(scored) > len(generation), "this run must also have bred something"


def test_the_generation_is_ranked_before_evolution_breeds(monkeypatch):
    """The first generation has measurements by the time evolution's body runs.

    The template generation is chained in front of the bred stream, and a generator's body runs
    on the first pull -- which is the pull after the generation has been exhausted, and therefore
    after every seed has been scored. Breeding from an unranked generation is what a ``None``
    answer is for, and it is not what a run should start from when the seeds are right there.
    """
    generation = _seeds(3)
    opinions: list[float | None] = []

    def stream(source, *, fitness, **_kwargs):
        opinions.extend(fitness(seed.expression) for seed in generation)
        # A generator with nothing to yield: the body above still runs on the first pull, and it
        # must not run at construction time or this test would measure the wrong moment.
        for individual in ():
            yield individual

    monkeypatch.setattr(engine_module, "evolve", stream)
    with _patched_expansion(monkeypatch, generation):
        _run(*_fixture(), search_mode="evolution", thresholds=_NO_DEDUPLICATION)
    assert opinions and all(value is not None for value in opinions), \
        "the first generation was bred from before it was ranked"


def test_every_emitted_individual_gets_a_trial_row(monkeypatch):
    """Every individual the engine receives is evaluated and written down, generation included.

    The ledger is the denominator of the selection-bias correction, so nothing the run scored may
    be missing from it. What the engine receives is the template generation plus what evolution
    yields from it, and the two are bounded differently: the generation is a count the run knows,
    while the yield is only bounded by the budget -- a refused draw, and a draw that repeats an
    expression the run already holds, both spend budget without being an individual at all.
    """
    emitted: list = []
    real_evolve = engine_module.evolve

    def counting(source, **kwargs):
        for individual in real_evolve(source, **kwargs):
            emitted.append(individual)
            yield individual

    monkeypatch.setattr(engine_module, "evolve", counting)
    result, _ = _run(*_fixture(), search_mode="evolution", budget=20, thresholds=_NO_DEDUPLICATION)
    generation = expand_candidates(budget=20, seed=3)
    assert emitted, "the stream produced nothing, so this run says nothing about the ledger"
    assert [item["fingerprint"] for item in result["trials"]] == \
        [seed.fingerprint for seed in generation] + [seed.fingerprint for seed in emitted]
    assert len(emitted) <= 20, "the budget bounds the draws, not the run's ledger"


@pytest.mark.parametrize("search_mode", ["enumeration", "evolution"])
def test_run_is_byte_identical_for_the_same_seed(search_mode):
    """Two runs of one mode and one seed produce the same ledger, byte for byte.

    Evolution draws its randomness from ``random.Random(seed)`` alone and its fitness is the
    engine's own measurement, so the bred population, its trials and their order are all
    reproducible. Comparing serialized records rather than fingerprints is what makes this
    byte-identical: a reordered metric or a changed float would pass a fingerprint comparison.
    """
    first, _ = _run(*_fixture(), search_mode=search_mode, thresholds=_NO_DEDUPLICATION)
    second, _ = _run(*_fixture(), search_mode=search_mode, thresholds=_NO_DEDUPLICATION)
    assert first is not second, "a memoized second run would compare a record with itself"
    assert json.dumps(first["trials"], sort_keys=True) == json.dumps(second["trials"], sort_keys=True)
    assert len(first["trials"]) > 1, "a single repeated trial would satisfy any equality"


def test_the_search_and_the_review_tell_the_provider_which_warmup_rule_applies():
    """The warmup follows the mode, so the engine has to say which mode the panel is for.

    A provider left to guess would fetch v1's candidate-derived window for an evolved run --
    short of the history the candidates being reviewed need, and silent about it.
    """
    provider = _FakeProvider(*_fixture())
    engine = FactorMiningEngine(data_provider=provider)
    survivor = {"expression": _MOMENTUM_5, "fingerprint": "f" * 64, "origin": "template",
                "template_id": "momentum", "status": "search", "reason": "",
                "metrics": {}, "outOfSample": {}}
    engine.run(user_id=1, code="PORTFOLIO = {}", config=_config(budget=2),
               search_mode="evolution")
    engine.review_frozen(user_id=1, code="PORTFOLIO = {}", config=_config(), survivors=[survivor],
                         trials_count=1, search_mode="evolution")
    assert provider.search_modes == ["evolution", "evolution"]


def test_the_breeding_settings_come_from_the_run_config(monkeypatch):
    """The population, its generations and its elite share are the run's own, not the engine's.

    A second copy of these numbers inside the engine would leave a stored run's config unable to
    explain the search it ran -- the record and the numbers would be free to drift apart.
    """
    seen: dict = {}

    def stream(source, **kwargs):
        seen.update(kwargs)
        return iter(())

    monkeypatch.setattr(engine_module, "evolve", stream)
    _run(*_fixture(), search_mode="evolution", population=7, generations=3, elite_ratio=0.5,
         thresholds=_NO_DEDUPLICATION)

    assert (seen["population"], seen["generations"], seen["elite_ratio"]) == (7, 3, 0.5)


def test_a_run_that_names_no_mode_is_byte_identical_to_enumeration():
    """No mode named anywhere searches v1's templates: the same candidates, byte for byte.

    The service leans on this default, so it is pinned here rather than assumed -- a changed
    default would move what every v1 caller searches without any of them naming a mode.
    """
    unnamed = FactorMiningEngine(data_provider=_FakeProvider(*_fixture())).run(
        user_id=1, code="PORTFOLIO = {}", config=_config(thresholds=_NO_DEDUPLICATION),
    )
    named, _ = _search(thresholds=_NO_DEDUPLICATION)
    assert unnamed["candidates"], "a run with no candidates would satisfy any comparison"
    assert json.dumps(unnamed["candidates"], sort_keys=True) == \
        json.dumps(named["candidates"], sort_keys=True)


def test_an_unknown_search_mode_is_refused_rather_than_enumerated():
    """A mode the engine does not know is a caller's bug, not a request for the default.

    Enumerating anyway would search the templates and label the run with the mode it was asked
    for -- two records disagreeing about what ran, and a caller who cannot see the difference.
    """
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        engine.run(user_id=1, code="PORTFOLIO = {}", config=_config(), search_mode="genetic")


def test_the_review_refuses_an_unknown_search_mode_too():
    """Both entries read the same config, so both have to answer for its mode.

    A mode the search would have refused must not reach the review at all -- and the review that
    has nothing to review is no exception, because the two entries must not disagree about one
    config. The refusal comes before the empty-survivors shortcut for that reason.
    """
    survivor = {"expression": _MOMENTUM_5, "fingerprint": "f" * 64, "origin": "template",
                "template_id": "momentum", "status": "search", "reason": "",
                "metrics": {}, "outOfSample": {}}
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    for survivors in ([survivor], []):
        with pytest.raises(ValueError, match="factorMining.invalidParameter"):
            engine.review_frozen(user_id=1, code="PORTFOLIO = {}", config=_config(),
                                 survivors=survivors, trials_count=1, search_mode="genetic")


class _FakeRepo:
    """The proposal store an AI run writes through. Tests that bypass ``_candidate_source``
    never reach it; the ones that do read ``history_calls`` to see what the run asked for."""

    def __init__(self):
        self.history_calls: list[dict] = []
        self.saved: list[tuple] = []
        self.status_updates: list[dict] = []

    def failure_summary(self, **kwargs):
        self.history_calls.append(kwargs)
        return {"runIds": [], "stageCounts": [], "recentFailures": []}

    def save_proposals(self, run_id, *, user_id, proposals):
        self.saved.append((run_id, user_id, proposals))

    def update_proposal_status(self, run_id, *, user_id, round_no, ordinals, status):
        self.status_updates.append({
            "run_id": run_id, "user_id": user_id, "round_no": round_no,
            "ordinals": tuple(ordinals), "status": status,
        })


@pytest.fixture
def _engine_fixture():
    frames, members = _fixture()
    return FactorMiningEngine(data_provider=_FakeProvider(frames, members)), _config()


def _ai_options(**overrides):
    base = dict(run_id=9, research_notes="", proposer=None, repository=_FakeRepo(),
                replay_proposals=())
    base.update(overrides)
    return AISearchOptions(**base)


def test_ai_mode_requires_ai_options(_engine_fixture):
    engine, config = _engine_fixture
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        engine.run(user_id=4, code="code", config=config, search_mode=SEARCH_MODE_AI)


def test_non_ai_mode_refuses_ai_options(_engine_fixture):
    engine, config = _engine_fixture
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        engine.run(user_id=4, code="code", config=config, ai=_ai_options())


def test_ai_mode_uses_max_lookback_for_search_and_review():
    assert _warmup_bars(_config(search_mode=SEARCH_MODE_AI), SEARCH_MODE_AI) == MAX_LOOKBACK


def test_the_ai_review_warmup_travels_to_the_provider():
    provider = _FakeProvider(*_fixture())
    survivor = {"expression": _MOMENTUM_5, "fingerprint": "f" * 64, "origin": "ai",
                "template_id": "", "status": "search", "reason": "",
                "metrics": {}, "outOfSample": {}}
    FactorMiningEngine(data_provider=provider).review_frozen(
        user_id=1, code="PORTFOLIO = {}", config=_config(), survivors=[survivor],
        trials_count=1, search_mode=SEARCH_MODE_AI,
    )
    assert provider.search_modes == [SEARCH_MODE_AI]


def test_every_yielded_ai_seed_gets_a_trial(_engine_fixture, monkeypatch):
    engine, config = _engine_fixture
    calls = []

    class _Source:
        def __iter__(self):
            yield CandidateSeed(
                expression={"op": "ts_pct_change", "args": ["close"], "params": {"n": 5}},
                fingerprint="a" * 64, template_id="", origin="ai", params={})

        def finish(self):
            pass

        def ai_summary(self):
            return {"enabled": True, "proposals": 1, "evaluated": 1}

    monkeypatch.setattr(engine_module, "_candidate_source",
                        lambda *a, **k: (_Source(), 1))
    result = engine.run(
        user_id=4, code="code", config=config, search_mode=SEARCH_MODE_AI,
        ai=AISearchOptions(run_id=9, research_notes="", proposer=None,
                           repository=_FakeRepo(), replay_proposals=()))
    assert len(result["trials"]) == 1
    assert result["summary"]["ai"]["enabled"] is True


def test_a_seeds_outcome_is_complete_before_the_source_resumes(_engine_fixture, monkeypatch):
    """The source closes a round when it is resumed, so the outcome must already be there.

    Evaluating while iterating is what the second AI round's feedback is built from; a source
    drained first would find every outcome missing and prompt the model with an empty round.
    """
    engine, config = _engine_fixture
    seed = CandidateSeed(expression=_MOMENTUM_5, fingerprint="a" * 64, template_id="",
                         origin="ai", params={})
    captured: dict = {}
    resumed: list = []

    class _Source:
        def __iter__(self):
            captured["before"] = captured["outcome"](seed.fingerprint)
            yield seed
            resumed.append(captured["outcome"](seed.fingerprint))

        def finish(self):
            pass

        def ai_summary(self):
            return {"enabled": True}

    def fake_source(*args, **kwargs):
        captured["outcome"] = kwargs["outcome"]
        return _Source(), 1

    monkeypatch.setattr(engine_module, "_candidate_source", fake_source)
    engine.run(user_id=4, code="code", config=config, search_mode=SEARCH_MODE_AI,
               ai=_ai_options())

    assert captured["before"] is None
    outcome = resumed[0]
    assert outcome["stage"] in ("L1", "L2", "L3", "L4")
    assert outcome["verdict"] in ("pass", "reject")
    assert isinstance(outcome["metrics"], dict)


def test_the_ai_candidate_source_reads_scoped_history_and_returns_a_proposal_stream():
    """The history is the tenant's own, this run excluded, and the total is the AI budget."""
    config = _config(search_mode=SEARCH_MODE_AI, ai_rounds=3, ai_proposals_per_round=5,
                     budget=20)
    repo = _FakeRepo()
    source, total = engine_module._candidate_source(
        config, SEARCH_MODE_AI, lambda expression: None,
        user_id=4, ai=_ai_options(run_id=9, repository=repo), outcome=lambda fp: None,
    )
    assert repo.history_calls == [{"user_id": 4, "market": "USStock", "timeframe": "1d",
                                   "exclude_run_id": 9}]
    assert total == 15
    assert hasattr(source, "ai_summary")


def _payload(**overrides):
    base = {"market": "USStock", "timeframe": "1D", "startDate": "2024-01-01",
            "endDate": "2024-10-31", "sourceId": 1, "budget": 12, "seed": 3}
    base.update(overrides)
    return base


def _config_from(payload):
    return MiningConfig.from_payload(payload, default_commission=0.0005,
                                     default_slippage=0.0005)


def test_ai_settings_are_refused_outside_ai_mode():
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        _config_from(_payload(aiRounds=2))
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        _config_from(_payload(aiProposalsPerRound=5))


def test_ai_settings_default_and_bounds():
    defaults = _config_from(_payload(searchMode="ai"))
    assert (defaults.ai_rounds, defaults.ai_proposals_per_round) == (2, 10)
    named = _config_from(_payload(searchMode="ai", aiRounds=3, aiProposalsPerRound=20,
                                  replayJobId="job-1"))
    assert (named.ai_rounds, named.ai_proposals_per_round, named.replay_job_id) == \
        (3, 20, "job-1")
    for bad in ({"aiRounds": 0}, {"aiRounds": 4}, {"aiProposalsPerRound": 0},
                {"aiProposalsPerRound": 21}):
        with pytest.raises(ValueError, match="factorMining.invalidParameter"):
            _config_from(_payload(searchMode="ai", **bad))


def test_replay_is_refused_outside_ai_mode():
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        _config_from(_payload(replayJobId="job-1"))


def test_ai_outcomes_keep_only_the_feedback_metrics(_engine_fixture, monkeypatch):
    """The per-round outcome lives for the whole run, so it keeps only what feedback reads."""
    engine, config = _engine_fixture
    seed = CandidateSeed(expression=_MOMENTUM_5, fingerprint="a" * 64, template_id="",
                         origin="ai", params={})
    captured: dict = {}

    class _Source:
        def __iter__(self):
            yield seed

        def finish(self):
            pass

        def ai_summary(self):
            return {"enabled": True}

    def fake_source(*args, **kwargs):
        captured.update(kwargs)
        return _Source(), 1

    monkeypatch.setattr(engine_module, "_candidate_source", fake_source)
    result = engine.run(user_id=4, code="code", config=config, search_mode=SEARCH_MODE_AI,
                        ai=_ai_options())

    outcome = captured["outcome"](seed.fingerprint)
    assert set(outcome) == {"stage", "verdict", "reason", "metrics"}
    assert set(outcome["metrics"]) == {"rankIc", "icir"}
    assert outcome["metrics"]["rankIc"] == result["trials"][0]["metrics"]["rankIc"]
    assert outcome["metrics"]["icir"] == result["trials"][0]["metrics"]["icir"]


def test_non_ai_runs_do_not_retain_outcomes(_engine_fixture, monkeypatch):
    """Enumeration and evolution must not hold a per-candidate copy of the full metrics."""
    engine, config = _engine_fixture
    captured: dict = {}

    def fake_source(*args, **kwargs):
        captured.update(kwargs)
        return iter(()), 0

    monkeypatch.setattr(engine_module, "_candidate_source", fake_source)
    engine.run(user_id=4, code="code", config=config)
    assert captured["outcome"] is None


def test_ai_evaluation_is_capped_by_the_run_budget(monkeypatch):
    """The rounds bound the source; the run's candidate budget bounds what is evaluated."""
    entries = tuple(
        {"round": 1, "ordinal": ordinal,
         "expression": {"op": "ts_pct_change", "args": ["close"], "params": {"n": window}}}
        for ordinal, window in enumerate((2, 3, 4, 5, 6), start=1)
    )
    seen: list[dict] = []
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()),
                                on_progress=seen.append)
    monkeypatch.setattr(engine_module, "PROGRESS_EVERY", 1)
    result = engine.run(
        user_id=4, code="code",
        config=_config(budget=2, ai_rounds=2, ai_proposals_per_round=5),
        search_mode=SEARCH_MODE_AI,
        ai=_ai_options(replay_proposals=entries),
    )
    assert len(result["trials"]) == 2
    assert [event["evaluated"] for event in seen] == [1, 2]
    assert {event["total"] for event in seen} == {2}


def test_ai_context_budget_is_the_effective_cap(monkeypatch):
    """The prompt reports the budget the run will actually evaluate, not the raw product."""
    captured: dict = {}

    class _Source:
        def __iter__(self):
            return iter(())

        def finish(self):
            pass

        def ai_summary(self):
            return {"enabled": True}

    def fake_source(**kwargs):
        captured.update(kwargs)
        return _Source()

    monkeypatch.setattr(engine_module, "ProposalSource", fake_source)
    source, total = engine_module._candidate_source(
        _config(budget=2, ai_rounds=2, ai_proposals_per_round=5), SEARCH_MODE_AI,
        lambda expression: None, user_id=4, ai=_ai_options(),
        outcome=lambda fingerprint: None,
    )
    assert captured["context"].budget == 2
    assert total == 2
    assert list(source) == []


def test_a_capped_ai_run_closes_the_last_round():
    """A cap stops the stream before its close code; the run must close the round itself."""
    entries = tuple(
        {"round": 1, "ordinal": ordinal,
         "expression": {"op": "ts_pct_change", "args": ["close"], "params": {"n": window}}}
        for ordinal, window in enumerate((2, 3, 4, 5, 6), start=1)
    )
    repo = _FakeRepo()
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    result = engine.run(
        user_id=4, code="code",
        config=_config(budget=2, ai_rounds=2, ai_proposals_per_round=5),
        search_mode=SEARCH_MODE_AI,
        ai=_ai_options(run_id=9, repository=repo, replay_proposals=entries),
    )

    assert result["summary"]["ai"]["evaluated"] == len(result["trials"]) == 2
    assert repo.status_updates == [{
        "run_id": 9, "user_id": 4, "round_no": 1,
        "ordinals": (1, 2), "status": "evaluated",
    }]
    rows = repo.saved[-1][2]
    assert [row["status"] for row in rows] == [
        "evaluated", "evaluated", "proposed", "proposed", "proposed",
    ]


def test_a_full_budget_ai_run_closes_its_last_round():
    """The default configuration hits the cap exactly, so no further pull ever closes the tail."""
    entries = tuple(
        {"round": (ordinal - 1) // 5 + 1, "ordinal": (ordinal - 1) % 5 + 1,
         "expression": {"op": "ts_pct_change", "args": ["close"], "params": {"n": window}}}
        for ordinal, window in enumerate(range(2, 12), start=1)
    )
    repo = _FakeRepo()
    engine = FactorMiningEngine(data_provider=_FakeProvider(*_fixture()))
    result = engine.run(
        user_id=4, code="code",
        config=_config(budget=10, ai_rounds=2, ai_proposals_per_round=5),
        search_mode=SEARCH_MODE_AI,
        ai=_ai_options(run_id=9, repository=repo, replay_proposals=entries),
    )

    assert result["summary"]["ai"]["evaluated"] == len(result["trials"]) == 10
    assert [(call["round_no"], call["ordinals"]) for call in repo.status_updates] == [
        (1, (1, 2, 3, 4, 5)), (2, (1, 2, 3, 4, 5)),
    ]
