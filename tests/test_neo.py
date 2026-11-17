import pytest

from trading_desk.agents.neo import SetupRules, analyze_chart
from trading_desk.models import Classification, Indicators


def make_indicators(**overrides):
    """Test helper (not a test): builds indicators for the tests below."""
    values = {
        "candle_count": 80,
        "close": 110.0,
        "sma20": 105.0,
        "sma50": 100.0,
        "rsi14": 60.0,
        "atr14": 2.0,
        "return20_pct": 6.0,
        "relative_volume20": 1.1,
        "high20": 111.0,
        "low20": 99.0,
    }
    return Indicators(**{**values, **overrides})


def test_bullish_trend_gets_atr_stop_and_reward_multiple_target():
    """Verifies: bullish trend gets atr stop and reward multiple target."""
    chart = analyze_chart(make_indicators(), blockers=[])
    assert chart.thesis.classification is Classification.BULLISH
    assert chart.setup.entry_reference == 110.0
    assert chart.setup.stop == pytest.approx(106.0)
    assert chart.setup.target == pytest.approx(122.0)
    assert chart.support == 99.0
    assert chart.resistance == 111.0
    assert "106.0000" in chart.setup.invalidation


def test_setup_rules_change_the_levels():
    """Verifies: setup rules change the levels."""
    chart = analyze_chart(
        make_indicators(),
        blockers=[],
        setup_rules=SetupRules(stop_atr_multiple=1.5, target_reward_multiple=3),
    )
    assert chart.setup.stop == pytest.approx(107.0)
    assert chart.setup.target == pytest.approx(119.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"close": 104.0},
        {"close": 99.0},
        {"atr14": None},
        {"atr14": 0.0},
        {"atr14": 80.0},
    ],
)
def test_no_setup_without_a_trigger_and_a_usable_stop(overrides):
    """Verifies: no setup without a trigger and a usable stop."""
    chart_report = analyze_chart(make_indicators(**overrides), blockers=[])
    assert chart_report.setup is None
    # NEO still reports: what it is watching for, and the context levels.
    assert "A clear trend with a trigger in its direction" in chart_report.watch_for
    assert chart_report.support == 99.0 and chart_report.resistance == 111.0


def test_blockers_prevent_levels_and_the_counter_case_abstains():
    """Verifies: blockers prevent levels and the counter case abstains."""
    chart = analyze_chart(make_indicators(), blockers=["Stale daily data"])
    assert chart.setup is None
    assert chart.counter_case.abstain


def test_overbought_momentum_is_kept_as_a_bearish_counter_case():
    """Verifies: overbought momentum is kept as a bearish counter case."""
    chart = analyze_chart(make_indicators(rsi14=78.0), blockers=[])
    assert chart.setup is not None
    assert chart.counter_case.classification is Classification.BEARISH


def test_insufficient_history_abstains_without_levels():
    """Verifies: insufficient history abstains without levels."""
    chart = analyze_chart(make_indicators(sma50=None), blockers=[])
    assert chart.thesis.abstain
    assert chart.setup is None


def notes_for(chart, strategy_name):
    """Test helper (not a test): notes for."""
    return next(note for note in chart.strategy_notes if note.startswith(strategy_name))


def test_trend_continuation_is_used_when_nothing_else_triggers():
    """Verifies: trend continuation is used when nothing else triggers."""
    chart = analyze_chart(make_indicators(), blockers=[])
    assert chart.setup.strategy.value == "TREND_CONTINUATION"
    assert "not enough history" in notes_for(chart, "BREAKOUT")
    assert "triggered" in notes_for(chart, "TREND_CONTINUATION")
    assert "bands unavailable" in notes_for(chart, "BOLLINGER_REVERSION")


def test_breakout_above_a_prior_high_on_a_volume_surge_takes_priority():
    """Verifies: breakout above a prior high on a volume surge takes priority."""
    chart = analyze_chart(
        make_indicators(prior_high20=108.0, prior_high55=109.0, relative_volume20=1.8), blockers=[]
    )
    assert chart.setup.strategy.value == "BREAKOUT"
    assert chart.setup.entry_reference == 110.0
    assert chart.setup.stop == pytest.approx(106.0)  # 2 ATR under the close
    assert chart.setup.target == pytest.approx(122.0)  # 3 times the risk
    assert "55-session high 109.0000 on 1.80x volume" in chart.setup.confirmation
    assert "breakout failed" in chart.setup.invalidation
    assert "Not used: BREAKOUT has priority." in notes_for(chart, "TREND_CONTINUATION")


def test_breakout_names_the_longest_window_it_cleared():
    """Verifies: breakout names the longest window it cleared."""
    yearly = analyze_chart(
        make_indicators(
            prior_high20=108.0, prior_high55=109.0, prior_high252=109.5, relative_volume20=2.0
        ),
        blockers=[],
    )
    assert "52-week high 109.5000" in yearly.setup.confirmation
    only_short = analyze_chart(
        make_indicators(
            prior_high20=108.0, prior_high55=115.0, prior_high252=130.0, relative_volume20=2.0
        ),
        blockers=[],
    )
    assert only_short.setup.strategy.value == "BREAKOUT"
    assert "20-session high 108.0000" in only_short.setup.confirmation


@pytest.mark.parametrize("relative_volume", [None, 0.8, 1.49])
def test_breakout_without_a_volume_surge_is_not_taken(relative_volume):
    """Verifies: breakout without a volume surge is not taken."""
    chart = analyze_chart(
        make_indicators(prior_high20=108.0, relative_volume20=relative_volume), blockers=[]
    )
    assert chart.setup.strategy.value == "TREND_CONTINUATION"  # the trend rule still applies
    note = notes_for(chart, "BREAKOUT")
    assert "not taken" in note and "needs 1.5x" in note


def test_close_at_or_below_the_prior_high_is_not_a_breakout():
    """Verifies: close at or below the prior high is not a breakout."""
    chart = analyze_chart(make_indicators(prior_high20=110.0, relative_volume20=3.0), blockers=[])
    assert "close is not above its prior 20-session high" in notes_for(chart, "BREAKOUT")


PULLBACK = {
    "close": 103.0,
    "sma20": 106.0,
    "sma50": 100.0,
    "latest_low": 101.5,
    "bollinger_lower20": 102.0,
    "bollinger_upper20": 110.0,
    "high20": 111.0,
    "atr14": 2.0,
}


def test_touching_the_lower_band_in_an_uptrend_is_a_reversion_setup():
    """Verifies: touching the lower band in an uptrend is a reversion setup."""
    chart = analyze_chart(make_indicators(**PULLBACK), blockers=[])
    assert chart.setup.strategy.value == "BOLLINGER_REVERSION"
    assert chart.setup.entry_reference == 103.0
    assert chart.setup.stop == pytest.approx(100.5)  # half an ATR under the low that touched
    # The first target is the upper band as it stands today; it then follows the band.
    assert chart.setup.target == pytest.approx(110.0)
    assert chart.setup.target_follows_band is True
    assert "Exit is the next touch of the upper band, now 110.0000" in chart.setup.confirmation
    assert "bounce failed" in chart.setup.invalidation
    assert "not above SMA20" in notes_for(chart, "TREND_CONTINUATION")
    assert chart.trend is Classification.BULLISH


def test_reversion_exit_is_the_upper_band_whatever_the_nearby_resistance():
    """Verifies: the Bollinger exit is the upper band, not the 20-session high, because the
    owner's rule is "enter at the lower band, exit at the next touch of the upper band".
    """
    chart = analyze_chart(make_indicators(**{**PULLBACK, "high20": 108.0}), blockers=[])
    assert chart.setup.target == pytest.approx(110.0)


def test_only_the_bollinger_setup_has_a_target_that_follows_the_band():
    """Verifies: trend and breakout setups keep a fixed target; only the band reversion's
    target moves with the upper band.
    """
    trend = analyze_chart(make_indicators(), blockers=[])
    breakout = analyze_chart(
        make_indicators(prior_high20=108.0, relative_volume20=2.0), blockers=[]
    )
    assert trend.setup.target_follows_band is False
    assert breakout.setup.target_follows_band is False


@pytest.mark.parametrize(
    "overrides,expected_trend",
    [
        ({}, "BULLISH"),
        ({"close": 103.0, "sma20": 106.0}, "BULLISH"),  # pullback under SMA20, still above SMA50
        ({"close": 90.0, "sma20": 95.0}, "BEARISH"),
        ({"close": 99.0}, "NEUTRAL"),  # SMA20 above SMA50 but the close has lost SMA50
        ({"sma20": 98.0}, "NEUTRAL"),  # close above SMA50 but SMA20 below it
        ({"sma50": None}, "INSUFFICIENT_DATA"),
    ],
)
def test_overall_trend_is_read_from_the_averages(overrides, expected_trend):
    """Verifies: the trend that decides the side is bullish only when SMA20 and the close are
    both above SMA50, bearish only when both are below, and neutral when they disagree.
    """
    from trading_desk.agents.neo import overall_trend

    assert overall_trend(make_indicators(**overrides)).value == expected_trend


BEARISH = {"close": 90.0, "sma20": 95.0, "sma50": 100.0, "atr14": 2.0}


def test_a_bearish_trend_takes_no_long_even_when_a_long_pattern_would_trigger():
    """Verifies: in a bearish trend a lower-band touch and an upside volume breakout, both
    long patterns, are never bought. The only setup NEO may produce there is a short.
    """
    lower_band_touch = analyze_chart(
        make_indicators(
            **BEARISH, latest_low=88.0, bollinger_lower20=89.0, bollinger_upper20=101.0
        ),
        blockers=[],
    )
    upside_breakout = analyze_chart(
        make_indicators(**BEARISH, prior_high20=89.0, relative_volume20=3.0), blockers=[]
    )
    for chart in (lower_band_touch, upside_breakout):
        assert chart.trend is Classification.BEARISH
        assert chart.setup is None or chart.setup.side.value == "SHORT"


def test_with_the_short_side_switched_off_a_bearish_trend_takes_nothing():
    """Verifies: running long-only, a bearish trend produces no setup and the note says the
    short side is off, rather than taking a long against the trend.
    """
    chart = analyze_chart(
        make_indicators(**BEARISH), blockers=[], setup_rules=SetupRules(allowed_sides=("LONG",))
    )
    assert chart.setup is None
    assert len(chart.strategy_notes) == 1
    assert "calls for short setups only" in chart.strategy_notes[0]
    assert "short side is switched off" in chart.strategy_notes[0]
    long_only_uptrend = analyze_chart(
        make_indicators(), blockers=[], setup_rules=SetupRules(allowed_sides=("LONG",))
    )
    assert long_only_uptrend.setup.side.value == "LONG"
    short_only_uptrend = analyze_chart(
        make_indicators(), blockers=[], setup_rules=SetupRules(allowed_sides=("SHORT",))
    )
    assert short_only_uptrend.setup is None


def test_downtrend_continuation_is_a_short_with_the_stop_above_and_the_target_below():
    """Verifies: close below SMA20 below SMA50 produces a short: stop 2 ATR above the entry,
    target three times that risk below it.
    """
    chart = analyze_chart(make_indicators(**BEARISH), blockers=[])
    assert chart.setup.strategy.value == "TREND_CONTINUATION"
    assert chart.setup.side.value == "SHORT"
    assert chart.setup.entry_reference == 90.0
    assert chart.setup.stop == pytest.approx(94.0)
    assert chart.setup.target == pytest.approx(78.0)
    assert chart.setup.target_follows_band is False
    assert "Downtrend confirmed" in chart.setup.confirmation
    assert "A close above 94.0000 (2 ATR over entry)" in chart.setup.invalidation


def test_breakdown_below_a_prior_low_on_a_volume_surge_is_a_short_breakout():
    """Verifies: in a bearish trend a close under the longest broken prior low, with a volume
    surge, is the short BREAKOUT; without the surge it is refused like its long twin.
    """
    with_volume = analyze_chart(
        make_indicators(**BEARISH, prior_low20=92.0, prior_low55=91.0, relative_volume20=1.8),
        blockers=[],
    )
    assert with_volume.setup.strategy.value == "BREAKOUT"
    assert with_volume.setup.side.value == "SHORT"
    assert "Breakdown below the 55-session low 91.0000 on 1.80x volume" in (
        with_volume.setup.confirmation
    )
    assert "breakdown failed" in with_volume.setup.invalidation
    quiet = analyze_chart(
        make_indicators(**BEARISH, prior_low20=92.0, relative_volume20=1.0), blockers=[]
    )
    assert quiet.setup.strategy.value == "TREND_CONTINUATION"  # the trend rule still applies
    assert "not taken" in notes_for(quiet, "BREAKOUT")
    not_broken = analyze_chart(
        make_indicators(**BEARISH, prior_low20=89.0, relative_volume20=3.0), blockers=[]
    )
    assert "close is not below its prior 20-session low" in notes_for(not_broken, "BREAKOUT")


RALLY_IN_A_DOWNTREND = {
    "close": 97.0,
    "sma20": 95.0,
    "sma50": 100.0,
    "latest_high": 98.6,
    "bollinger_upper20": 98.0,
    "bollinger_lower20": 90.0,
    "atr14": 2.0,
}


def test_touching_the_upper_band_in_a_downtrend_is_the_short_reversion_entry():
    """Verifies: in a bearish trend the upper band is the entry and the lower band the exit:
    the mirror of the long rule, with the stop half an ATR above the high that touched.
    """
    chart = analyze_chart(make_indicators(**RALLY_IN_A_DOWNTREND), blockers=[])
    assert chart.trend is Classification.BEARISH
    assert chart.setup.strategy.value == "BOLLINGER_REVERSION"
    assert chart.setup.side.value == "SHORT"
    assert chart.setup.entry_reference == 97.0
    assert chart.setup.stop == pytest.approx(99.6)
    assert chart.setup.target == pytest.approx(90.0)
    assert chart.setup.target_follows_band is True
    assert "upper Bollinger band 98.0000 was touched: short entry" in chart.setup.confirmation
    assert "Exit is the next touch of the lower band, now 90.0000" in chart.setup.confirmation
    assert "rejection failed" in chart.setup.invalidation


@pytest.mark.parametrize(
    "overrides,expected_note",
    [
        ({"latest_high": 97.5}, "high did not reach the upper band"),
        ({"bollinger_lower20": 97.5}, "no room left to the lower band"),
        ({"latest_high": None}, "bands unavailable"),
    ],
)
def test_short_reversion_is_refused_with_the_reason_stated(overrides, expected_note):
    """Verifies: the short band entry has the same refusals as the long one, each explained."""
    chart = analyze_chart(
        make_indicators(**{**RALLY_IN_A_DOWNTREND, **overrides}),
        blockers=[],
        setup_rules=SetupRules(enabled_strategies=("BOLLINGER_REVERSION",)),
    )
    assert chart.setup is None
    assert expected_note in notes_for(chart, "BOLLINGER_REVERSION")


def test_a_short_whose_target_would_be_at_or_below_zero_is_refused():
    """Verifies: a short on a low-priced symbol with a wide ATR cannot have a target at or
    below zero; the strategy is disqualified instead.
    """
    chart = analyze_chart(make_indicators(close=5.0, sma20=6.0, sma50=7.0, atr14=1.0), blockers=[])
    assert chart.setup is None
    assert "stop or target would be at or below zero" in notes_for(chart, "TREND_CONTINUATION")


def test_without_a_clear_trend_nothing_is_taken():
    """Verifies: when SMA20 and the close disagree about SMA50 there is no setup of any kind."""
    chart = analyze_chart(
        make_indicators(sma20=98.0, prior_high20=108.0, relative_volume20=3.0), blockers=[]
    )
    assert chart.trend is Classification.NEUTRAL
    assert chart.setup is None
    assert "No clear trend" in chart.strategy_notes[0]


@pytest.mark.parametrize(
    "overrides,expected_note",
    [
        ({"latest_low": 102.5}, "low did not reach the lower band"),
        ({"bollinger_upper20": 102.5}, "no room left to the upper band"),
        ({"bollinger_lower20": None}, "bands unavailable"),
        ({"latest_low": None}, "bands unavailable"),
    ],
)
def test_reversion_is_refused_with_the_reason_stated(overrides, expected_note):
    """Verifies: reversion is refused with the reason stated."""
    chart = analyze_chart(make_indicators(**{**PULLBACK, **overrides}), blockers=[])
    assert chart.setup is None
    assert chart.setup is None
    assert expected_note in notes_for(chart, "BOLLINGER_REVERSION")


def test_a_touch_exactly_at_the_band_counts():
    """Verifies: a touch exactly at the band counts."""
    chart = analyze_chart(make_indicators(**{**PULLBACK, "latest_low": 102.0}), blockers=[])
    assert chart.setup.strategy.value == "BOLLINGER_REVERSION"


def test_only_enabled_strategies_are_considered_in_the_order_given():
    """Verifies: only enabled strategies are considered in the order given."""
    breakout_and_trend = make_indicators(prior_high20=108.0, relative_volume20=2.0)
    trend_only = analyze_chart(
        breakout_and_trend,
        blockers=[],
        setup_rules=SetupRules(enabled_strategies=("TREND_CONTINUATION",)),
    )
    assert trend_only.setup.strategy.value == "TREND_CONTINUATION"
    assert len(trend_only.strategy_notes) == 1
    trend_first = analyze_chart(
        breakout_and_trend,
        blockers=[],
        setup_rules=SetupRules(enabled_strategies=("TREND_CONTINUATION", "BREAKOUT")),
    )
    assert trend_first.setup.strategy.value == "TREND_CONTINUATION"
    assert "Not used: TREND_CONTINUATION has priority." in notes_for(trend_first, "BREAKOUT")
    nothing_enabled = analyze_chart(
        breakout_and_trend, blockers=[], setup_rules=SetupRules(enabled_strategies=())
    )
    assert nothing_enabled.setup is None and nothing_enabled.strategy_notes == []


def test_blockers_and_unusable_volatility_stop_every_strategy():
    """Verifies: blockers and unusable volatility stop every strategy."""
    assert analyze_chart(make_indicators(prior_high20=108.0), blockers=["Stale data"]).setup is None
    for unusable in (None, 0.0):
        chart = analyze_chart(make_indicators(**{**PULLBACK, "atr14": unusable}), blockers=[])
        assert chart.setup is None and chart.strategy_notes == []


def test_a_stop_at_or_below_zero_disqualifies_the_strategy():
    """Verifies: a stop at or below zero disqualifies the strategy."""
    chart = analyze_chart(
        make_indicators(atr14=80.0, prior_high20=108.0, relative_volume20=2.0), blockers=[]
    )
    assert chart.setup is None
    assert "stop or target would be at or below zero" in notes_for(chart, "BREAKOUT")
    assert "stop or target would be at or below zero" in notes_for(chart, "TREND_CONTINUATION")
