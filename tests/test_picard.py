import pytest

from trading_desk.agents import picard
from trading_desk.models import (
    Analysis,
    ChartReport,
    Classification,
    DecisionAction,
    Evidence,
    HypeRisk,
    NewsReport,
    RiskCheck,
    RiskReport,
    RiskVerdict,
    ScoutReport,
    ScoutVerdict,
    SentimentReport,
    TradePlan,
    TradeSetup,
    TradeSide,
)


def analysis(classification=Classification.BULLISH):
    is_abstaining = classification is Classification.INSUFFICIENT_DATA
    return Analysis(
        classification=classification,
        summary=f"{classification} view.",
        evidence=[] if is_abstaining else [Evidence(metric="close", value=100.0)],
        risks=[],
        abstain=is_abstaining,
    )


def scout_report(verdict=ScoutVerdict.WATCH):
    return ScoutReport(
        symbol="AAPL",
        verdict=verdict,
        score=None if verdict is ScoutVerdict.NEED_MORE_DATA else 0.8,
        reasons=["r"],
        evidence=[],
    )


def setup(counter_case=Classification.NEUTRAL, strategy="TREND_CONTINUATION"):
    """A chart report carrying a complete trade setup."""
    return ChartReport(
        thesis=analysis(),
        counter_case=analysis(counter_case),
        setup=TradeSetup(
            strategy=strategy,
            entry_reference=100.0,
            stop=95.0,
            target=112.5,
            confirmation="c",
            invalidation="i",
        ),
    )


def news_report(verdict=Classification.NEUTRAL):
    return NewsReport(
        verdict=verdict, abstain=verdict is Classification.INSUFFICIENT_DATA, summary="News view."
    )


def sentiment_report(verdict=Classification.NEUTRAL, hype_risk=HypeRisk.LOW):
    is_abstaining = verdict is Classification.INSUFFICIENT_DATA
    return SentimentReport(
        verdict=verdict,
        abstain=is_abstaining,
        summary="Mood view.",
        mood_score=None if is_abstaining else 55,
        hype_risk=hype_risk,
    )


def plan():
    return TradePlan(
        symbol="AAPL",
        side=TradeSide.LONG,
        entry_reference=100.0,
        stop=95.0,
        target=112.5,
        units=50,
        notional=5000.0,
        risk_amount=250.0,
        reward_to_risk=2.5,
    )


def approval():
    passing = RiskCheck(name="Open ideas", limit="fewer than 1 open", actual="0 open", passed=True)
    return RiskReport(verdict=RiskVerdict.APPROVE, checks=[passing], plan=plan())


def refusal(verdict, failed_checks, revision=None):
    failing = RiskCheck(name=failed_checks[0], limit="limit", actual="actual", passed=False)
    return RiskReport(
        verdict=verdict, checks=[failing], failed_checks=failed_checks, revision=revision
    )


def decide(**overrides):
    reports = {
        "scout_report": scout_report(),
        "chart": setup(),
        "news_report": news_report(),
        "sentiment_report": sentiment_report(),
        "risk_report": approval(),
    }
    return picard.decide(**{**reports, **overrides})


def test_trade_carries_the_approved_plan_unchanged():
    decision = decide()
    assert decision.action is DecisionAction.TRADE
    assert decision.plan == plan()
    assert "ODIN APPROVE" in decision.sources
    assert "maximum loss 250.00" in decision.summary


def test_risk_rejection_is_final_whatever_the_other_agents_say():
    decision = decide(
        risk_report=refusal(RiskVerdict.REJECT, ["Kill switch", "Open ideas"]),
        sentiment_report=sentiment_report(Classification.BULLISH),
    )
    assert decision.action is DecisionAction.REJECT
    assert decision.failed_rule == "Kill switch, Open ideas"
    assert decision.plan is None


def test_risk_revision_becomes_wait_with_the_requested_change():
    decision = decide(
        risk_report=refusal(RiskVerdict.REVISE, ["Reward to risk after costs"], "Widen the target.")
    )
    assert decision.action is DecisionAction.WAIT
    assert decision.required_confirmation == "Widen the target."


@pytest.mark.parametrize(
    "overrides,expected_text",
    [
        ({"scout_report": scout_report(ScoutVerdict.SKIP)}, "HAWKEYE"),
        ({"chart": setup(Classification.BEARISH)}, "counter-case"),
        ({"news_report": news_report(Classification.BEARISH)}, "SNOWDEN"),
        (
            {"sentiment_report": sentiment_report(Classification.BULLISH, HypeRisk.HIGH)},
            "hype",
        ),
    ],
)
def test_approved_ideas_still_wait_when_an_agent_objects(overrides, expected_text):
    decision = decide(**overrides)
    assert decision.action is DecisionAction.WAIT
    assert decision.plan is None
    assert expected_text in decision.summary
    assert decision.required_confirmation


def test_disagreement_and_missing_views_are_kept_as_dissent():
    decision = decide(
        news_report=news_report(Classification.INSUFFICIENT_DATA),
        sentiment_report=sentiment_report(Classification.NEUTRAL),
    )
    assert decision.action is DecisionAction.TRADE
    assert len(decision.dissent) == 3
    assert any(line.startswith("SNOWDEN=INSUFFICIENT_DATA") for line in decision.dissent)
    assert any(line.startswith("FAMA=NEUTRAL") for line in decision.dissent)


def test_full_agreement_records_no_dissent():
    decision = decide(
        chart=setup(Classification.BULLISH),
        news_report=news_report(Classification.BULLISH),
        sentiment_report=sentiment_report(Classification.BULLISH),
    )
    assert decision.action is DecisionAction.TRADE
    assert decision.dissent == []


def scout_with(verdict, signals=()):
    from trading_desk.models import ScoutSignal

    return ScoutReport(
        symbol="AAPL",
        verdict=verdict,
        score=None if verdict is ScoutVerdict.NEED_MORE_DATA else 0.4,
        signals=[
            ScoutSignal(kind=kind, level=100.0, is_bullish=is_bullish, volume_confirmed=confirmed)
            for kind, is_bullish, confirmed in signals
        ],
        reasons=["r"],
        evidence=[],
    )


def decide_reversion(reversion_scout_report):
    return decide(scout_report=reversion_scout_report, chart=setup(strategy="BOLLINGER_REVERSION"))


def test_a_reversion_setup_may_trade_without_a_trend_watch_rating():
    decision = decide_reversion(scout_with(ScoutVerdict.SKIP))
    assert decision.action is DecisionAction.TRADE


def test_a_trend_setup_still_needs_the_watch_rating():
    decision = decide(scout_report=scout_with(ScoutVerdict.SKIP))
    assert decision.action is DecisionAction.WAIT


def test_a_reversion_setup_waits_on_a_heavy_volume_breakdown_or_missing_data():
    breakdown = scout_with(ScoutVerdict.SKIP, [("BREAKDOWN_BELOW_PRIOR_DAY_LOW", False, True)])
    assert decide_reversion(breakdown).action is DecisionAction.WAIT
    assert decide_reversion(scout_with(ScoutVerdict.NEED_MORE_DATA)).action is DecisionAction.WAIT
    quiet_breakdown = scout_with(
        ScoutVerdict.SKIP, [("BREAKDOWN_BELOW_PRIOR_DAY_LOW", False, False)]
    )
    assert decide_reversion(quiet_breakdown).action is DecisionAction.TRADE
