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


def make_analysis(classification=Classification.BULLISH):
    """Test helper (not a test): builds analysis for the tests below."""
    is_abstaining = classification is Classification.INSUFFICIENT_DATA
    return Analysis(
        classification=classification,
        summary=f"{classification} view.",
        evidence=[] if is_abstaining else [Evidence(metric="close", value=100.0)],
        risks=[],
        abstain=is_abstaining,
    )


def make_scout_report(verdict=ScoutVerdict.WATCH):
    """Test helper (not a test): builds scout report for the tests below."""
    return ScoutReport(
        symbol="AAPL",
        verdict=verdict,
        score=None if verdict is ScoutVerdict.NEED_MORE_DATA else 0.8,
        reasons=["r"],
        evidence=[],
    )


def make_chart_report_with_setup(
    counter_case=Classification.NEUTRAL, strategy="TREND_CONTINUATION"
):
    """Test helper (not a test): A chart report carrying a complete trade setup."""
    return ChartReport(
        thesis=make_analysis(),
        counter_case=make_analysis(counter_case),
        setup=TradeSetup(
            strategy=strategy,
            entry_reference=100.0,
            stop=95.0,
            target=112.5,
            confirmation="c",
            invalidation="i",
        ),
    )


def make_news_report(verdict=Classification.NEUTRAL):
    """Test helper (not a test): builds news report for the tests below."""
    return NewsReport(
        verdict=verdict, abstain=verdict is Classification.INSUFFICIENT_DATA, summary="News view."
    )


def make_sentiment_report(verdict=Classification.NEUTRAL, hype_risk=HypeRisk.LOW):
    """Test helper (not a test): builds sentiment report for the tests below."""
    is_abstaining = verdict is Classification.INSUFFICIENT_DATA
    return SentimentReport(
        verdict=verdict,
        abstain=is_abstaining,
        summary="Mood view.",
        mood_score=None if is_abstaining else 55,
        hype_risk=hype_risk,
    )


def make_trade_plan():
    """Test helper (not a test): builds trade plan for the tests below."""
    return TradePlan(
        strategy="TREND_CONTINUATION",
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


def make_risk_approval():
    """Test helper (not a test): builds risk approval for the tests below."""
    passing = RiskCheck(name="Open ideas", limit="fewer than 1 open", actual="0 open", passed=True)
    return RiskReport(verdict=RiskVerdict.APPROVE, checks=[passing], plan=make_trade_plan())


def make_risk_refusal(verdict, failed_checks, revision=None):
    """Test helper (not a test): builds risk refusal for the tests below."""
    failing = RiskCheck(name=failed_checks[0], limit="limit", actual="actual", passed=False)
    return RiskReport(
        verdict=verdict, checks=[failing], failed_checks=failed_checks, revision=revision
    )


def decide(**overrides):
    """Test helper (not a test): decide."""
    reports = {
        "scout_report": make_scout_report(),
        "chart": make_chart_report_with_setup(),
        "news_report": make_news_report(),
        "sentiment_report": make_sentiment_report(),
        "risk_report": make_risk_approval(),
    }
    return picard.decide(**{**reports, **overrides})


def test_trade_carries_the_approved_plan_unchanged():
    """Verifies: trade carries the approved plan unchanged."""
    decision = decide()
    assert decision.action is DecisionAction.TRADE
    assert decision.plan == make_trade_plan()
    assert "ODIN APPROVE" in decision.sources
    assert "maximum loss 250.00" in decision.summary


def test_risk_rejection_is_final_whatever_the_other_agents_say():
    """Verifies: risk rejection is final whatever the other agents say."""
    decision = decide(
        risk_report=make_risk_refusal(RiskVerdict.REJECT, ["Kill switch", "Open ideas"]),
        sentiment_report=make_sentiment_report(Classification.BULLISH),
    )
    assert decision.action is DecisionAction.REJECT
    assert decision.failed_rule == "Kill switch, Open ideas"
    assert decision.plan is None


def test_risk_revision_becomes_wait_with_the_requested_change():
    """Verifies: risk revision becomes wait with the requested change."""
    decision = decide(
        risk_report=make_risk_refusal(
            RiskVerdict.REVISE, ["Reward to risk after costs"], "Widen the target."
        )
    )
    assert decision.action is DecisionAction.WAIT
    assert decision.required_confirmation == "Widen the target."


@pytest.mark.parametrize(
    "overrides,expected_text",
    [
        ({"scout_report": make_scout_report(ScoutVerdict.SKIP)}, "HAWKEYE"),
        ({"chart": make_chart_report_with_setup(Classification.BEARISH)}, "counter-case"),
        ({"news_report": make_news_report(Classification.BEARISH)}, "SNOWDEN"),
        (
            {"sentiment_report": make_sentiment_report(Classification.BULLISH, HypeRisk.HIGH)},
            "hype",
        ),
    ],
)
def test_approved_ideas_still_wait_when_an_agent_objects(overrides, expected_text):
    """Verifies: approved ideas still wait when an agent objects."""
    decision = decide(**overrides)
    assert decision.action is DecisionAction.WAIT
    assert decision.plan is None
    assert expected_text in decision.summary
    assert decision.required_confirmation


def test_disagreement_and_missing_views_are_kept_as_dissent():
    """Verifies: disagreement and missing views are kept as dissent."""
    decision = decide(
        news_report=make_news_report(Classification.INSUFFICIENT_DATA),
        sentiment_report=make_sentiment_report(Classification.NEUTRAL),
    )
    assert decision.action is DecisionAction.TRADE
    assert len(decision.dissent) == 3
    assert any(line.startswith("SNOWDEN=INSUFFICIENT_DATA") for line in decision.dissent)
    assert any(line.startswith("FAMA=NEUTRAL") for line in decision.dissent)


def test_full_agreement_records_no_dissent():
    """Verifies: full agreement records no dissent."""
    decision = decide(
        chart=make_chart_report_with_setup(Classification.BULLISH),
        news_report=make_news_report(Classification.BULLISH),
        sentiment_report=make_sentiment_report(Classification.BULLISH),
    )
    assert decision.action is DecisionAction.TRADE
    assert decision.dissent == []


def make_scout_report_with_signals(verdict, signals=()):
    """Test helper (not a test): builds scout report with signals for the tests below."""
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
    """Test helper (not a test): decide reversion."""
    return decide(
        scout_report=reversion_scout_report,
        chart=make_chart_report_with_setup(strategy="BOLLINGER_REVERSION"),
    )


def test_a_reversion_setup_may_trade_without_a_trend_watch_rating():
    """Verifies: a reversion setup may trade without a trend watch rating."""
    decision = decide_reversion(make_scout_report_with_signals(ScoutVerdict.SKIP))
    assert decision.action is DecisionAction.TRADE


def test_a_trend_setup_still_needs_the_watch_rating():
    """Verifies: a trend setup still needs the watch rating."""
    decision = decide(scout_report=make_scout_report_with_signals(ScoutVerdict.SKIP))
    assert decision.action is DecisionAction.WAIT


def test_a_reversion_setup_waits_on_a_heavy_volume_breakdown_or_missing_data():
    """Verifies: a reversion setup waits on a heavy volume breakdown or missing data."""
    breakdown = make_scout_report_with_signals(
        ScoutVerdict.SKIP, [("BREAKDOWN_BELOW_PRIOR_DAY_LOW", False, True)]
    )
    assert decide_reversion(breakdown).action is DecisionAction.WAIT
    assert (
        decide_reversion(make_scout_report_with_signals(ScoutVerdict.NEED_MORE_DATA)).action
        is DecisionAction.WAIT
    )
    quiet_breakdown = make_scout_report_with_signals(
        ScoutVerdict.SKIP, [("BREAKDOWN_BELOW_PRIOR_DAY_LOW", False, False)]
    )
    assert decide_reversion(quiet_breakdown).action is DecisionAction.TRADE


def make_short_chart_report(counter_case=Classification.NEUTRAL, strategy="TREND_CONTINUATION"):
    """Test helper (not a test): builds a chart report carrying a complete short setup."""
    return ChartReport(
        thesis=make_analysis(Classification.BEARISH),
        counter_case=make_analysis(counter_case),
        setup=TradeSetup(
            strategy=strategy,
            side="SHORT",
            entry_reference=100.0,
            stop=105.0,
            target=85.0,
            confirmation="c",
            invalidation="i",
        ),
    )


def make_short_scout_report():
    """Test helper (not a test): builds a WATCH rating on the short side."""
    return ScoutReport(
        symbol="AAPL",
        verdict=ScoutVerdict.WATCH,
        score=0.8,
        side="SHORT",
        reasons=["r"],
        evidence=[],
    )


def decide_short(**overrides):
    """Test helper (not a test): decides a short with every agent agreeing unless overridden."""
    return decide(
        **{
            "scout_report": make_short_scout_report(),
            "chart": make_short_chart_report(),
            **overrides,
        }
    )


def test_a_short_trades_when_the_scout_and_the_setup_both_point_down():
    """Verifies: an approved short with a short-side WATCH rating is a TRADE."""
    assert decide_short().action is DecisionAction.TRADE


def test_the_scout_and_the_setup_must_point_the_same_way():
    """Verifies: a long-side WATCH cannot wave through a short setup, nor a short-side WATCH a
    long setup; the desk waits and says why.
    """
    short_setup_long_scout = decide(
        scout_report=make_scout_report().model_copy(update={"side": TradeSide.LONG}),
        chart=make_short_chart_report(),
    )
    assert short_setup_long_scout.action is DecisionAction.WAIT
    assert "long candidate, but NEO's setup is short" in short_setup_long_scout.summary
    long_setup_short_scout = decide(scout_report=make_short_scout_report())
    assert long_setup_short_scout.action is DecisionAction.WAIT
    assert "short candidate, but NEO's setup is long" in long_setup_short_scout.summary


def test_each_objection_blocks_the_side_it_argues_against():
    """Verifies: bullish news and an oversold (bullish) counter-case stop a short but not a
    long; bearish news and an overbought counter-case stop a long but not a short.
    """
    assert decide_short(news_report=make_news_report(Classification.BULLISH)).action is (
        DecisionAction.WAIT
    )
    assert decide_short(news_report=make_news_report(Classification.BEARISH)).action is (
        DecisionAction.TRADE
    )
    assert decide_short(chart=make_short_chart_report(Classification.BULLISH)).action is (
        DecisionAction.WAIT
    )
    assert decide_short(chart=make_short_chart_report(Classification.BEARISH)).action is (
        DecisionAction.TRADE
    )
    assert decide(news_report=make_news_report(Classification.BULLISH)).action is (
        DecisionAction.TRADE
    )


def test_crowd_hype_stops_a_long_but_not_a_short():
    """Verifies: one-sided bullish hype is a reason not to buy; it is not a reason not to
    sell short.
    """
    hyped = make_sentiment_report(Classification.BULLISH, HypeRisk.HIGH)
    assert decide(sentiment_report=hyped).action is DecisionAction.WAIT
    assert decide_short(sentiment_report=hyped).action is DecisionAction.TRADE


def test_a_short_band_trade_waits_on_a_heavy_volume_breakout_against_it():
    """Verifies: the band strategy's narrower scout gate mirrors for shorts: a volume-backed
    breakout blocks it, a volume-backed breakdown does not.
    """
    short_band_chart = make_short_chart_report(strategy="BOLLINGER_REVERSION")
    squeezed = make_scout_report_with_signals(
        ScoutVerdict.SKIP, [("BREAKOUT_ABOVE_PRIOR_DAY_HIGH", True, True)]
    )
    falling = make_scout_report_with_signals(
        ScoutVerdict.SKIP, [("BREAKDOWN_BELOW_PRIOR_DAY_LOW", False, True)]
    )
    assert decide(scout_report=squeezed, chart=short_band_chart).action is DecisionAction.WAIT
    assert decide(scout_report=falling, chart=short_band_chart).action is DecisionAction.TRADE
