"""Factor pool incremental value: what a candidate carries that the pool does not already hold.

Every panel here is synthetic -- no store, no provider, no clock -- because the module under test
is a pure function of three panels. Expectations are built outside it rather than read back from
it: a candidate that is a strictly monotone transform of a pool member *is* that member in rank
space, a candidate drawn independently of the pool keeps its signal after the orthogonalization,
and a candidate whose two halves track the returns from opposite sides lands where the sign rule
says it must.

The frozen tail appears once, in the segment test, as the rows a caller must not hand over: the
search-period judgment is the caller's slice to make, and that test is what shows it matters.
"""

import numpy as np
import pandas as pd

from app.services.factor_mining import incremental
from app.services.factor_mining.gates import GateThresholds

SYMBOLS = [f"S{index:02d}" for index in range(30)]
SEARCH_DATES = pd.bdate_range("2025-01-01", periods=40)
FREEZE_DATES = pd.bdate_range(SEARCH_DATES[-1] + pd.Timedelta(days=1), periods=20)
MEMBER = "member-fingerprint"


def _panel(index, rng) -> pd.DataFrame:
    """One synthetic factor panel: independent draws, one column per symbol, one row per date."""
    return pd.DataFrame(
        rng.normal(size=(len(index), len(SYMBOLS))), index=index, columns=SYMBOLS,
    )


def test_pool_gain_is_measured_without_the_freeze_segment():
    """The frozen tail cannot reach the verdict: this module reads only the panels it is given.

    Incremental value is a search-period question, so the caller hands over the in-sample and
    validation rows and nothing else. Two datasets identical inside the search window and wildly
    different after it come back byte for byte the same -- and the same noised panels handed over
    whole do not, so the equality is not the trivial one of two identical inputs: the tail is
    live data that a module reaching past its arguments (a rolling warmup, a panel-wide
    normalization) would read.
    """
    rng = np.random.default_rng(2026)
    full = SEARCH_DATES.append(FREEZE_DATES)
    member = _panel(full, rng)
    candidate = _panel(full, rng)
    returns = 0.9 * candidate + _panel(full, rng)
    noisy = [frame.copy() for frame in (member, candidate, returns)]
    for frame in noisy:
        frame.loc[FREEZE_DATES] = _panel(FREEZE_DATES, rng)
    noisy_member, noisy_candidate, noisy_returns = noisy
    end = SEARCH_DATES[-1]

    search_only = incremental.residual_ic(
        candidate.loc[:end], {MEMBER: member.loc[:end]}, returns.loc[:end],
    )
    noised_search_only = incremental.residual_ic(
        noisy_candidate.loc[:end], {MEMBER: noisy_member.loc[:end]}, noisy_returns.loc[:end],
    )
    whole = incremental.residual_ic(
        noisy_candidate, {MEMBER: noisy_member}, noisy_returns,
    )

    assert search_only.measured
    assert search_only == noised_search_only
    assert repr(search_only) == repr(noised_search_only)
    assert whole != search_only


def test_incremental_value_rejects_a_factor_the_pool_already_explains():
    """A monotone transform of a pool member is that member: no residual, no incremental value.

    ``exp`` reorders nothing, so the candidate's ranks are the member's ranks exactly and the
    orthogonalization leaves a residual that is zero to the last bit -- while the candidate's own
    rank IC is a strong one, so the rejection is the residual's doing and not a weak signal's.
    It is also what pins the regression to ranks: against the member's raw values, ``exp(member)``
    is not explained at all and would be kept.
    """
    rng = np.random.default_rng(31)
    member = _panel(SEARCH_DATES, rng)
    returns = 0.8 * member + _panel(SEARCH_DATES, rng)

    result = incremental.residual_ic(np.exp(member), {MEMBER: member}, returns)

    assert result.measured
    assert abs(result.raw_ic) > 0.5
    assert result.residual_ic == 0.0
    assert result.has_incremental_value is False
    assert result.reason == "factorMining.reject.noIncrementalValue"


def test_every_pool_member_is_orthogonalized_against_not_only_one_of_them():
    """The comparison is the pool as a span, not the best single member.

    The candidate is a monotone transform of the *second* member, with an unrelated member drawn
    before it, so an implementation that regressed against one member of the pool -- the first,
    the newest, the most correlated of them -- would leave the candidate's whole signal standing
    and keep it. The pool is what a candidate has to add to, and one member is not the pool.
    """
    rng = np.random.default_rng(33)
    explaining = _panel(SEARCH_DATES, rng)
    decoy = _panel(SEARCH_DATES, rng)
    returns = 0.8 * explaining + _panel(SEARCH_DATES, rng)

    result = incremental.residual_ic(
        np.exp(explaining), {"decoy-fingerprint": decoy, MEMBER: explaining}, returns,
    )

    assert result.measured
    assert abs(result.raw_ic) > 0.5
    assert result.residual_ic == 0.0
    assert result.has_incremental_value is False
    assert result.reason == "factorMining.reject.noIncrementalValue"


def test_incremental_value_keeps_an_orthogonal_factor():
    """A candidate the pool cannot explain keeps its signal: the residual IC is the raw IC.

    The member is drawn independently of both the candidate and the returns, so the
    orthogonalization removes almost nothing of the candidate's rank IC and the candidate is kept
    with the value the residual carries -- not with the raw reading it came in on.
    """
    rng = np.random.default_rng(41)
    member = _panel(SEARCH_DATES, rng)
    candidate = _panel(SEARCH_DATES, rng)
    returns = 0.9 * candidate + _panel(SEARCH_DATES, rng)

    result = incremental.residual_ic(candidate, {MEMBER: member}, returns)

    assert result.measured and result.has_incremental_value is True
    assert result.reason == ""
    assert abs(result.residual_ic) >= 0.02
    assert result.residual_ic * result.raw_ic > 0
    assert abs(result.residual_ic - result.raw_ic) < 0.1


def test_residual_ic_requires_the_same_sign_as_the_raw_ic():
    """A residual that flipped direction reads as noise, however strong its magnitude.

    The returns are driven by ``signal``, the pool member carries that signal too, and the
    candidate is the member plus a piece that tracks the returns *against* them. Removing the
    member leaves the opposing piece, so the residual IC clears the magnitude bar on the far side
    of zero -- the shape of an overfit rather than of information. On magnitude alone this
    candidate passes.
    """
    rng = np.random.default_rng(51)
    signal = _panel(SEARCH_DATES, rng)
    returns = signal + _panel(SEARCH_DATES, rng)
    member = signal + 0.75 * _panel(SEARCH_DATES, rng)
    opposing = -signal + _panel(SEARCH_DATES, rng)
    candidate = member + 0.6 * opposing

    result = incremental.residual_ic(candidate, {MEMBER: member}, returns)

    assert result.measured
    assert result.raw_ic > 0.15
    assert result.residual_ic < -0.05
    assert abs(result.residual_ic) >= 0.02
    assert result.has_incremental_value is False
    assert result.reason == "factorMining.reject.noIncrementalValue"


def test_an_empty_pool_short_circuits_to_no_measurement():
    """No pool is not "no value": the question was never asked, and the answer says so.

    A candidate is not worse for having nothing to compare against, so the empty pool is neither
    verdict: nothing is measured, both readings stay absent, the value flag is ``None`` rather
    than the ``False`` a failed measurement wears, and the reason is no rejection -- a caller
    reading this as "no incremental value" would retire a candidate on no evidence. The reason
    names the empty pool itself, so an operator is not left comparing two counts to find out
    whether the library is empty or its members could not be measured.
    """
    rng = np.random.default_rng(61)
    candidate = _panel(SEARCH_DATES, rng)

    result = incremental.residual_ic(
        candidate, {}, 0.9 * candidate + _panel(SEARCH_DATES, rng),
    )

    assert result.measured is False
    assert result.has_incremental_value is None
    assert result.residual_ic is None and result.raw_ic is None
    assert result.used_cross_sections == 0 and result.skipped_cross_sections == 0
    assert result.reason == "factorMining.poolIncrement.noPool"
    assert not result.reason.startswith("factorMining.reject.")


def test_too_few_usable_cross_sections_is_unmeasured_not_a_rejection():
    """A comparing set nothing could be read off is a missing measurement, not a verdict.

    The member was rebuilt and carries values in one session only, and the mean of a single IC is
    that IC, so there is no incremental reading to compare against the bar. What comes back is
    the *other* unmeasured reason -- the pool is there and could not be measured, which is not the
    same fact as a library that is empty -- and rejection of no kind comes out of a measurement
    that never happened.
    """
    rng = np.random.default_rng(71)
    candidate = _panel(SEARCH_DATES, rng)
    returns = 0.9 * candidate + _panel(SEARCH_DATES, rng)
    member = pd.DataFrame(np.nan, index=SEARCH_DATES, columns=SYMBOLS)
    member.loc[SEARCH_DATES[-1]] = rng.normal(size=len(SYMBOLS))

    result = incremental.residual_ic(candidate, {MEMBER: member}, returns)

    assert result.measured is False
    assert result.has_incremental_value is None
    assert result.residual_ic is None and result.raw_ic is None
    assert result.used_cross_sections == 1
    assert result.skipped_cross_sections == len(SEARCH_DATES) - 1
    assert result.reason == "factorMining.poolIncrement.unmeasured"
    assert not result.reason.startswith("factorMining.reject.")


def test_a_section_the_pool_cannot_be_measured_on_is_skipped_and_counted():
    """One dead session does not swallow the judgment, and what it cost is reported.

    A member with no values in ten of the forty sessions leaves those sessions with nothing to
    orthogonalize against; the remaining thirty still support a verdict, and both counts travel
    with it rather than one failure standing for the whole pool.
    """
    rng = np.random.default_rng(81)
    candidate = _panel(SEARCH_DATES, rng)
    member = _panel(SEARCH_DATES, rng)
    returns = 0.9 * candidate + _panel(SEARCH_DATES, rng)
    member.loc[SEARCH_DATES[:10]] = np.nan

    result = incremental.residual_ic(candidate, {MEMBER: member}, returns)

    assert result.measured is True
    assert result.has_incremental_value is True
    assert result.used_cross_sections == len(SEARCH_DATES) - 10
    assert result.skipped_cross_sections == 10


def test_a_candidate_with_no_values_anywhere_is_unmeasured_not_rejected():
    """A candidate nothing could be read on is not a candidate that carries nothing.

    A candidate that never evaluated has no session to be orthogonalized over -- the same state a
    member that never carries values leaves every session in: no reading, no verdict, and no
    rejection key. The invalid candidate is the engine's to reject, and on its own evidence rather
    than this module's.
    """
    rng = np.random.default_rng(91)
    member = _panel(SEARCH_DATES, rng)

    result = incremental.residual_ic(
        pd.DataFrame(np.nan, index=SEARCH_DATES, columns=SYMBOLS),
        {MEMBER: member},
        0.9 * member + _panel(SEARCH_DATES, rng),
    )

    assert result.measured is False
    assert result.has_incremental_value is None
    assert result.residual_ic is None and result.raw_ic is None
    assert result.used_cross_sections == 0
    assert result.skipped_cross_sections == len(SEARCH_DATES)
    assert result.reason == "factorMining.poolIncrement.unmeasured"
    assert not result.reason.startswith("factorMining.reject.")


def test_the_incremental_bar_is_the_l2_bar():
    """The default bar restates L2's, and the two spellings must stay one number.

    It is restated rather than imported for the reason the pool restates the invalid-expression
    key: this module is pure computation and importing the thresholds module would pull the whole
    gate ladder in behind it. That is exactly why the equality needs pinning -- a run whose L2 bar
    moved while the incremental bar stayed put would demand a candidate clear one height to enter
    the ladder and another to survive it, with nothing to notice the drift.
    """
    assert incremental.DEFAULT_MIN_ABS_RANK_IC == GateThresholds().min_abs_rank_ic


def _p1_rotating_members():
    dates = pd.date_range("2025-01-01", periods=4)
    member = pd.DataFrame([[1., 2., 3., 4., 5., -1e12]] * 4,
                          index=dates, columns=["A", "B", "C", "D", "E", "outsider"])
    candidate = member * 10
    returns = member.copy()
    candidate["outsider"] = 1e12
    returns["outsider"] = 1e12
    candidate.loc[dates[:2], "E"] = 1e12
    member.loc[dates[:2], "E"] = -1e12
    returns.loc[dates[:2], "E"] = -1e12
    candidate.loc[dates[2:], "D"] = -1e12
    member.loc[dates[2:], "D"] = 1e12
    returns.loc[dates[2:], "D"] = 1e12
    membership = pd.DataFrame({"A": True, "B": True, "C": True,
                               "D": [True, True, False, False], "E": [False, False, True, True],
                               "outsider": False}, index=dates)
    return candidate, member, returns, membership


def test_p1_l6_raw_and_residual_ic_only_use_current_members_with_label_alignment():
    candidate, member, returns, membership = _p1_rotating_members()
    # A pool-only column and reversed axes cannot enlarge or reorder the candidate's universe.
    member["pool_only"] = 1e15
    returns["pool_only"] = -1e15
    unfiltered = incremental.residual_ic(candidate, {MEMBER: member}, returns)
    result = incremental.residual_ic(candidate, {MEMBER: member.iloc[::-1, ::-1]},
                                     returns.iloc[::-1, ::-1], membership=membership.iloc[::-1, ::-1])

    assert unfiltered.measured and abs(unfiltered.raw_ic - 1.) > 0.1
    assert result.measured and result.has_incremental_value is False
    assert np.isclose(result.raw_ic, 1.)
    assert result.residual_ic == 0.
    assert result.used_cross_sections == 4 and result.skipped_cross_sections == 0
    assert result.reason == incremental.REJECT_NO_INCREMENTAL_VALUE


def test_p1_l6_does_not_use_outsiders_to_fill_an_insufficient_cross_section():
    candidate, member, returns, membership = _p1_rotating_members()
    membership.loc[:, ["C", "D", "E"]] = False
    result = incremental.residual_ic(candidate, {MEMBER: member}, returns, membership=membership)

    assert result.measured is False and result.has_incremental_value is None
    assert result.raw_ic is None and result.residual_ic is None
    assert result.used_cross_sections == 0 and result.skipped_cross_sections == 4
    assert result.reason == incremental.UNMEASURED_REASON


def test_p1_l6_missing_membership_dates_or_symbols_are_inactive():
    candidate, member, returns, membership = _p1_rotating_members()
    membership = membership.drop(index=membership.index[0], columns="outsider")
    result = incremental.residual_ic(candidate, {MEMBER: member}, returns, membership=membership)

    assert result.measured and np.isclose(result.raw_ic, 1.)
    assert result.residual_ic == 0.
    assert result.used_cross_sections == 3 and result.skipped_cross_sections == 1
