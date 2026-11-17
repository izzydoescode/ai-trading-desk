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
    assert setup.target == pytest.approx(120.0)
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
