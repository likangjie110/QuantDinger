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
from datetime import timedelta

import pytest

from app.services.backtest_limits import backtest_warmup_calendar_days
from app.services.factor_mining import engine as engine_module
from app.services.factor_mining.engine import (
    FactorMiningEngine,
    MiningDataProvider,
    PeriodSplit,
    _warmup_bars,
    split_periods,
)
from app.services.factor_mining.gates import GateThresholds, GateVerdict, net_return_series
from app.services.factor_mining.templates import expand_candidates
from app.utils import agent_jobs
from tests.test_factor_mining_data import (
    _FakeBacktestService,
    _SOURCE,
    _config as _provider_config,
)
from tests.test_factor_mining_engine import (
    _FakeProvider,
    _config,
    _frames_and_members,
    _fixture,
    _run,
    _seed_for,
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
