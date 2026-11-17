"""The desk contracts enforce their invariants; these tests pin each one."""

import pytest
from pydantic import ValidationError

from trading_desk.models import (
    Analysis,
    ChartReport,
    Classification,
    Decision,
    DecisionAction,
    Evidence,
    NewsReport,
    RiskCheck,
    RiskLimits,
    RiskReport,
    RiskVerdict,
    ScoutReport,
    ScoutVerdict,
    SentimentReport,
    TradePlan,
    TradeSetup,
    TradeSide,
)


def make_bullish_analysis():
    """Test helper (not a test): builds bullish analysis for the tests below."""
    return Analysis(
        classification=Classification.BULLISH,
        summary="Trend up.",
        evidence=[Evidence(metric="close", value=100.0)],
        risks=[],
        abstain=False,
    )


def make_trade_plan(**overrides):
    """Test helper (not a test): builds trade plan for the tests below."""
    values = {
        "symbol": "AAPL",
        "side": TradeSide.LONG,
        "strategy": "TREND_CONTINUATION",
        "entry_reference": 100.0,
        "stop": 95.0,
        "target": 112.0,
        "units": 10,
        "notional": 1000.0,
        "risk_amount": 50.0,
        "reward_to_risk": 2.4,
    }
    return TradePlan(**{**values, **overrides})


def make_passing_check():
    """Test helper (not a test): builds passing check for the tests below."""
    return RiskCheck(name="Open ideas", limit="<= 1", actual="0", passed=True)


def make_failing_check():
    """Test helper (not a test): builds failing check for the tests below."""
    return RiskCheck(name="Reward to risk", limit=">= 2.00", actual="1.40", passed=False)


def test_scout_score_is_required_unless_more_data_is_needed():
    """Verifies: scout score is required unless more data is needed."""
    ScoutReport(symbol="AAPL", verdict=ScoutVerdict.WATCH, score=0.7, reasons=["x"], evidence=[])
    ScoutReport(symbol="AAPL", verdict=ScoutVerdict.NEED_MORE_DATA, reasons=["x"], evidence=[])
    with pytest.raises(ValidationError):
        ScoutReport(symbol="AAPL", verdict=ScoutVerdict.SKIP, reasons=["x"], evidence=[])
    with pytest.raises(ValidationError):
        ScoutReport(
            symbol="AAPL",
            verdict=ScoutVerdict.NEED_MORE_DATA,
            score=0.5,
            reasons=["x"],
            evidence=[],
        )


def make_trade_setup(**overrides):
    """Test helper (not a test): builds trade setup for the tests below."""
    return TradeSetup(
        **{
            "strategy": "TREND_CONTINUATION",
            "entry_reference": 100.0,
            "stop": 95.0,
            "target": 115.0,
            "confirmation": "c",
            "invalidation": "i",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "missing_field",
    ["strategy", "entry_reference", "stop", "target", "confirmation", "invalidation"],
)
def test_a_trade_setup_cannot_exist_without_every_one_of_its_parts(missing_field):
    """Verifies: a trade setup cannot exist without every one of its parts."""
    complete = make_trade_setup().model_dump()
    del complete[missing_field]
    with pytest.raises(ValidationError):
        TradeSetup(**complete)


@pytest.mark.parametrize(
    "levels",
    [
        {"stop": 105.0},  # stop above entry
        {"target": 99.0},  # target below entry
        {"stop": 100.0},  # no risk distance
        {"target": 100.0},  # no reward distance
        {"stop": 0.0},
    ],
)
def test_a_long_trade_setup_needs_stop_below_entry_below_target(levels):
    """Verifies: a long trade setup needs stop below entry below target."""
    with pytest.raises(ValidationError):
        make_trade_setup(**levels)


def test_a_chart_report_may_carry_a_setup_or_say_what_it_is_watching_for():
    """Verifies: a chart report may carry a setup or say what it is watching for."""
    analyses = {"thesis": make_bullish_analysis(), "counter_case": make_bullish_analysis()}
    with_setup = ChartReport(**analyses, setup=make_trade_setup())
    assert with_setup.setup.stop == 95.0
    # No pattern triggered: there is no setup at all, only what NEO is watching for.
    watching = ChartReport(**analyses, watch_for="A close above SMA20 above SMA50.")
    assert watching.setup is None
    # Saying nothing is not allowed: no setup and nothing to watch for is an empty report.
    with pytest.raises(ValidationError, match="watching for"):
        ChartReport(**analyses)
    with pytest.raises(ValidationError, match="watching for"):
        ChartReport(**analyses, watch_for="")


@pytest.mark.parametrize("report_type", [NewsReport, SentimentReport])
def test_news_and_sentiment_abstention_must_match_the_verdict(report_type):
    """Verifies: news and sentiment abstention must match the verdict."""
    report_type(verdict=Classification.INSUFFICIENT_DATA, abstain=True, summary="none")
    with pytest.raises(ValidationError):
        report_type(verdict=Classification.NEUTRAL, abstain=True, summary="none")
    with pytest.raises(ValidationError):
        report_type(verdict=Classification.INSUFFICIENT_DATA, abstain=False, summary="none")


def test_abstaining_sentiment_cannot_carry_a_mood_score():
    """Verifies: abstaining sentiment cannot carry a mood score."""
    with pytest.raises(ValidationError):
        SentimentReport(
            verdict=Classification.INSUFFICIENT_DATA, abstain=True, summary="none", mood_score=50
        )


@pytest.mark.parametrize("levels", [{"stop": 101.0}, {"target": 99.0}])
def test_trade_plan_requires_stop_below_entry_below_target(levels):
    """Verifies: trade plan requires stop below entry below target."""
    with pytest.raises(ValidationError):
        make_trade_plan(**levels)


def test_risk_approval_requires_every_check_and_a_plan():
    """Verifies: risk approval requires every check and a plan."""
    RiskReport(verdict=RiskVerdict.APPROVE, checks=[make_passing_check()], plan=make_trade_plan())
    with pytest.raises(ValidationError):
        RiskReport(verdict=RiskVerdict.APPROVE, checks=[make_passing_check()])
    with pytest.raises(ValidationError):
        RiskReport(
            verdict=RiskVerdict.APPROVE,
            checks=[make_passing_check(), make_failing_check()],
            plan=make_trade_plan(),
        )


def test_risk_rejection_names_failures_and_carries_no_plan():
    """Verifies: risk rejection names failures and carries no plan."""
    RiskReport(
        verdict=RiskVerdict.REJECT, checks=[make_failing_check()], failed_checks=["Reward to risk"]
    )
    with pytest.raises(ValidationError):
        RiskReport(verdict=RiskVerdict.REJECT, checks=[make_failing_check()])
    with pytest.raises(ValidationError):
        RiskReport(
            verdict=RiskVerdict.REJECT,
            checks=[make_failing_check()],
            failed_checks=["Reward to risk"],
            plan=make_trade_plan(),
        )
    with pytest.raises(ValidationError):
        RiskReport(verdict=RiskVerdict.REVISE, checks=[make_passing_check()], failed_checks=["x"])


def test_decision_outcomes_must_be_explained():
    """Verifies: decision outcomes must be explained."""
    Decision(action=DecisionAction.TRADE, summary="go", plan=make_trade_plan())
    Decision(action=DecisionAction.WAIT, summary="hold", required_confirmation="close > SMA20")
    Decision(action=DecisionAction.REJECT, summary="no", failed_rule="Reward to risk")
    with pytest.raises(ValidationError):
        Decision(action=DecisionAction.TRADE, summary="go")
    with pytest.raises(ValidationError):
        Decision(action=DecisionAction.WAIT, summary="hold", plan=make_trade_plan())
    with pytest.raises(ValidationError):
        Decision(action=DecisionAction.WAIT, summary="hold")
    with pytest.raises(ValidationError):
        Decision(action=DecisionAction.REJECT, summary="no")


def test_risk_limits_default_to_the_beginner_rules_and_are_immutable():
    """Verifies: risk limits default to the beginner rules and are immutable."""
    limits = RiskLimits()
    assert limits.max_loss_per_idea_fraction == 0.01
    assert limits.minimum_reward_to_risk == 2.75
    assert limits.maximum_open_ideas == 1
    assert not limits.kill_switch_engaged
    with pytest.raises(ValidationError):
        limits.maximum_open_ideas = 5
