"""Frozen-segment review tests for factor mining.

Every data source is faked: no network call and no database runs here. The real dependencies are
``FactorResearchEngine`` and the L5 gate, because the review scores survivors through the same
pair the search scores candidates through.

The review's input is built here, never read out of a search's candidate list. The shared
fixture's candidates are near-perfect copies of the ``reversal_5`` built-in, so the default
de-duplication bar rejects every one of them: a test asserting on ``run()["candidates"]`` would
assert on an empty list and hold whatever the review did. A survivor is assembled from one of the
search's *trials* instead -- the ledger holds one record per evaluated candidate, rejected ones
included -- paired with the seed that produced it.

The frozen tail is served by the fake provider, which is what lets these tests make it disagree
with the search window. ``_reverting_fixture`` makes the tail pay the signal backwards, for the
sign-flip case, and ``_oscillating_fixture`` gives the long/short curve a return distribution the
selection-bias haircut can price: the plain fixture drifts at a constant rate, so its period
returns are identical and ``deflated_sharpe`` answers ``insufficientVariation`` instead of a
probability.
"""

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.backtest_limits import backtest_warmup_calendar_days
from app.services.factor_mining import engine as engine_module
from app.services.factor_mining.engine import (
    ComparisonSet,
    FactorMiningEngine,
    MiningDataProvider,
    PeriodSplit,
    PreparedPanel,
    _field_panels,
    _warmup_bars,
    split_periods,
)
from app.services.factor_mining.gates import GateThresholds, GateVerdict, net_return_series
from app.services.factor_mining.pool import PoolMember
from app.services.factor_mining.templates import expand_candidates
from app.utils import agent_jobs
from tests.test_factor_mining_data import (
    _FakeBacktestService,
    _SOURCE,
    _config as _provider_config,
)
from tests.test_factor_mining_engine import (
    _MOMENTUM_5,
    _FakeProvider,
    _config,
    _evaluator,
    _frames_and_members,
    _fixture,
    _run,
    _seed_for,
    _unrelated_panel,
)

_CODE = "PORTFOLIO = {}"

# Passed to both the expansion and the run, so the seeds this module expands are exactly the
# candidates that run evaluates, in the same order.
_SEED = 3

# A candidate that parses but cannot be evaluated: scalar division by zero dies inside the
# operator layer, not in the DSL. The frozen review must record it, not inherit the exception.
_ZERO_DIVISION = {"op": "div", "args": [
    {"op": "const", "args": [], "params": {"value": 1}},
    {"op": "const", "args": [], "params": {"value": 0}},
], "params": {}}

_STRICT_OOS = GateThresholds(
    min_ic_observations=3, min_abs_rank_ic=0.0, min_icir=0.0, min_fold_consistency=0.0,
    max_turnover=1.0, min_coverage=0.0, min_oos_abs_rank_ic=0.99,
    min_deflated_sharpe_probability=0.0,
)


class _CountingProvider(_FakeProvider):
    """The shared fake plus a call log, so "the frozen tail is read once" is counted."""

    def __init__(self, frames, members):
        super().__init__(frames, members)
        self.calls: list[str] = []

    def prepare(self, *, user_id, code, config, search_mode=None):
        self.calls.append("prepare")
        return super().prepare(user_id=user_id, code=code, config=config, search_mode=search_mode)

    def prepare_freeze(self, *, user_id, code, config, search_mode=None):
        self.calls.append("prepare_freeze")
        return super().prepare_freeze(user_id=user_id, code=code, config=config,
                                      search_mode=search_mode)


def _record(seed, metrics):
    """One survivor record, in the shape ``run`` writes into its candidate list.

    The review extends exactly those records, so its input is built from the search's own record
    shape rather than from a candidate the ladder happened to keep.
    """
    return {"expression": seed.expression, "fingerprint": seed.fingerprint,
            "origin": seed.origin, "template_id": seed.template_id,
            "status": "search", "reason": "", "metrics": metrics, "outOfSample": {}}


def _survivor_records(trials, seeds):
    """Pair a search's ledger with the seeds that produced it, one record per candidate."""
    return [_record(seed, trial["metrics"]) for seed, trial in zip(seeds, trials)]


def _survivors(count=1, panel=None):
    """Survivors over ``panel`` (the shared fixture by default) with search-segment metrics.

    The metrics come out of a real search -- the window and the engine the pipeline searches
    with -- so the in-sample side of the L5 comparison is a measurement, not a hand-written
    number. Trials are read rather than candidates because a trial exists for every evaluated
    candidate: this input does not depend on the ladder having passed anything. The seeds are
    expanded with the same budget and seed the run gets, which is what lines the two up.
    """
    frames, members = _fixture() if panel is None else panel
    seeds = expand_candidates(budget=count, seed=_SEED)
    result, _ = _run(frames, members, budget=count, seed=_SEED)
    return _survivor_records(result["trials"], seeds)


def _oscillating_fixture():
    """A fresh panel whose prices carry a per-symbol wave, so the frozen window has variance.

    The plain fixture drifts at a constant rate, which keeps every symbol's rank stable and the
    long/short curve's period returns identical: ``deflated_sharpe`` then reports
    ``insufficientVariation`` and no survivor could ever clear L5. A wave that moves the ranking
    over time is what the selection-bias haircut needs in order to answer with a probability.
    Built from ``_frames_and_members`` rather than the suite's memoized fixture: the shared
    frames are read-only for every other test.
    """
    frames, members = _frames_and_members()
    for index, key in enumerate(sorted(frames)):
        frame = frames[key]
        wave = [1.0 + 0.05 * math.sin((day + index * 2.0) / 3.0) for day in range(len(frame))]
        frames[key] = frame.assign(close=frame["close"] * wave, open=frame["open"] * wave)
    return frames, members


def _reverting_fixture():
    """A frozen tail whose prices pay the signal *backwards*.

    The factor is read off ``close`` and the trade is entered and exited at ``open``, so giving
    each symbol an ``open`` series drifting opposite to its ``close`` makes every cross-section
    rank the factor one way and the forward return the other. Drift grows with the symbol's
    index, so the frozen window's IC is the exact negative of the search window's -- a real sign
    flip, not a re-labelling: permuting prices between symbols cannot move a cross-sectional
    correlation, so the reversal has to live in the relationship between signal and return.
    """
    frames, members = _frames_and_members()
    for index, key in enumerate(sorted(frames)):
        frame = frames[key]
        drift = 0.002 * (index + 1)
        frame["open"] = [100 * (1 - drift) ** day for day in range(len(frame))]
    return frames, members


def test_freeze_review_freezes_a_survivor_whose_edge_holds_out_of_sample():
    """The frozen panel is a real measurement of the frozen window, and it can be passed."""
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    reviewed = FactorMiningEngine(data_provider=_FakeProvider(*panel)).review_frozen(
        user_id=1, code=_CODE, config=_config(), survivors=[survivor], trials_count=12)

    assert [item["status"] for item in reviewed] == ["frozen"]
    assert reviewed[0]["reason"] == ""
    out_of_sample = reviewed[0]["outOfSample"]
    # Both windows agree on the sign, which is the first half of L5 -- asserted here as the
    # mechanism behind the frozen status rather than left to the gate's own suite.
    assert out_of_sample["rankIc"] * survivor["metrics"]["rankIc"] > 0
    # And it is the frozen window that was scored, not the whole fetched panel. The survivor's
    # metrics come out of the trial ledger, which keeps its IC observation count rather than the
    # series -- the length is what L2 reads, and the series is what makes a ledger row large.
    assert len(out_of_sample["icSeries"]) < survivor["metrics"]["icObservations"]
    # The review answers with a new record instead of editing the run's ledger entry.
    assert survivor["status"] == "search"
    assert survivor["outOfSample"] == {}


def test_freeze_review_rejects_when_out_of_sample_sign_flips():
    """The frozen tail pays the signal backwards, so the survivor's edge does not survive it.

    L5 checks the sign before the strength bar, and the bar from the brief
    (``min_oos_abs_rank_ic=0.99``) is cleared here either way -- a constant-drift panel ranks at
    an ``abs(rankIc)`` of 1. The assertion below pins the reason to the flip itself: the frozen
    window's IC is the search window's, negated.
    """
    survivor = _survivors()[0]
    reviewed = FactorMiningEngine(data_provider=_FakeProvider(*_reverting_fixture())
                                 ).review_frozen(
        user_id=1, code=_CODE, config=_config(thresholds=_STRICT_OOS),
        survivors=[survivor], trials_count=12)

    assert [item["status"] for item in reviewed] == ["rejected"]
    assert [item["reason"] for item in reviewed] == ["factorMining.reject.weakOutOfSample"]
    assert reviewed[0]["outOfSample"]["rankIc"] * survivor["metrics"]["rankIc"] < 0


def test_freeze_review_reads_the_frozen_tail_once_for_every_survivor():
    """One fetch serves the whole review: the segment is read once per run, not once per candidate."""
    panel = _oscillating_fixture()
    provider = _CountingProvider(*panel)
    reviewed = FactorMiningEngine(data_provider=provider).review_frozen(
        user_id=1, code=_CODE, config=_config(),
        survivors=_survivors(count=3, panel=panel), trials_count=12)

    assert len(reviewed) == 3
    assert all(item["outOfSample"] for item in reviewed), "every survivor was scored"
    assert provider.calls == ["prepare_freeze"]


def test_freeze_review_reads_nothing_when_there_are_no_survivors():
    """A search that kept nothing has nothing to review -- and no reason to open the tail."""
    provider = _CountingProvider(*_fixture())
    reviewed = FactorMiningEngine(data_provider=provider).review_frozen(
        user_id=1, code=_CODE, config=_config(), survivors=[], trials_count=12)

    assert reviewed == []
    assert provider.calls == []


def test_freeze_review_reports_progress_and_stops_on_a_cancellation():
    """The review has to report, because that callback is where ``agent_jobs`` checks for cancel.

    A review that never reported would ignore a cancellation issued while it ran: the job row
    would say ``cancelled`` while the run kept writing candidates and closed itself as a success,
    so the two records would state opposite things about one event. One report per survivor, and
    before the survivor is scored -- the point of the check is not to spend the work.
    """
    panel = _oscillating_fixture()
    survivors = _survivors(count=3, panel=panel)
    seen: list[dict] = []
    reviewed = FactorMiningEngine(data_provider=_FakeProvider(*panel), on_progress=seen.append
                                  ).review_frozen(
        user_id=1, code=_CODE, config=_config(), survivors=survivors, trials_count=12)

    assert [event["stage"] for event in seen] == ["review"] * 3
    assert [(event["evaluated"], event["total"]) for event in seen] == [(1, 3), (2, 3), (3, 3)]
    assert len(reviewed) == 3

    reported: list[int] = []

    def _cancel(event):
        reported.append(event["evaluated"])
        raise agent_jobs.JobCancelledError("JOB_CANCELLED")

    with pytest.raises(agent_jobs.JobCancelledError):
        FactorMiningEngine(data_provider=_FakeProvider(*panel), on_progress=_cancel).review_frozen(
            user_id=1, code=_CODE, config=_config(), survivors=survivors, trials_count=12)
    assert reported == [1], "the review scored a survivor after the cancellation was raised"


def test_freeze_review_prices_the_costs_the_run_passed_in():
    """Commission and slippage have to reach the frozen measurement, not just its label.

    ``_research_window`` forwards them to the research engine, which charges them against each
    rebalance. The label on the metrics block is written from the config either way, so comparing
    the two net curves is what distinguishes a cost that was charged from one that was merely
    declared -- and a turnover of zero would make the comparison hold for free.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    assert survivor["metrics"]["averageTurnover"] > 0, \
        "a candidate that never traded would show no cost difference at all"

    def review(**costs):
        return FactorMiningEngine(data_provider=_FakeProvider(*panel)).review_frozen(
            user_id=1, code=_CODE, config=_config(**costs),
            survivors=[survivor], trials_count=12)[0]["outOfSample"]

    free = review(commission=0.0, slippage=0.0)
    charged = review(commission=0.05, slippage=0.05)

    assert free["executionAssumptions"]["commission"] == 0.0
    assert charged["executionAssumptions"]["commission"] == 0.05
    free_nav = [point["net"] for point in free["longShortCurve"]]
    charged_nav = [point["net"] for point in charged["longShortCurve"]]
    assert free_nav != charged_nav, "the costs never reached the frozen measurement"
    assert charged_nav[-1] < free_nav[-1], "a cost that was charged lowers a net long/short curve"


def test_freeze_review_prices_the_haircut_with_the_runs_whole_trial_count(monkeypatch):
    """``trials`` is forwarded as given: the correction has to price the whole ledger.

    The recording gate stands in for L5, so what is asserted is the calls the review makes, one
    per survivor: the trial count it passes on -- the run's, identical for every survivor rather
    than a count of the survivors themselves -- and the return series it prices. Each series is
    recomputed here from that survivor's own frozen curve at full precision, so a review that
    rounded it, or that fed the haircut the search window's curve, would compare unequal.
    """
    calls: list[dict] = []

    def _recording_screen(**kwargs):
        calls.append(kwargs)
        return GateVerdict(passed=False, stage="L5", reason="factorMining.reject.selectionBias")

    monkeypatch.setattr(engine_module, "screen_out_of_sample", _recording_screen)
    reviewed = FactorMiningEngine(data_provider=_FakeProvider(*_fixture())).review_frozen(
        user_id=1, code=_CODE, config=_config(),
        survivors=_survivors(count=2), trials_count=37)

    assert [call["trials"] for call in calls] == [37, 37]
    assert [call["returns"] for call in calls] == [
        net_return_series(item["outOfSample"]["longShortCurve"]) for item in reviewed]
    assert all(call["returns"] for call in calls), "the frozen curve must produce a return series"
    assert {item["reason"] for item in reviewed} == {"factorMining.reject.selectionBias"}


def test_freeze_review_records_an_unreadable_survivor_without_losing_the_others(monkeypatch):
    """A survivor the frozen panel cannot score is rejected, and the review carries on."""
    panel = _oscillating_fixture()
    seeds = [_seed_for(_ZERO_DIVISION), *expand_candidates(budget=1, seed=_SEED)]
    # Patched by hand: ``_run`` is not memoized, so there is no cache to keep honest here.
    monkeypatch.setattr(engine_module, "expand_candidates", lambda **kwargs: seeds)
    result, _ = _run(*panel)
    survivors = _survivor_records(result["trials"], seeds)
    reviewed = FactorMiningEngine(data_provider=_FakeProvider(*panel)).review_frozen(
        user_id=1, code=_CODE, config=_config(), survivors=survivors, trials_count=12)

    assert [item["status"] for item in reviewed] == ["rejected", "frozen"]
    assert reviewed[0]["reason"] == "factorMining.reject.invalid"
    assert reviewed[0]["outOfSample"] == {}
    assert reviewed[1]["reason"] == ""


def test_prepare_freeze_fetches_the_frozen_tail_with_its_warmup():
    """The tail is fetched by its own request: same path, the window the search did not load."""
    service = _FakeBacktestService()
    config = _provider_config(freeze_ratio=0.25)
    provider = MiningDataProvider(backtest_service=service)
    search = provider.prepare(user_id=1, code=_SOURCE, config=config)
    panel = provider.prepare_freeze(user_id=1, code=_SOURCE, config=config)
    split = split_periods(config)
    warmup_days = backtest_warmup_calendar_days(config.timeframe, _warmup_bars(config, "enumeration"))

    _, search_fetch_start, search_fetch_end = service.fetch_calls[0]
    _, freeze_fetch_start, freeze_fetch_end = service.fetch_calls[1]
    assert search_fetch_end == split.search_end < freeze_fetch_end == split.freeze_end
    assert freeze_fetch_start == split.freeze_start - timedelta(days=warmup_days)
    assert freeze_fetch_start > search_fetch_start, "the tail is its own request"
    # Same universe and same panel semantics as the search panel: only the range differs.
    assert panel.members == search.members
    assert panel.frequency == config.timeframe


def test_prepare_freeze_refuses_a_split_that_overlaps_the_search(monkeypatch):
    """A frozen tail overlapping the search window is refused before a bar is fetched.

    ``split_periods`` hands back disjoint windows by construction, so this guard only fires if
    the split changes meaning. That is exactly when it matters: candidates fitted to the bars
    that are supposed to judge them are not being reviewed at all.
    """
    service = _FakeBacktestService()
    config = _provider_config()
    split = split_periods(config)
    overlapping = PeriodSplit(search_start=split.search_start, search_end=split.freeze_start,
                              freeze_start=split.freeze_start, freeze_end=split.freeze_end)
    monkeypatch.setattr(engine_module, "split_periods", lambda _: overlapping)

    with pytest.raises(ValueError, match="factorMining.searchFreezeOverlap"):
        MiningDataProvider(backtest_service=service).prepare_freeze(
            user_id=1, code=_SOURCE, config=config)
    assert service.fetch_calls == []


# --- L6: what a survivor adds to the pool the run compared it against ----------------------

# One pool member's comparison name, spelled the way ``_comparison_set`` spells it: the pool
# namespace plus the first ``POOL_NAME_LENGTH`` characters of the member's fingerprint.
_MEMBER = "pool:deadbeef"


def _pool(panels, skipped=()):
    """A comparison set holding a pool and nothing else: the half L6 measures against."""
    return ComparisonSet(pool=tuple(panels), panels=dict(panels), skipped=tuple(skipped))


def _search_panel(panel):
    """One fixture's frames as a ``PreparedPanel``, the way ``_FakeProvider`` builds its own."""
    frames, members = panel
    return PreparedPanel(frames=frames, members=members, fields=_field_panels(frames),
                         snapshot_id="b" * 64, frequency="1d", warnings=[])


class _SplitProvider(_FakeProvider):
    """A fake whose search panel and frozen tail are different frames.

    ``_FakeProvider`` serves one panel for both calls, so it cannot show a review reading a
    window the search never loaded, nor two reviews of one search that differ only in the tail.
    The tail is an attribute rather than a constructor argument for the second of those: L6's
    inputs are assembled by the search and live on the engine that ran it, so a test that moves
    the tail between two reviews has to move it under that one engine.
    """

    def __init__(self, search, frozen=None):
        super().__init__(*search)
        self.frozen = _search_panel(frozen or search)

    def prepare_freeze(self, *, user_id, code, config, search_mode=None):
        self.search_modes.append(search_mode)
        return self.frozen


def _searched(panel, comparison, *, frozen=None, config=None):
    """``(engine, provider, config)`` for an engine that has run over ``panel``.

    Every L6 reading is taken from the search that produced the survivors, so a test has to run
    one: the review alone holds no search window, and a review with no search behind it has
    nothing to measure an increment on. The budget is one template because the count of
    candidates a run evaluates is not what L6 reads.
    """
    config = config or _config(budget=1, seed=_SEED)
    provider = _SplitProvider(panel, frozen)
    provider.comparison = comparison
    engine = FactorMiningEngine(data_provider=provider)
    engine.run(user_id=1, code=_CODE, config=config)
    return engine, provider, config


def _review(engine, config, survivors):
    return engine.review_frozen(
        user_id=1, code=_CODE, config=config, survivors=survivors, trials_count=12)


def _truncated(panel, end):
    """The same fixture with every frame cut at ``end``: a source that stopped at the window end."""
    frames, members = panel
    return {key: frame.loc[:end] for key, frame in frames.items()}, members


def _drifted(panel):
    """Another shipment of the same data: every price row given a compounding per-symbol drift.

    The frozen panel a review hands over has to *really* differ from the search panel for the
    isolation test to mean anything, and a constant factor is not enough -- it leaves every ratio
    inside the scaled block alone, so most of the window's forward returns would not move.
    Compounding a different rate per symbol re-ranks the cross-sections, and the drift runs over
    the whole panel rather than the frozen tail alone on purpose: a review that read the frozen
    panel where it should have read the search window would otherwise still agree with itself on
    the search-period stretch. It moves L5's reading, which is measured inside the frozen tail,
    and it is invisible to every bar L6 is allowed to read.
    """
    frames, members = panel
    drifted: dict[str, pd.DataFrame] = {}
    for offset, key in enumerate(sorted(frames)):
        frame = frames[key].copy()
        rates = pd.Series([(1.0 + 0.01 * (offset + 1)) ** step for step in range(len(frame))],
                          index=frame.index)
        drifted[key] = frame.mul(rates, axis=0)
    return drifted, members


def test_a_frozen_survivor_carries_its_pool_increment():
    """A survivor that clears L5 is then asked what it adds to the library the run compared to.

    The member is a panel unrelated to the candidate, so taking it out leaves the candidate's own
    reading standing: the increment is a measured clearance rather than a missing measurement.
    The pool's size and the names that were really compared travel with it, and the verdict is
    the three-state flag -- ``True`` here, which is the only state that says "adds information".
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    engine, _provider, config = _searched(panel, _pool({_MEMBER: _unrelated_panel(panel[0])}))

    reviewed = _review(engine, config, [survivor])

    assert [item["status"] for item in reviewed] == ["frozen"]
    increment = reviewed[0]["outOfSample"]["poolIncrement"]
    assert increment["hasIncrementalValue"] is True
    assert increment["reason"] == ""
    assert increment["residualIc"] * increment["rawIc"] > 0, "the sign rule is what passed"
    assert abs(increment["residualIc"]) >= increment["minAbsRankIc"]
    # The increment's own grid and nothing wider: the search window of this fixture holds 31
    # cross-sections (200 bars, every fifth one, 70% of them searched), of which 25 are measured.
    # Five of the six the pool could not be read on are the window's opening dates, where the
    # candidate's own 23-bar lookback reaches back past the start of the panel; the sixth is its
    # last date, whose forward return exits after ``search_end`` and so is not in the window at
    # all. Reading the fetched panel instead of that window would measure the last one off the
    # frozen tail and count the rest of the tail as sessions the pool could not be measured on.
    assert increment["usedCrossSections"] == 25
    assert increment["skippedCrossSections"] == 6
    assert increment["poolSize"] == 1 and increment["complete"] is True
    assert increment["compared"] == [_MEMBER] and increment["skipped"] == []


def test_a_candidate_without_increment_is_rejected_at_l6():
    """A member that reproduces the candidate leaves nothing of it, and L6 retires it by its key.

    The member is the survivor's own expression, so the orthogonalization removes the candidate
    exactly: the residual's mean is zero to the last bit while the candidate's own rank IC is a
    strong one, which is what makes this the residual's doing rather than a weak signal's. L5's
    frozen measurement stays on the record -- L6 adds a verdict, it does not erase the one before
    it -- and the reason is L6's own key, never an L5 or L4 one.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    member = _evaluator(panel[0], survivor["expression"])
    engine, _provider, config = _searched(panel, _pool({_MEMBER: member}))

    reviewed = _review(engine, config, [survivor])

    assert [item["status"] for item in reviewed] == ["rejected"]
    assert [item["reason"] for item in reviewed] == ["factorMining.reject.noIncrementalValue"]
    out_of_sample = reviewed[0]["outOfSample"]
    assert out_of_sample["icSeries"], "L5 measured this survivor before L6 retired it"
    increment = out_of_sample["poolIncrement"]
    assert increment["hasIncrementalValue"] is False
    assert increment["residualIc"] == 0.0
    assert abs(increment["rawIc"]) > 0.0, "a candidate with no signal of its own would prove nothing"


def test_l6_runs_after_l5_and_never_reads_the_freeze_segment():
    """The increment is a search-period judgment, asked only of a survivor L5 has cleared.

    Two pins in one. Isolation: one search, two frozen panels that disagree everywhere, and L6
    comes back byte for byte the same while L5's reading moves with the panel it was handed. The
    increment is measured on the search window, which is the only panel the run left behind, and
    the reading asserted non-empty below is what keeps the equality from being the trivial one of
    two unmeasured candidates. Order: a survivor L5 refuses carries no increment at all -- not an
    empty one, no key -- because the stage that asks whether the candidate is worth the pool is
    only reached by the candidates the stage before it kept.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    engine, provider, config = _searched(panel, _pool({_MEMBER: _unrelated_panel(panel[0])}))

    first = _review(engine, config, [survivor])
    refused = _review(engine, _config(thresholds=_STRICT_OOS), [survivor])
    provider.frozen = _search_panel(_drifted(panel))
    second = _review(engine, config, [survivor])

    assert [item["status"] for item in first] == ["frozen"] == [item["status"] for item in second]
    assert first[0]["outOfSample"]["poolIncrement"]["hasIncrementalValue"] is True, \
        "an unmeasured increment would satisfy the equality below without measuring anything"
    assert first[0]["outOfSample"]["rankIc"] != second[0]["outOfSample"]["rankIc"], \
        "the rewritten tail must move L5's reading for the equality below to mean anything"
    assert first[0]["outOfSample"]["poolIncrement"] == second[0]["outOfSample"]["poolIncrement"]

    assert [item["reason"] for item in refused] == ["factorMining.reject.weakOutOfSample"]
    assert "poolIncrement" not in refused[0]["outOfSample"]


def test_an_empty_pool_leaves_candidates_frozen():
    """An empty library is a question that was never asked: nothing is measured, nothing is retired.

    The increment is ``None`` here, not ``False``, and the two are not interchangeable: a review
    branching on the flag's truthiness retires every candidate it never measured, under a reason
    key that is not even a rejection. Nothing is measured, so nothing is retired -- and the reason
    names the empty pool rather than one of the rejections.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    engine, _provider, config = _searched(panel, ComparisonSet())

    reviewed = _review(engine, config, [survivor])

    assert [item["status"] for item in reviewed] == ["frozen"]
    assert [item["reason"] for item in reviewed] == [""]
    increment = reviewed[0]["outOfSample"]["poolIncrement"]
    assert increment["hasIncrementalValue"] is None
    assert increment["residualIc"] is None and increment["rawIc"] is None
    assert increment["reason"] == "factorMining.poolIncrement.noPool"
    assert not increment["reason"].startswith("factorMining.reject.")
    assert increment["poolSize"] == 0 and increment["compared"] == [] and increment["skipped"] == []


def test_a_pool_the_run_could_only_partly_rebuild_says_so():
    """A member nothing could be rebuilt for is a hole in the comparison, and the record shows it.

    ``pool_panels`` hands such a member back as a skip, outside the panels the measurement reads
    at all, so the reading below is taken against one member of a pool of two. A reader has to be
    able to see that: the count next to the names is what tells a clearance of a pool of two that
    was compared whole from one of a pool of two that was compared by half, and ``complete`` is
    the same fact said outright instead of left to whoever counts the list.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    comparison = _pool(
        {_MEMBER: _unrelated_panel(panel[0])},
        skipped=({"fingerprint": "a" * 64, "reason": "factorMining.reject.invalid"},),
    )
    engine, _provider, config = _searched(panel, comparison)

    reviewed = _review(engine, config, [survivor])

    increment = reviewed[0]["outOfSample"]["poolIncrement"]
    assert increment["poolSize"] == 2, "the pool the caller holds, not the part that rebuilt"
    assert increment["compared"] == [_MEMBER]
    assert increment["skipped"] == ["pool:aaaaaaaa"]
    assert increment["complete"] is False
    assert increment["hasIncrementalValue"] is True


def test_a_pool_that_could_not_be_rebuilt_at_all_is_not_reported_as_an_empty_library():
    """Every member unreadable is a library nothing could be read off, not a caller without one.

    The whole pool travels as skips, so the measurement is handed an empty mapping and answers
    with ``noPool`` -- the key that sends an operator looking for a library that is right there,
    full, and unreadable. The size and the skipped names are what say which of the two facts this
    is, and the reason is the other unmeasured key: the question was asked and could not be
    answered, which is not a rejection either.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    comparison = ComparisonSet(skipped=tuple(
        {"fingerprint": fingerprint * 64, "reason": "factorMining.reject.invalid"}
        for fingerprint in "ab"
    ))
    engine, _provider, config = _searched(panel, comparison)

    reviewed = _review(engine, config, [survivor])

    assert [item["status"] for item in reviewed] == ["frozen"]
    increment = reviewed[0]["outOfSample"]["poolIncrement"]
    assert increment["poolSize"] == 2
    assert increment["compared"] == []
    assert increment["skipped"] == ["pool:aaaaaaaa", "pool:bbbbbbbb"]
    assert increment["hasIncrementalValue"] is None
    assert increment["reason"] == "factorMining.poolIncrement.unmeasured"
    assert increment["reason"] != "factorMining.poolIncrement.noPool"


def test_a_pool_nothing_can_be_measured_off_never_retires_a_candidate():
    """A member that rebuilt and carries no values is a missing measurement, not a failed one.

    The pool is there and was compared against, and no session of the search window could be read
    off it, so the increment comes back unmeasured a second way -- with the member named, which is
    what tells this apart from the empty library above. Only a measured ``False`` retires: the
    candidate keeps the status L5 gave it.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    blank = _evaluator(panel[0], _MOMENTUM_5) * float("nan")
    engine, _provider, config = _searched(panel, _pool({_MEMBER: blank}))

    reviewed = _review(engine, config, [survivor])

    assert [item["status"] for item in reviewed] == ["frozen"]
    increment = reviewed[0]["outOfSample"]["poolIncrement"]
    assert increment["poolSize"] == 1 and increment["compared"] == [_MEMBER]
    assert increment["hasIncrementalValue"] is None
    assert increment["residualIc"] is None
    assert increment["reason"] == "factorMining.poolIncrement.unmeasured"
    assert increment["usedCrossSections"] == 0 and increment["skippedCrossSections"] > 0


def test_a_review_without_a_search_behind_it_does_not_report_an_empty_library():
    """A review that never read a pool is not a caller whose library is empty.

    L6's inputs are assembled by the search, so a review run on an engine that never searched has
    no search window to measure on at all. That is the third fact again: nothing was compared
    against, and it is neither the empty library -- ``noPool``, which an operator reads as "this
    caller has no library yet" -- nor a pool that could not be read. Whether or not anything can
    replace the missing window, the survivor is not retired for it: ``None`` is not a verdict.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    engine = FactorMiningEngine(data_provider=_FakeProvider(*panel))

    reviewed = _review(engine, _config(), [survivor])

    assert [item["status"] for item in reviewed] == ["frozen"]
    increment = reviewed[0]["outOfSample"]["poolIncrement"]
    assert increment["hasIncrementalValue"] is None
    assert increment["reason"] == "factorMining.poolIncrement.unmeasured"
    assert increment["reason"] != "factorMining.poolIncrement.noPool"


def test_the_increment_is_measured_inside_the_search_window_whatever_the_source_returns():
    """Bars past the window's end cannot reach the reading, whether the source sends them or not.

    A forward return is read *at* a date and takes its exit price from bars after it, so the last
    cross-sections of a window are the ones a source that over-returns would fill from the frozen
    tail -- the tail the review measures L5 on, which may not also be what L6 reads. The research
    engine's window cut is what stops that, and this reads the increment through it: the same
    survivor, one search over frames that carry on into the tail and one over frames that stop at
    ``search_end``, has to come back with the same reading, the same counts and the same verdict.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    comparison = _pool({_MEMBER: _unrelated_panel(panel[0])})
    end = split_periods(_config()).search_end

    carried_on, _provider, carried_on_config = _searched(panel, comparison)
    stopped, _stopped_provider, stopped_config = _searched(
        _truncated(panel, end), comparison, frozen=panel)

    first = _review(carried_on, carried_on_config, [survivor])[0]["outOfSample"]["poolIncrement"]
    second = _review(stopped, stopped_config, [survivor])[0]["outOfSample"]["poolIncrement"]

    assert first["hasIncrementalValue"] is True, "a reading, so the equality below is not vacuous"
    assert first == second


def test_a_holding_period_that_cannot_go_forward_is_clamped_like_the_research_engines():
    """A period the payload got wrong cannot turn the forward return pastward.

    ``holdingPeriod`` reaches the config without a lower bound, and the shift that builds a forward
    return is negative: at a period of ``-3`` the exit price lands *before* the entry, and the
    increment would be a measured verdict read off a series running backwards -- candidates retired
    for a sign the engine never looked at. The research engine clamps the period once and reads
    every window with ``max(1, int(period or 5))``; the increment has to read the same window, so a
    period below one and a period of one are one reading.
    """
    panel = _oscillating_fixture()
    survivor = _survivors(panel=panel)[0]
    comparison = _pool({_MEMBER: _unrelated_panel(panel[0])})

    backwards, _provider, backwards_config = _searched(
        panel, comparison, config=_config(budget=1, seed=_SEED, holding_period=-3))
    forward, _other, forward_config = _searched(
        panel, comparison, config=_config(budget=1, seed=_SEED, holding_period=1))

    first = _review(backwards, backwards_config, [survivor])[0]["outOfSample"]["poolIncrement"]
    second = _review(forward, forward_config, [survivor])[0]["outOfSample"]["poolIncrement"]

    assert first["hasIncrementalValue"] is True, "a reading, so the equality below is not vacuous"
    assert first == second


# The pool member the provider-path test loads. Its name is the ``pool:<fingerprint[:8]>`` the
# engine builds from a stored row's hash, so the reading's ``compared`` list is this.
_POOL_MEMBER_HASH = "deadbeef" + "0" * 56
_POOL_MEMBER = "pool:deadbeef"


class _OverReturningSource(_FakeBacktestService):
    """A source that keeps sending bars past the end it was asked for -- or one that stops there.

    ``_FakeBacktestService`` builds its index from ``[start_date, end_date]``, so it cannot show
    this shape at all: a real source is not obliged to stop at the end it was asked for (the loader
    hands back the range it has closed), and ``MiningDataProvider`` passes on whatever it is given
    -- ``_prepare_window`` does not clip. Everything that keeps those bars out of the increment
    therefore lives below the provider, in the search window's prepared frames, and a panel a test
    trimmed itself (the engine tests) never travels that path. ``clip`` is the only difference
    between the two runs, so the readings are comparable by construction.
    """

    def __init__(self, frames, *, clip: bool):
        super().__init__()
        self.frames = frames
        self.clip = clip

    def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
        return ([{"key": key, "market": "USStock", "symbol": key.split(":", 1)[1],
                  "effectiveFrom": None} for key in sorted(self.frames)], 42)

    def fetch_frames(self, candidates, frequency, start_date, end_date):
        self.fetch_calls.append((frequency, start_date, end_date))
        frames = {}
        for key, frame in self.frames.items():
            index = frame.index
            window = (index >= start_date) & (index <= end_date) if self.clip else index >= start_date
            frames[key] = frame.loc[window]
        return frames, []


def _review_through_the_provider(frames, *, clip):
    """One reviewed survivor, with the search panel built by the real provider.

    The pool is injected into the provider rather than handed to the engine as a
    ``ComparisonSet``, because travelling the production path is the point: ``prepare`` builds the
    panel from what the source sent, ``comparison_set`` rebuilds the stored member against those
    very field panels, and the review's L6 reads them. The member is a real factor shape over the
    fixture's own columns and it explains the candidate only partly, so the reading below is a
    measurement rather than the pool reproducing the candidate exactly; everything but the
    de-duplication bars keeps the fixture's default thresholds, and both L4 bars are raised so the
    run has a genuine pass path into L6 (``_NO_DEDUPLICATION``'s rule, on the other side of the
    provider).
    """
    provider = MiningDataProvider(
        backtest_service=_OverReturningSource(frames, clip=clip),
        pool_loader=lambda *_args, **_kwargs: [
            PoolMember(candidate_id=1, fingerprint=_POOL_MEMBER_HASH,
                       expression={"op": "div", "args": ["close", "volume"], "params": {}}),
        ],
    )
    config = _provider_config(
        start_date=datetime(2024, 1, 1), end_date=datetime(2024, 9, 30), budget=1, seed=_SEED,
        thresholds=GateThresholds(
            min_coverage=0.0, min_ic_observations=5, min_abs_rank_ic=0.0, min_icir=0.0,
            min_fold_consistency=0.0, max_turnover=1.0, max_builtin_correlation=1.1,
            max_pool_correlation=1.1, min_oos_abs_rank_ic=0.0,
            min_deflated_sharpe_probability=0.0,
        ),
    )
    engine = FactorMiningEngine(data_provider=provider)
    result = engine.run(user_id=1, code=_SOURCE, config=config)
    survivors = _survivor_records(result["trials"], expand_candidates(budget=1, seed=_SEED))
    return engine.review_frozen(user_id=1, code=_SOURCE, config=config, survivors=survivors,
                                trials_count=len(result["trials"]))[0]


def test_a_source_that_over_returns_cannot_move_the_increment():
    """The trim that keeps the frozen tail out of L6 has to hold at the provider's own seam.

    The engine tests hand L6 a panel they trimmed themselves, which pins the *contract*. In
    production the bars arrive through ``MiningDataProvider``, which does not trim what a source
    sent, so the panel carries the tail even though every caller asked for the search window
    alone -- the defensive layer worth pinning on its own. One run's source stops at the end it
    was asked for, the other's keeps going into the fixture's frozen tail, and the two increments
    have to be the same block: same residual IC, same counts, same verdict. Delete the trim the
    increment reads its frames through and only the second run moves.
    """
    frames, _members = _oscillating_fixture()

    stopped = _review_through_the_provider(frames, clip=True)
    carried_on = _review_through_the_provider(frames, clip=False)

    assert stopped["outOfSample"]["icSeries"], "L5 measured this candidate, so L6 ran"
    stopped_increment = stopped["outOfSample"]["poolIncrement"]
    carried_on_increment = carried_on["outOfSample"]["poolIncrement"]
    assert stopped_increment["compared"] == [_POOL_MEMBER], \
        "the pool member really was measured, not skipped"
    assert stopped_increment["complete"] is True and stopped_increment["usedCrossSections"] > 0
    assert stopped_increment["hasIncrementalValue"] is True, \
        "a reading, so the equality below is not vacuous"
    assert carried_on_increment == stopped_increment


def test_an_increment_that_cannot_be_measured_never_ends_the_review(monkeypatch):
    """The increment's guard covers everything the measurement does, not only the evaluation.

    ``residual_ic`` is handed panels the review did not build, and a measurement that raises from
    inside it would travel out of the review and into the job -- failing a run whose candidates
    were already judged, which is the opposite of what the guards around every other evaluation in
    this engine are for. One candidate whose increment cannot be measured is a missing measurement:
    no verdict, no rejection, and the review carries on to the survivors behind it.
    """
    panel = _oscillating_fixture()
    survivors = _survivors(count=2, panel=panel)

    def _explode(*args, **kwargs):
        raise RuntimeError("the measurement blew up")

    monkeypatch.setattr(engine_module, "residual_ic", _explode)
    engine, _provider, config = _searched(panel, _pool({_MEMBER: _unrelated_panel(panel[0])}))

    reviewed = _review(engine, config, survivors)

    assert len(reviewed) == len(survivors), "the review carried on past the measurement that raised"
    # Not every seed of this fixture clears L5, so the assertion is on the survivors that reached
    # L6 -- and there has to be one, or the guard was never exercised.
    increments = [item["outOfSample"]["poolIncrement"] for item in reviewed
                  if item["status"] == "frozen"]
    assert increments, "no survivor reached L6, so this test measured nothing"
    assert [item["hasIncrementalValue"] for item in increments] == [None] * len(increments)
    assert [item["reason"] for item in increments] == [
        "factorMining.poolIncrement.unmeasured"] * len(increments)
    assert all(item["reason"] != "factorMining.reject.noIncrementalValue" for item in reviewed)
