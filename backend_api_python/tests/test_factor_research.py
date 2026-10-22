import pandas as pd
import pytest

from app.services.strategy_v2.factor_research import FactorResearchEngine


@pytest.mark.parametrize("stop_after", ["metrics", "stability", "correlation", None])
def test_p2_research_stages_are_ordered_and_preserve_full_response(stop_after):
    frames, index = _comparable_frames()
    arguments = dict(frames=frames, factor_id="momentum_20", start_date=index[0],
                     end_date=index[-1], groups=3, holding_period=5,
                     commission=0.001, slippage=0.002, neutralize_industry=True)
    full = FactorResearchEngine().run(**arguments)
    stages = []

    def on_stage(stage, metrics):
        stages.append(stage)
        assert "icSeries" in metrics
        assert "groupCurves" not in metrics and "longShortCurve" not in metrics
        return stage != stop_after

    result = FactorResearchEngine().run(**arguments, on_stage=on_stage)
    expected = ["metrics", "stability", "correlation"]
    assert stages == (expected[:expected.index(stop_after) + 1] if stop_after else expected)
    assert result["rankIc"] == full["rankIc"]
    assert result["coverage"] == full["coverage"]
    if stop_after != "metrics":
        assert result["stability"] == full["stability"]
        assert result["averageTurnover"] == full["averageTurnover"]
    if stop_after:
        assert "longShortCurve" not in result
    else:
        assert result == full


def _comparable_frames(periods=90, symbols=("A", "B", "C", "D", "E", "F")):
    """Frames whose symbols rank the same way on every date, so correlations are known.

    Every price is an exact geometric curve, so any trend or reversal reading of ``close``
    orders the six symbols identically and the built-in proxies correlate with each other at
    plus or minus one -- which is what lets a test assert a name's presence or absence without
    inventing a tolerance for it.
    """
    index = pd.date_range("2025-01-01", periods=periods, freq="B")
    frames = {}
    for offset, symbol in enumerate(symbols):
        prices = [100 + offset * 3 + day * (0.1 + offset * 0.02) for day in range(periods)]
        frames[f"USStock:{symbol}"] = pd.DataFrame({
            "open": prices,
            "high": [value * 1.01 for value in prices],
            "low": [value * 0.99 for value in prices],
            "close": prices,
            "volume": [100000.0] * periods,
            "industry": ["Tech" if offset < 3 else "Finance"] * periods,
        }, index=index)
    return frames, index


def test_the_correlation_study_accepts_an_injected_comparison_set():
    """An injected set is the whole comparison set: the class constant is not consulted.

    The study is asked to measure a candidate against what the caller named -- the mining
    search's registry factors and candidate pool -- so the proxies it happens not to have
    injected must be absent, not quietly added back beside them.
    """
    frames, index = _comparable_frames()

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[0],
        end_date=index[-1],
        groups=3,
        holding_period=5,
        comparison_factors=("volatility_20", "reversal_5"),
    )

    names = result["factorCorrelation"]["factors"]
    assert names == ["momentum_20", "volatility_20", "reversal_5"]
    assert "value" not in names and "size" not in names


def test_the_default_comparison_set_is_unchanged():
    """Without an injection the six built-in proxies are compared, as a full matrix.

    Both halves are load bearing. The names are the contract every existing caller reads; the
    square matrix is what the research page renders, and an injected set deliberately fills
    the candidate's row alone, so a default that lost its other rows would be a silent break.
    """
    frames, index = _comparable_frames()

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[0],
        end_date=index[-1],
        groups=3,
        holding_period=5,
    )

    block = result["factorCorrelation"]
    assert block["factors"][0] == "momentum_20"
    assert set(block["factors"][1:]) <= set(FactorResearchEngine._CORRELATION_FACTORS)
    assert {"volatility_20", "reversal_5"} <= set(block["factors"])
    size = len(block["factors"])
    assert len(block["matrix"]) == size and len(block["observations"]) == size
    assert all(len(row) == size for row in block["matrix"])
    assert block["matrix"][0][1] == pytest.approx(block["matrix"][1][0])


def test_an_injected_comparison_set_fills_only_the_candidates_row():
    """An injected set is the candidate's comparison set, not a peer group.

    Only the candidate's row is measured, and the observation matrix says the rest was never
    measured rather than reporting a zero for it. That is what bounds the mining search's cost:
    the registry and the pool are tens of factors, and every candidate would otherwise pay for
    a quadratic number of cross-sectional Spearman runs whose other rows nobody reads.
    """
    frames, index = _comparable_frames()

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[0],
        end_date=index[-1],
        groups=3,
        holding_period=5,
        comparison_factors=("volatility_20", "reversal_5"),
    )

    block = result["factorCorrelation"]
    assert block["observations"][0][0] > 0
    assert block["observations"][0][1] > 0
    assert block["observations"][1][0] == 0
    assert block["matrix"][1][0] == 0.0


def test_an_injected_panel_is_measured_against_an_injected_name():
    """A caller's own panel and a caller's own comparison set travel together.

    The mining search hands in the candidate's panel under the neutral ``expression`` name and
    the comparison factors beside it. If the injected panels were only consulted for the
    selected factor, every comparison name would be regenerated as a built-in -- for a registry
    id that is not a built-in at all, the panel would come back empty and the name would drop
    out of the study without a word.
    """
    frames, index = _comparable_frames()
    engine = FactorResearchEngine()
    _, dates = engine.research_window(frames=frames, start_date=index[0], end_date=index[-1],
                                      holding_period=5)
    candidate = pd.DataFrame(
        {symbol: [float(offset) + day for day in range(len(dates))]
         for offset, symbol in enumerate(sorted(frames))},
        index=dates,
    )

    result = engine.run(
        frames=frames,
        factor_id="",
        factor_panel=candidate,
        start_date=index[0],
        end_date=index[-1],
        groups=3,
        holding_period=5,
        comparison_factors=("rsi", "pool:01234567"),
        comparison_panels={"rsi": candidate * -1.0, "pool:01234567": candidate},
    )

    assert result["factorCorrelation"]["factors"] == ["expression", "rsi", "pool:01234567"]
    assert result["factorCorrelation"]["matrix"][0][1] == pytest.approx(-1.0, abs=1e-9)
    assert result["factorCorrelation"]["matrix"][0][2] == pytest.approx(1.0, abs=1e-9)


def test_factor_research_returns_ic_groups_costs_and_stability():
    index = pd.date_range("2025-01-01", periods=90, freq="B")
    frames = {}
    for offset, symbol in enumerate(["A", "B", "C", "D", "E", "F"]):
        prices = [100 + offset * 3 + day * (0.1 + offset * 0.02) for day in range(len(index))]
        frames[f"USStock:{symbol}"] = pd.DataFrame({
            "open": prices,
            "high": [value * 1.01 for value in prices],
            "low": [value * 0.99 for value in prices],
            "close": prices,
            "volume": [100000] * len(index),
            "industry": ["Tech" if offset < 3 else "Finance"] * len(index),
        }, index=index)

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[0],
        end_date=index[-1],
        groups=3,
        holding_period=5,
        commission=0.0005,
        slippage=0.0005,
        neutralize_industry=True,
    )

    assert result["icSeries"]
    assert len(result["groupCurves"]) == 3
    assert result["coverage"] > 0
    assert result["missingRate"] < 1
    assert result["neutralized"] is True
    assert result["factorCorrelation"]["factors"]
    assert "rankAutocorrelation" in result["stability"]


def test_factor_research_rejects_empty_cross_sectional_observations():
    index = pd.date_range("2025-01-01", periods=30, freq="B")
    frames = {
        f"USStock:{symbol}": pd.DataFrame({
            "open": [100.0] * len(index),
            "close": [100.0] * len(index),
        }, index=index)
        for symbol in ["A", "B", "C"]
    }

    with pytest.raises(ValueError, match="factorResearchInsufficientObservations"):
        FactorResearchEngine().run(
            frames=frames,
            factor_id="quality",
            start_date=index[0],
            end_date=index[-1],
            groups=3,
        )


def test_factor_research_preserves_warmup_before_selected_start():
    index = pd.date_range("2025-01-01", periods=70, freq="B")
    frames = {}
    for offset, symbol in enumerate(["A", "B", "C", "D", "E", "F"]):
        prices = [100 + offset + day * (0.1 + offset * 0.03) for day in range(len(index))]
        frames[symbol] = pd.DataFrame({"open": prices, "close": prices}, index=index)

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[25],
        end_date=index[-1],
        groups=3,
        holding_period=5,
    )

    assert pd.Timestamp(result["icSeries"][0]["time"]) == index[25]


def test_factor_research_applies_membership_intervals_on_each_research_date():
    index = pd.date_range("2025-01-01", periods=70, freq="B")
    symbols = ["A", "B", "C", "D", "E", "F"]
    frames = {}
    members = []
    for offset, symbol in enumerate(symbols):
        prices = [100 + offset + day * (0.1 + offset * 0.02) for day in range(len(index))]
        frames[symbol] = pd.DataFrame({"open": prices, "close": prices}, index=index)
        members.append({
            "key": symbol,
            "valid_from": index[0] if symbol in {"A", "B", "C"} else index[35],
            "valid_to": None,
        })

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[25],
        end_date=index[-1],
        groups=3,
        holding_period=5,
        members=members,
    )

    first_time = result["icSeries"][0]["time"]
    first_members = {
        symbol
        for row in result["groupObservations"]
        if row["time"] == first_time
        for symbol in row["members"]
    }
    assert first_members == {"A", "B", "C"}
    assert result["pointInTimeUniverseApplied"] is True


def test_factor_research_preserves_membership_gaps_and_reentry():
    index = pd.date_range("2025-01-01", periods=6, freq="D")

    panel = FactorResearchEngine._membership_panel(
        [{
            "key": "A",
            "membership_periods": [
                {"valid_from": index[0], "valid_to": index[2]},
                {"valid_from": index[4], "valid_to": None},
            ],
        }],
        index,
        pd.Index(["A", "B"]),
    )

    assert panel["A"].tolist() == [True, True, False, False, True, True]
    assert panel["B"].tolist() == [False] * len(index)


def test_factor_research_long_short_net_return_deducts_both_legs_costs():
    long_values = [{
        "time": "2025-01-01",
        "grossReturn": 0.10,
        "netReturn": 0.09,
        "cost": 0.01,
    }]
    short_values = [{
        "time": "2025-01-01",
        "grossReturn": -0.05,
        "netReturn": -0.07,
        "cost": 0.02,
    }]

    points = FactorResearchEngine._long_short_curve(long_values, short_values)

    assert points[0]["gross"] == pytest.approx(1.15)
    assert points[0]["net"] == pytest.approx(1.12)


def test_factor_research_reduces_groups_for_small_cross_sections():
    index = pd.date_range("2025-01-01", periods=70, freq="B")
    frames = {}
    for offset, symbol in enumerate(["A", "B", "C", "D", "E", "F", "G"]):
        prices = [100 + offset + day * (0.1 + offset * 0.02) for day in range(len(index))]
        frames[symbol] = pd.DataFrame({"open": prices, "close": prices}, index=index)

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="momentum_20",
        start_date=index[25],
        end_date=index[-1],
        groups=5,
        holding_period=5,
        annualization_periods=365.25,
    )

    assert result["effectiveGroups"] == 3
    assert result["requestedGroups"] == 5
    assert result["methodologyVersion"] == 2
    assert "groupsReduced" in result["sampleDiagnostics"]["warnings"]
    assert result["executionAssumptions"]["periodsPerYear"] == pytest.approx(365.25)


def test_factor_research_sizes_groups_from_usable_factor_cross_section():
    index = pd.date_range("2025-01-01", periods=40, freq="B")
    frames = {}
    for offset, symbol in enumerate("ABCDEFGHIJ"):
        prices = [100 + offset + day * (0.1 + offset * 0.02) for day in range(len(index))]
        frame = pd.DataFrame({"open": prices, "close": prices}, index=index)
        if offset < 4:
            frame["pe_ratio"] = 10.0 + offset
        frames[symbol] = frame

    result = FactorResearchEngine().run(
        frames=frames,
        factor_id="value",
        start_date=index[0],
        end_date=index[-1],
        groups=5,
        holding_period=5,
    )

    assert result["effectiveGroups"] == 2
    assert result["sampleDiagnostics"]["medianCrossSectionSize"] == 4
