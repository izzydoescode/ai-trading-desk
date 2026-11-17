"""The six-agent decision graph: one symbol, one evaluation time, one decision.

    market_data -> quality_audit -> HAWKEYE | NEO | SNOWDEN | FAMA -> ODIN -> PICARD

The four analysts run in parallel on the same validated data. ODIN then applies
the hard risk rules and PICARD makes the single final call. Every step here is
deterministic; model commentary is layered on separately and can never change a
number or overturn ODIN.
"""

import asyncio
import operator
from collections.abc import Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from trading_desk.agents import fama, hawkeye, neo, odin, picard, snowden
from trading_desk.agents.fama import SentimentReading
from trading_desk.agents.neo import DEFAULT_SETUP_RULES, SetupRules
from trading_desk.graph import (
    audit_data_quality,
    load_market_data,
    record_trace,
    validate_request,
)
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import (
    AccountSnapshot,
    ChartSetup,
    Decision,
    DeskReport,
    Indicators,
    MarketData,
    NewsItem,
    NewsReport,
    RiskLimits,
    RiskReport,
    ScoutReport,
    SentimentReport,
    Trace,
)
from trading_desk.tools.market_data import MarketDataProvider

RULES_PROVIDER_NAME = "deterministic-rules"
ANALYST_NODE_NAMES = ["hawkeye", "neo", "snowden", "fama"]
DEFAULT_RISK_LIMITS = RiskLimits()


class DeskState(TypedDict, total=False):
    run_id: str
    symbol: str
    as_of: datetime
    account: AccountSnapshot
    news_items: Sequence[NewsItem] | None
    sentiment_reading: SentimentReading | None
    data: MarketData
    indicators: Indicators
    blockers: list[str]
    limitations: list[str]
    scout: ScoutReport
    chart: ChartSetup
    news: NewsReport
    sentiment: SentimentReport
    risk: RiskReport
    decision: Decision
    # Written by parallel nodes, so it needs an append reducer.
    trace: Annotated[list[Trace], operator.add]


def build_desk_graph(
    data_provider: MarketDataProvider,
    market_profile: MarketProfile = US_EQUITIES,
    limits: RiskLimits = DEFAULT_RISK_LIMITS,
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
):
    def trace(state: DeskState, agent: str, start: float, error: str | None = None) -> list[Trace]:
        return record_trace(state["run_id"], agent, start, error, RULES_PROVIDER_NAME)

    async def retrieve_market_data(state: DeskState):
        start = perf_counter()
        data, indicators, error = await load_market_data(
            data_provider, state["symbol"], state["as_of"]
        )
        return {
            "data": data,
            "indicators": indicators,
            "trace": record_trace(state["run_id"], "market_data", start, error, data_provider.name),
        }

    def audit_quality(state: DeskState):
        start = perf_counter()
        blockers, limitations = audit_data_quality(state["data"], state["as_of"], market_profile)
        limitations.append("Long setups only; scoring and levels are uncalibrated rules.")
        return {
            "blockers": blockers,
            "limitations": limitations,
            "trace": trace(
                state, "quality_audit", start, "Data quality gate failed" if blockers else None
            ),
        }

    def run_hawkeye(state: DeskState):
        start = perf_counter()
        scout_report = hawkeye.scout(state["symbol"], state["indicators"], state["blockers"])
        return {"scout": scout_report, "trace": trace(state, "hawkeye", start)}

    def run_neo(state: DeskState):
        start = perf_counter()
        setup = neo.build_setup(state["indicators"], state["blockers"], setup_rules)
        return {"chart": setup, "trace": trace(state, "neo", start)}

    def run_snowden(state: DeskState):
        start = perf_counter()
        news_report = snowden.report(state.get("news_items"), state["as_of"])
        return {"news": news_report, "trace": trace(state, "snowden", start)}

    def run_fama(state: DeskState):
        start = perf_counter()
        sentiment_report = fama.report(state.get("sentiment_reading"))
        return {"sentiment": sentiment_report, "trace": trace(state, "fama", start)}

    def run_odin(state: DeskState):
        start = perf_counter()
        risk_report = odin.evaluate(
            state["symbol"],
            state["chart"],
            state["account"],
            limits,
            market_profile,
            state["blockers"],
        )
        return {"risk": risk_report, "trace": trace(state, "odin", start)}

    def run_picard(state: DeskState):
        start = perf_counter()
        decision = picard.decide(
            state["scout"], state["chart"], state["news"], state["sentiment"], state["risk"]
        )
        return {"decision": decision, "trace": trace(state, "picard", start)}

    builder = StateGraph(DeskState)
    for node_name, node in [
        ("market_data", retrieve_market_data),
        ("quality_audit", audit_quality),
        ("hawkeye", run_hawkeye),
        ("neo", run_neo),
        ("snowden", run_snowden),
        ("fama", run_fama),
        ("odin", run_odin),
        ("picard", run_picard),
    ]:
        builder.add_node(node_name, node)
    builder.add_edge(START, "market_data")
    builder.add_edge("market_data", "quality_audit")
    for analyst_node_name in ANALYST_NODE_NAMES:
        builder.add_edge("quality_audit", analyst_node_name)
    builder.add_edge(ANALYST_NODE_NAMES, "odin")
    builder.add_edge("odin", "picard")
    builder.add_edge("picard", END)
    return builder.compile()


async def run_desk_decision(
    symbol: str,
    data_provider: MarketDataProvider,
    account: AccountSnapshot,
    as_of: datetime | None = None,
    *,
    market_profile: MarketProfile = US_EQUITIES,
    limits: RiskLimits = DEFAULT_RISK_LIMITS,
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
    news_items: Sequence[NewsItem] | None = None,
    sentiment_reading: SentimentReading | None = None,
) -> DeskReport:
    """Run all six agents for one symbol and return the complete, validated report."""
    symbol, as_of = validate_request(symbol, as_of)
    graph = build_desk_graph(data_provider, market_profile, limits, setup_rules)
    state = await graph.ainvoke(
        {
            "run_id": str(uuid4()),
            "symbol": symbol,
            "as_of": as_of,
            "account": account,
            "news_items": news_items,
            "sentiment_reading": sentiment_reading,
            "trace": [],
        }
    )
    data = state["data"]
    return DeskReport(
        run_id=state["run_id"],
        symbol=symbol,
        market=market_profile.name,
        analysis_timestamp=datetime.now(UTC),
        evaluated_as_of=as_of,
        mode="deterministic",
        data=data,
        market_data_timestamp=data.candles[-1].timestamp if data.candles else None,
        indicators=state["indicators"],
        account=account,
        limits=limits,
        scout=state["scout"],
        chart=state["chart"],
        news=state["news"],
        sentiment=state["sentiment"],
        risk=state["risk"],
        decision=state["decision"],
        limitations=state["limitations"]
        + state["blockers"]
        + [entry.error for entry in state["trace"] if entry.error],
        trace=state["trace"],
    )


async def scan_watchlist(
    symbols: Sequence[str],
    data_provider: MarketDataProvider,
    account: AccountSnapshot,
    as_of: datetime | None = None,
    **decision_options,
) -> list[DeskReport]:
    """Decide every symbol against the same account snapshot, best scout rank first.

    Each report is independent: the caller chooses which approved idea to act on,
    so two approvals here do not mean two positions.
    """
    desk_reports = await asyncio.gather(
        *(
            run_desk_decision(symbol, data_provider, account, as_of, **decision_options)
            for symbol in symbols
        )
    )
    reports_by_symbol = {desk_report.symbol: desk_report for desk_report in desk_reports}
    ranked_scout_reports = hawkeye.rank_watchlist(desk_report.scout for desk_report in desk_reports)
    return [reports_by_symbol[scout_report.symbol] for scout_report in ranked_scout_reports]
