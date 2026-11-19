"""PICARD, the head trader: one decision from five reports.

PICARD chooses exactly one of TRADE, WAIT or REJECT. ODIN's veto is final, the
approved plan is passed through unchanged, and disagreement between agents is
recorded as dissent instead of being averaged away.
"""

from trading_desk.models import (
    ChartReport,
    Classification,
    Decision,
    DecisionAction,
    HypeRisk,
    NewsReport,
    RiskReport,
    RiskVerdict,
    ScoutReport,
    ScoutVerdict,
    SentimentReport,
    SetupStrategy,
    TradeSide,
)


def decide(
    scout_report: ScoutReport,
    chart: ChartReport,
    news_report: NewsReport,
    sentiment_report: SentimentReport,
    risk_report: RiskReport,
) -> Decision:
    """Combine the five reports into one decision: TRADE, WAIT or REJECT.

    ODIN's REJECT is final and its REVISE becomes WAIT. An approved idea still waits if HAWKEYE has
    not rated the symbol WATCH (a Bollinger reversion instead needs usable data and no heavy-volume
    breakdown), if NEO's own counter-case is bearish, if SNOWDEN reports bearish news, or if FAMA
    reports high hype risk. Otherwise the decision is TRADE and carries ODIN's plan unchanged.
    Disagreement between agents is always recorded as dissent.
    """
    sources = [
        f"HAWKEYE {scout_report.verdict}",
        f"NEO thesis {chart.thesis.classification}",
        f"NEO counter-case {chart.counter_case.classification}",
        f"SNOWDEN {news_report.verdict}",
        f"FAMA {sentiment_report.verdict}",
        f"ODIN {risk_report.verdict}",
    ]
    dissent = _collect_dissent(chart, news_report, sentiment_report)

    if risk_report.verdict == RiskVerdict.REJECT:
        failed_rule = ", ".join(risk_report.failed_checks)
        return Decision(
            action=DecisionAction.REJECT,
            summary=f"ODIN rejected the idea: {failed_rule}.",
            sources=sources,
            dissent=dissent,
            failed_rule=failed_rule,
        )
    if risk_report.verdict == RiskVerdict.REVISE:
        return _wait("ODIN asked for a revision.", risk_report.revision, sources, dissent)
    setup = chart.setup
    is_long = setup is None or setup.side is TradeSide.LONG
    if setup is not None and setup.strategy is SetupStrategy.BOLLINGER_REVERSION:
        # A pullback to the band is never a with-the-trend WATCH, so the scout's gate for
        # it is narrower: usable data and no heavy-volume move against the trade.
        has_confirmed_move_against = any(
            signal.volume_confirmed and signal.is_bullish != is_long
            for signal in scout_report.signals
        )
        if scout_report.verdict == ScoutVerdict.NEED_MORE_DATA or has_confirmed_move_against:
            return _wait(
                "HAWKEYE flags a heavy-volume move against the trade or lacks the data to "
                "judge it.",
                "The pullback must hold without a heavy-volume move against it.",
                sources,
                dissent,
            )
    elif scout_report.verdict != ScoutVerdict.WATCH:
        return _wait(
            f"HAWKEYE did not put {scout_report.symbol} on watch.",
            "HAWKEYE must rate the symbol WATCH.",
            sources,
            dissent,
        )
    elif setup is not None and scout_report.side is not None and scout_report.side != setup.side:
        return _wait(
            f"HAWKEYE rates {scout_report.symbol} a {scout_report.side.value.lower()} "
            f"candidate, but NEO's setup is {setup.side.value.lower()}.",
            "The scout and the setup must point the same way.",
            sources,
            dissent,
        )
    # Each objection blocks the side it argues against: an overbought counter-case or
    # bearish news stops a long; an oversold counter-case or bullish news stops a short.
    opposing_view = Classification.BEARISH if is_long else Classification.BULLISH
    if chart.counter_case.classification == opposing_view:
        return _wait(
            f"NEO's own counter-case is {opposing_view.value.lower()}, so the thesis is contested.",
            "Momentum must cool (RSI14 below 70) while the uptrend holds."
            if is_long
            else "Momentum must recover (RSI14 above 30) while the downtrend holds.",
            sources,
            dissent,
        )
    if news_report.verdict == opposing_view:
        return _wait(
            f"SNOWDEN reports {opposing_view.value.lower()} news.",
            "The reported catalyst must clear or be absorbed by price.",
            sources,
            dissent,
        )
    if is_long and sentiment_report.hype_risk == HypeRisk.HIGH:
        return _wait(
            "FAMA reports high hype risk: the crowd is one-sided.",
            "Sentiment must cool to a medium or low hype reading.",
            sources,
            dissent,
        )
    return Decision(
        action=DecisionAction.TRADE,
        summary=(
            f"Enter {risk_report.plan.side} {risk_report.plan.units:g} unit(s) at the next "
            f"open. Stop {risk_report.plan.stop:.4f}, target {risk_report.plan.target:.4f}, "
            f"maximum loss {risk_report.plan.risk_amount:.2f}."
        ),
        plan=risk_report.plan,
        sources=sources,
        dissent=dissent,
    )


def wait_for_review(approved_decision: Decision, reason: str) -> Decision:
    """Turn an approved TRADE into WAIT because the model reviewer did not clear it."""
    return _wait(
        reason,
        "A later bar on which the model reviewer approves the idea.",
        approved_decision.sources + ["MODEL REVIEW blocked"],
        approved_decision.dissent,
    )


def _wait(
    summary: str, required_confirmation: str, sources: list[str], dissent: list[str]
) -> Decision:
    return Decision(
        action=DecisionAction.WAIT,
        summary=summary,
        sources=sources,
        dissent=dissent,
        required_confirmation=required_confirmation,
    )


def _collect_dissent(
    chart: ChartReport, news_report: NewsReport, sentiment_report: SentimentReport
) -> list[str]:
    """Every view that differs from NEO's thesis, and every missing view, stays visible."""
    thesis_classification = chart.thesis.classification
    views = [
        ("NEO counter-case", chart.counter_case.classification, chart.counter_case.summary),
        ("SNOWDEN", news_report.verdict, news_report.summary),
        ("FAMA", sentiment_report.verdict, sentiment_report.summary),
    ]
    return [
        f"{agent_name}={classification} vs thesis={thesis_classification}. {summary}"
        for agent_name, classification, summary in views
        if classification != thesis_classification
    ]
