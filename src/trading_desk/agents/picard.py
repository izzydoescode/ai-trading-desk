"""PICARD, the head trader: one decision from five reports.

PICARD chooses exactly one of TRADE, WAIT or REJECT. ODIN's veto is final, the
approved plan is passed through unchanged, and disagreement between agents is
recorded as dissent instead of being averaged away.
"""

from trading_desk.models import (
    ChartSetup,
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
)


def decide(
    scout_report: ScoutReport,
    setup: ChartSetup,
    news_report: NewsReport,
    sentiment_report: SentimentReport,
    risk_report: RiskReport,
) -> Decision:
    sources = [
        f"HAWKEYE {scout_report.verdict}",
        f"NEO thesis {setup.thesis.classification}",
        f"NEO counter-case {setup.counter_case.classification}",
        f"SNOWDEN {news_report.verdict}",
        f"FAMA {sentiment_report.verdict}",
        f"ODIN {risk_report.verdict}",
    ]
    dissent = _collect_dissent(setup, news_report, sentiment_report)

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
    if scout_report.verdict != ScoutVerdict.WATCH:
        return _wait(
            f"HAWKEYE did not put {scout_report.symbol} on watch.",
            "HAWKEYE must rate the symbol WATCH.",
            sources,
            dissent,
        )
    if setup.counter_case.classification == Classification.BEARISH:
        return _wait(
            "NEO's own counter-case is bearish, so the thesis is contested.",
            "Momentum must cool (RSI14 below 70) while the uptrend holds.",
            sources,
            dissent,
        )
    if news_report.verdict == Classification.BEARISH:
        return _wait(
            "SNOWDEN reports bearish news.",
            "The reported catalyst must clear or be absorbed by price.",
            sources,
            dissent,
        )
    if sentiment_report.hype_risk == HypeRisk.HIGH:
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
    setup: ChartSetup, news_report: NewsReport, sentiment_report: SentimentReport
) -> list[str]:
    """Every view that differs from NEO's thesis, and every missing view, stays visible."""
    thesis_classification = setup.thesis.classification
    views = [
        ("NEO counter-case", setup.counter_case.classification, setup.counter_case.summary),
        ("SNOWDEN", news_report.verdict, news_report.summary),
        ("FAMA", sentiment_report.verdict, sentiment_report.summary),
    ]
    return [
        f"{agent_name}={classification} vs thesis={thesis_classification}. {summary}"
        for agent_name, classification, summary in views
        if classification != thesis_classification
    ]
