import pytest

from trading_desk.agents.neo import SetupRules, build_setup
from trading_desk.models import Classification, Indicators


def indicators(**overrides):
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
    setup = build_setup(indicators(), blockers=[])
    assert setup.thesis.classification is Classification.BULLISH
    assert setup.entry_reference == 110.0
    assert setup.stop == pytest.approx(106.0)
    assert setup.target == pytest.approx(122.0)
    assert setup.support == 99.0
    assert setup.resistance == 111.0
    assert "106.0000" in setup.invalidation


def test_setup_rules_change_the_levels():
    setup = build_setup(
        indicators(),
        blockers=[],
        setup_rules=SetupRules(stop_atr_multiple=1.5, target_reward_multiple=3),
    )
    assert setup.stop == pytest.approx(107.0)
    assert setup.target == pytest.approx(119.0)


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
def test_no_levels_without_a_bullish_thesis_and_a_usable_stop(overrides):
    setup = build_setup(indicators(**overrides), blockers=[])
    assert not setup.has_trade_levels
    assert setup.stop is None
    assert "confirm" in setup.confirmation


def test_blockers_prevent_levels_and_the_counter_case_abstains():
    setup = build_setup(indicators(), blockers=["Stale daily data"])
    assert not setup.has_trade_levels
    assert setup.counter_case.abstain


def test_overbought_momentum_is_kept_as_a_bearish_counter_case():
    setup = build_setup(indicators(rsi14=78.0), blockers=[])
    assert setup.has_trade_levels
    assert setup.counter_case.classification is Classification.BEARISH


def test_insufficient_history_abstains_without_levels():
    setup = build_setup(indicators(sma50=None), blockers=[])
    assert setup.thesis.abstain
    assert not setup.has_trade_levels


def notes_for(setup, strategy_name):
    return next(note for note in setup.strategy_notes if note.startswith(strategy_name))


def test_trend_continuation_is_used_when_nothing_else_triggers():
    setup = build_setup(indicators(), blockers=[])
    assert setup.strategy.value == "TREND_CONTINUATION"
    assert "not enough history" in notes_for(setup, "BREAKOUT")
    assert "triggered" in notes_for(setup, "TREND_CONTINUATION")
    assert "bands unavailable" in notes_for(setup, "BOLLINGER_REVERSION")


def test_breakout_above_a_prior_high_on_a_volume_surge_takes_priority():
    setup = build_setup(
        indicators(prior_high20=108.0, prior_high55=109.0, relative_volume20=1.8), blockers=[]
    )
    assert setup.strategy.value == "BREAKOUT"
    assert setup.entry_reference == 110.0
    assert setup.stop == pytest.approx(106.0)  # 2 ATR under the close
    assert setup.target == pytest.approx(122.0)  # 3 times the risk
    assert "55-session high 109.0000 on 1.80x volume" in setup.confirmation
    assert "breakout failed" in setup.invalidation
    assert "Not used: BREAKOUT has priority." in notes_for(setup, "TREND_CONTINUATION")


def test_breakout_names_the_longest_window_it_cleared():
    yearly = build_setup(
        indicators(
            prior_high20=108.0, prior_high55=109.0, prior_high252=109.5, relative_volume20=2.0
        ),
        blockers=[],
    )
    assert "52-week high 109.5000" in yearly.confirmation
    only_short = build_setup(
        indicators(
            prior_high20=108.0, prior_high55=115.0, prior_high252=130.0, relative_volume20=2.0
        ),
        blockers=[],
    )
    assert only_short.strategy.value == "BREAKOUT"
    assert "20-session high 108.0000" in only_short.confirmation


@pytest.mark.parametrize("relative_volume", [None, 0.8, 1.49])
def test_breakout_without_a_volume_surge_is_not_taken(relative_volume):
    setup = build_setup(
        indicators(prior_high20=108.0, relative_volume20=relative_volume), blockers=[]
    )
    assert setup.strategy.value == "TREND_CONTINUATION"  # the trend rule still applies
    note = notes_for(setup, "BREAKOUT")
    assert "not taken" in note and "needs 1.5x" in note


def test_close_at_or_below_the_prior_high_is_not_a_breakout():
    setup = build_setup(indicators(prior_high20=110.0, relative_volume20=3.0), blockers=[])
    assert "close is not above its prior 20-session high" in notes_for(setup, "BREAKOUT")


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
    setup = build_setup(indicators(**PULLBACK), blockers=[])
    assert setup.strategy.value == "BOLLINGER_REVERSION"
    assert setup.entry_reference == 103.0
    assert setup.stop == pytest.approx(100.5)  # half an ATR under the low that touched
    assert setup.target == pytest.approx(110.0)  # upper band is nearer than the 20-session high
    assert "target is the upper band 110.0000" in setup.confirmation
    assert "bounce failed" in setup.invalidation
    assert "not above SMA20" in notes_for(setup, "TREND_CONTINUATION")


def test_reversion_targets_resistance_when_it_is_nearer_than_the_upper_band():
    setup = build_setup(indicators(**{**PULLBACK, "high20": 108.0}), blockers=[])
    assert setup.target == pytest.approx(108.0)
    assert "20-session high 108.0000" in setup.confirmation
    no_resistance = build_setup(indicators(**{**PULLBACK, "high20": None}), blockers=[])
    assert no_resistance.target == pytest.approx(110.0)


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
    setup = build_setup(indicators(**{**PULLBACK, **overrides}), blockers=[])
    assert setup.strategy is None
    assert not setup.has_trade_levels
    assert expected_note in notes_for(setup, "BOLLINGER_REVERSION")


def test_a_touch_exactly_at_the_band_counts():
    setup = build_setup(indicators(**{**PULLBACK, "latest_low": 102.0}), blockers=[])
    assert setup.strategy.value == "BOLLINGER_REVERSION"


def test_only_enabled_strategies_are_considered_in_the_order_given():
    breakout_and_trend = indicators(prior_high20=108.0, relative_volume20=2.0)
    trend_only = build_setup(
        breakout_and_trend,
        blockers=[],
        setup_rules=SetupRules(enabled_strategies=("TREND_CONTINUATION",)),
    )
    assert trend_only.strategy.value == "TREND_CONTINUATION"
    assert len(trend_only.strategy_notes) == 1
    trend_first = build_setup(
        breakout_and_trend,
        blockers=[],
        setup_rules=SetupRules(enabled_strategies=("TREND_CONTINUATION", "BREAKOUT")),
    )
    assert trend_first.strategy.value == "TREND_CONTINUATION"
    assert "Not used: TREND_CONTINUATION has priority." in notes_for(trend_first, "BREAKOUT")
    nothing_enabled = build_setup(
        breakout_and_trend, blockers=[], setup_rules=SetupRules(enabled_strategies=())
    )
    assert nothing_enabled.strategy is None and nothing_enabled.strategy_notes == []


def test_blockers_and_unusable_volatility_stop_every_strategy():
    assert build_setup(indicators(prior_high20=108.0), blockers=["Stale data"]).strategy is None
    for unusable in (None, 0.0):
        setup = build_setup(indicators(**{**PULLBACK, "atr14": unusable}), blockers=[])
        assert setup.strategy is None and setup.strategy_notes == []


def test_a_stop_at_or_below_zero_disqualifies_the_strategy():
    setup = build_setup(
        indicators(atr14=80.0, prior_high20=108.0, relative_volume20=2.0), blockers=[]
    )
    assert setup.strategy is None
    assert "stop would be at or below zero" in notes_for(setup, "BREAKOUT")
    assert "stop would be at or below zero" in notes_for(setup, "TREND_CONTINUATION")
