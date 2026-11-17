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
from trading_desk.llm import ReasoningProvider
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import (
    AccountSnapshot,
    ChartSetup,
    Decision,
    DecisionAction,
    DeskReport,
    GateVerdict,
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
from trading_desk.tools.news import NewsFeed
from trading_desk.tools.sentiment import SentimentFeed

RULES_PROVIDER_NAME = "deterministic-rules"
MODEL_REVIEW_TIMEOUT_SECONDS = 100
# Generous: a cold cache queues one rate-limited request per symbol per week.
NEWS_TIMEOUT_SECONDS = 60
SENTIMENT_TIMEOUT_SECONDS = 120
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
    model_review: GateVerdict | None
    # Written by parallel nodes, so it needs an append reducer.
    trace: Annotated[list[Trace], operator.add]


def build_desk_graph(
    data_provider: MarketDataProvider,
    market_profile: MarketProfile = US_EQUITIES,
    limits: RiskLimits = DEFAULT_RISK_LIMITS,
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
    llm: ReasoningProvider | None = None,
    news_feed: NewsFeed | None = None,
    sentiment_feed: SentimentFeed | None = None,
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

    async def run_snowden(state: DeskState):
        start = perf_counter()
        news_items, error, provider_name = state.get("news_items"), None, RULES_PROVIDER_NAME
        if news_items is None and news_feed is not None:
            provider_name = news_feed.name
            try:
                # The window ends at the evaluation time: later news did not exist yet.
                news_items = await asyncio.wait_for(
                    news_feed.fetch(
                        state["symbol"], state["as_of"] - snowden.NEWS_LOOKBACK, state["as_of"]
                    ),
                    timeout=NEWS_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                # Exception text can contain credential-bearing URLs; record only the class.
                # SNOWDEN then abstains: a failed feed is never read as "no news".
                error = f"News retrieval failed: {type(exc).__name__}"
        news_report = snowden.report(news_items, state["as_of"])
        return {
            "news": news_report,
            "trace": record_trace(state["run_id"], "snowden", start, error, provider_name),
        }

    async def run_fama(state: DeskState):
        start = perf_counter()
        sentiment_reading, error = state.get("sentiment_reading"), None
        provider_name = RULES_PROVIDER_NAME
        if sentiment_reading is None and sentiment_feed is not None:
            provider_name = sentiment_feed.name
            try:
                sentiment_reading = await asyncio.wait_for(
                    sentiment_feed.fetch(
                        state["symbol"], state["as_of"] - snowden.NEWS_LOOKBACK, state["as_of"]
                    ),
                    timeout=SENTIMENT_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                # Class name only, and FAMA abstains: a failed reading is not a calm crowd.
                error = f"Sentiment retrieval failed: {type(exc).__name__}"
        sentiment_report = fama.report(sentiment_reading)
        return {
            "sentiment": sentiment_report,
            "trace": record_trace(state["run_id"], "fama", start, error, provider_name),
        }

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

    async def run_picard(state: DeskState):
        start = perf_counter()
        decision = picard.decide(
            state["scout"], state["chart"], state["news"], state["sentiment"], state["risk"]
        )
        # Only an idea the rules already approved is worth a model call, and the
        # model can only take it away: approve leaves the plan untouched.
        if llm is None or decision.action is not DecisionAction.TRADE:
            return {"decision": decision, "trace": trace(state, "picard", start)}
        try:
            model_review = await asyncio.wait_for(
                llm.review_trade(build_review_context(state, decision)),
                timeout=MODEL_REVIEW_TIMEOUT_SECONDS,
            )
            model_review = GateVerdict.model_validate(model_review.model_dump())
        except Exception as error:
            # Fail closed: no review means no trade. Record the class, never the text.
            failure = f"Model review unavailable: {type(error).__name__}"
            return {
                "decision": picard.wait_for_review(decision, failure),
                "trace": record_trace(state["run_id"], "picard", start, failure, llm.name),
            }
        if not model_review.approve:
            decision = picard.wait_for_review(
                decision, f"Model reviewer vetoed: {model_review.rationale}"
            )
        return {
            "decision": decision,
            "model_review": model_review,
            "trace": record_trace(state["run_id"], "picard", start, None, llm.name),
        }

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


def build_review_context(state: DeskState, decision: Decision) -> dict:
    """What the model reviewer sees: the reports and the plan, with nothing volatile.

    Run ids and wall-clock times are left out so identical situations hash to the
    same cache entry.
    """
    return {
        "symbol": state["symbol"],
        "indicators": state["indicators"].model_dump(),
        "scout": state["scout"].model_dump(mode="json", exclude={"evidence"}),
        "chart_thesis": state["chart"].thesis.summary,
        "chart_counter_case": state["chart"].counter_case.summary,
        "news": state["news"].model_dump(mode="json"),
        "sentiment": state["sentiment"].model_dump(mode="json"),
        "risk_checks": [check.model_dump() for check in state["risk"].checks],
        "plan": decision.plan.model_dump(mode="json"),
        "dissent": decision.dissent,
    }


class Desk:
    """A compiled six-agent graph that can decide many symbols and times cheaply.

    Build one per configuration and reuse it: backtests make thousands of
    decisions and should not recompile the graph for each.
    """

    def __init__(
        self,
        data_provider: MarketDataProvider,
        market_profile: MarketProfile = US_EQUITIES,
        limits: RiskLimits = DEFAULT_RISK_LIMITS,
        setup_rules: SetupRules = DEFAULT_SETUP_RULES,
        llm: ReasoningProvider | None = None,
        news_feed: NewsFeed | None = None,
        sentiment_feed: SentimentFeed | None = None,
    ):
        self.market_profile = market_profile
        self.limits = limits
        self.setup_rules = setup_rules
        self.llm = llm
        self.graph = build_desk_graph(
            data_provider, market_profile, limits, setup_rules, llm, news_feed, sentiment_feed
        )

    async def decide(
        self,
        symbol: str,
        account: AccountSnapshot,
        as_of: datetime | None = None,
        *,
        news_items: Sequence[NewsItem] | None = None,
        sentiment_reading: SentimentReading | None = None,
    ) -> DeskReport:
        """Run all six agents for one symbol and return the complete, validated report."""
        symbol, as_of = validate_request(symbol, as_of)
        state = await self.graph.ainvoke(
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
            market=self.market_profile.name,
            analysis_timestamp=datetime.now(UTC),
            evaluated_as_of=as_of,
            mode="llm" if self.llm else "deterministic",
            data=data,
            market_data_timestamp=data.candles[-1].timestamp if data.candles else None,
            indicators=state["indicators"],
            account=account,
            limits=self.limits,
            scout=state["scout"],
            chart=state["chart"],
            news=state["news"],
            sentiment=state["sentiment"],
            risk=state["risk"],
            decision=state["decision"],
            model_review=state.get("model_review"),
            limitations=state["limitations"]
            + state["blockers"]
            + [entry.error for entry in state["trace"] if entry.error],
            trace=state["trace"],
        )

    async def scan(
        self, symbols: Sequence[str], account: AccountSnapshot, as_of: datetime | None = None
    ) -> list[DeskReport]:
        """Decide every symbol against the same account snapshot, best scout rank first.

        Each report is independent: the caller chooses which approved idea to act on,
        so two approvals here do not mean two positions.
        """
        desk_reports = await asyncio.gather(
            *(self.decide(symbol, account, as_of) for symbol in symbols)
        )
        reports_by_symbol = {desk_report.symbol: desk_report for desk_report in desk_reports}
        ranked_scout_reports = hawkeye.rank_watchlist(
            desk_report.scout for desk_report in desk_reports
        )
        return [reports_by_symbol[scout_report.symbol] for scout_report in ranked_scout_reports]


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
    llm: ReasoningProvider | None = None,
    news_feed: NewsFeed | None = None,
    sentiment_feed: SentimentFeed | None = None,
) -> DeskReport:
    """One-off decision: builds a Desk and asks it once."""
    desk = Desk(data_provider, market_profile, limits, setup_rules, llm, news_feed, sentiment_feed)
    return await desk.decide(
        symbol, account, as_of, news_items=news_items, sentiment_reading=sentiment_reading
    )


async def scan_watchlist(
    symbols: Sequence[str],
    data_provider: MarketDataProvider,
    account: AccountSnapshot,
    as_of: datetime | None = None,
    *,
    market_profile: MarketProfile = US_EQUITIES,
    limits: RiskLimits = DEFAULT_RISK_LIMITS,
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
    llm: ReasoningProvider | None = None,
    news_feed: NewsFeed | None = None,
    sentiment_feed: SentimentFeed | None = None,
) -> list[DeskReport]:
    """One-off ranked scan of a watchlist."""
    desk = Desk(data_provider, market_profile, limits, setup_rules, llm, news_feed, sentiment_feed)
    return await desk.scan(symbols, account, as_of)
