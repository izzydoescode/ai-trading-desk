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
        {"close": 90.0, "sma20": 95.0},
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
    assert "close above SMA20 above SMA50" in chart_report.watch_for
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
    assert chart.setup.target == pytest.approx(
        110.0
    )  # upper band is nearer than the 20-session high
    assert "target is the upper band 110.0000" in chart.setup.confirmation
    assert "bounce failed" in chart.setup.invalidation
    assert "not above SMA20" in notes_for(chart, "TREND_CONTINUATION")


def test_reversion_targets_resistance_when_it_is_nearer_than_the_upper_band():
    """Verifies: reversion targets resistance when it is nearer than the upper band."""
    chart = analyze_chart(make_indicators(**{**PULLBACK, "high20": 108.0}), blockers=[])
    assert chart.setup.target == pytest.approx(108.0)
    assert "20-session high 108.0000" in chart.setup.confirmation
    no_resistance = analyze_chart(make_indicators(**{**PULLBACK, "high20": None}), blockers=[])
    assert no_resistance.setup.target == pytest.approx(110.0)


@pytest.mark.parametrize(
    "overrides,expected_note",
    [
        ({"latest_low": 102.5}, "low did not reach the lower band"),
        ({"sma50": 104.0}, "a touch in a downtrend is not bought"),
        ({"bollinger_upper20": 102.5, "high20": 102.8}, "no room left to the target"),
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
    assert "stop would be at or below zero" in notes_for(chart, "BREAKOUT")
    assert "stop would be at or below zero" in notes_for(chart, "TREND_CONTINUATION")
