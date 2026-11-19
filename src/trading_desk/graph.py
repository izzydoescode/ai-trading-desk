"""Bounded explicit workflow with a parallel deterministic quality-audit branch."""

import asyncio
import logging
import operator
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from trading_desk.agents import contrarian, synthesis, technical
from trading_desk.llm import ReasoningProvider
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import Analysis, Classification, Indicators, MarketData, Report, Trace
from trading_desk.tools.indicators import calculate
from trading_desk.tools.market_data import MarketDataProvider

logger = logging.getLogger("trading_desk")

MARKET_DATA_TIMEOUT_SECONDS = 65
REASONING_TIMEOUT_SECONDS = 100
SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]{0,14}")


class State(TypedDict, total=False):
    run_id: str
    symbol: str
    as_of: datetime
    data: MarketData
    indicators: Indicators
    blockers: list[str]
    limitations: list[str]
    technical: Analysis
    contrarian: Analysis
    synthesis: Analysis
    trace: Annotated[list[Trace], operator.add]


# ---------------------------------------------------------------------------
# Building blocks shared by the research graph below and the six-agent desk graph.
# ---------------------------------------------------------------------------


def validate_request(symbol: str, as_of: datetime | None) -> tuple[str, datetime]:
    """Normalise the ticker and evaluation time, rejecting anything unusable."""
    symbol = symbol.upper().strip()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("Invalid ticker symbol")
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Evaluation timestamp must include a timezone")
    if as_of > datetime.now(UTC):
        raise ValueError("Evaluation timestamp cannot be in the future")
    return symbol, as_of


def record_trace(
    run_id: str,
    agent: str,
    start: float,
    error: str | None = None,
    provider: str = "python",
    skipped: bool = False,
) -> list[Trace]:
    """Build and log one trace entry; returned as a list for the append reducer."""
    entry = Trace(
        run_id=run_id,
        agent=agent,
        status="skipped" if skipped else "degraded" if error else "ok",
        duration_ms=round((perf_counter() - start) * 1000, 3),
        provider=provider,
        error=error,
    )
    logger.info(entry.model_dump_json())
    return [entry]


async def load_market_data(
    data_provider: MarketDataProvider, symbol: str, as_of: datetime
) -> tuple[MarketData, Indicators, str | None]:
    """Fetch, validate and trim bars to `as_of`, then compute indicators.

    Returns the data, the indicators and a sanitised error (or None). Failures
    never raise and never produce invented candles.
    """
    error = None
    try:
        data = await asyncio.wait_for(
            data_provider.fetch(symbol), timeout=MARKET_DATA_TIMEOUT_SECONDS
        )
        data = MarketData.model_validate(data.model_dump())
        if data.symbol != symbol:
            raise ValueError("provider symbol mismatch")
        # Never compute indicators using information after the evaluation date.
        data = data.model_copy(
            update={"candles": [candle for candle in data.candles if candle.timestamp <= as_of]}
        )
        if data.errors:
            error = "Market data provider reported errors"
    except Exception as exc:
        # Exception text can contain credential-bearing URLs; record only the class.
        error = f"Market data retrieval failed: {type(exc).__name__}"
        data = MarketData(symbol=symbol, source=data_provider.name, errors=[error])
    try:
        indicators = calculate(data.candles)
    except (ValueError, ArithmeticError) as exc:
        error = f"Indicator calculation rejected input: {type(exc).__name__}"
        data = data.model_copy(update={"errors": data.errors + [error]})
        indicators = Indicators(candle_count=len(data.candles))
    return data, indicators, error


def audit_data_quality(
    data: MarketData, as_of: datetime, market_profile: MarketProfile
) -> tuple[list[str], list[str]]:
    """Return (blockers, limitations). Any blocker forces abstention downstream."""
    blockers = list(data.errors)
    limitations = [
        "Current news, fundamentals and liquidity are not researched.",
        "Daily bars; completeness uses conservative calendar-gap heuristics, "
        "not an exchange holiday calendar.",
    ]
    if data.synthetic:
        limitations.append("Synthetic scenario: not actual AAPL prices or investment research.")
    if "unadjusted" in data.source:
        limitations.append("Unadjusted prices: splits/dividends can distort indicators.")
    minimum_history_candles = market_profile.minimum_history_candles
    if len(data.candles) < minimum_history_candles:
        blockers.append(
            f"Need {minimum_history_candles} valid candles; received {len(data.candles)}; "
            f"missing {minimum_history_candles - len(data.candles)} for SMA50."
        )
    if data.candles:
        age = as_of - data.candles[-1].timestamp
        if age > market_profile.maximum_data_age:
            blockers.append(f"Stale daily data: latest candle is {age.days} calendar days old.")
        maximum_candle_gap = market_profile.maximum_candle_gap
        if any(
            later_candle.timestamp - earlier_candle.timestamp > maximum_candle_gap
            for earlier_candle, later_candle in zip(data.candles, data.candles[1:], strict=False)
        ):
            blockers.append(
                f"Daily series contains a gap longer than {maximum_candle_gap.days} calendar days."
            )
    return blockers, limitations


# ---------------------------------------------------------------------------
# Research graph: technical thesis, independent contrarian review, synthesis.
# ---------------------------------------------------------------------------


def build_graph(
    data_provider: MarketDataProvider,
    llm: ReasoningProvider | None = None,
    market_profile: MarketProfile = US_EQUITIES,
):
    """Assemble the research graph: market data, then the technical thesis and the quality audit in
    parallel, then the contrarian review, then synthesis.

    Each reasoning step computes a rule-based answer first. With no model that answer stands; with
    a model, the model's answer replaces it only if it validates and cites exact indicator values,
    and an abstaining baseline skips the model call.
    """

    def trace(
        state: State,
        agent: str,
        start: float,
        error: str | None = None,
        provider: str = "python",
        skipped: bool = False,
    ) -> list[Trace]:
        return record_trace(state["run_id"], agent, start, error, provider, skipped)

    async def retrieve(state: State):
        start = perf_counter()
        data, indicators, error = await load_market_data(
            data_provider, state["symbol"], state["as_of"]
        )
        return {
            "data": data,
            "indicators": indicators,
            "trace": trace(state, "market_data", start, error, data_provider.name),
        }

    def quality(state: State):
        start = perf_counter()
        blockers, limitations = audit_data_quality(state["data"], state["as_of"], market_profile)
        return {
            "blockers": blockers,
            "limitations": limitations,
            "trace": trace(
                state, "quality_audit", start, "Data quality gate failed" if blockers else None
            ),
        }

    async def reason(state: State, role: str, fallback: Analysis):
        """Run one reasoning role: return the rule-based result, or ask the model and accept its
        answer only if it validates and every cited number matches the computed indicator. Any
        model failure becomes a recorded abstention, never a silent fallback to the rules.
        """
        start, error = perf_counter(), None
        if llm is None:
            result = fallback
        elif fallback.abstain:
            return {
                role: fallback,
                "trace": trace(state, role, start, provider=llm.name, skipped=True),
            }
        else:
            context = {
                "symbol": state["symbol"],
                "data": state["data"].model_dump(mode="json"),
                "indicators": state["indicators"].model_dump(),
                "blockers": state.get("blockers", []),
                "limitations": state.get("limitations", []),
            }
            for dependency in ("technical", "contrarian"):
                if dependency in state:
                    context[dependency] = state[dependency].model_dump(mode="json")
            try:
                result = await asyncio.wait_for(
                    llm.reason(role, context), timeout=REASONING_TIMEOUT_SECONDS
                )
                result = Analysis.model_validate(result.model_dump())
                for item in result.evidence:
                    actual = getattr(state["indicators"], item.metric)
                    if actual is None or abs(actual - item.value) > 1e-8:
                        raise ValueError("ungrounded evidence")
            except Exception as exc:
                error = f"Invalid or unavailable reasoning: {type(exc).__name__}"
                result = technical.abstention(error)
        return {
            role: result,
            "trace": trace(state, role, start, error, llm.name if llm else "deterministic-rules"),
        }

    async def analyst(state: State):
        return await reason(state, "technical", technical.analyze(state["indicators"]))

    async def reviewer(state: State):
        return await reason(
            state,
            "contrarian",
            contrarian.review(state["indicators"], state["technical"], state["blockers"]),
        )

    async def decider(state: State):
        """Synthesis step: reconcile the two analyses, then re-impose policy over any model
        answer. A
        baseline abstention stands, and a bullish-versus-bearish conflict may only be NEUTRAL or
        INSUFFICIENT_DATA.
        """
        baseline = synthesis.synthesize(state["technical"], state["contrarian"], state["blockers"])
        update = await reason(state, "synthesis", baseline)
        # Deterministic constraints have authority over every model, including synthesis.
        if baseline.abstain:
            update["synthesis"] = baseline
        elif {state["technical"].classification, state["contrarian"].classification} == {
            Classification.BULLISH,
            Classification.BEARISH,
        } and update["synthesis"].classification not in {
            Classification.NEUTRAL,
            Classification.INSUFFICIENT_DATA,
        }:
            update["synthesis"] = baseline
            update["trace"][0].status = "degraded"
            update["trace"][0].error = "Directional conflict overridden by deterministic policy"
        return update

    builder = StateGraph(State)
    for name, node in [
        ("market_data", retrieve),
        ("technical", analyst),
        ("quality_audit", quality),
        ("contrarian", reviewer),
        ("synthesis", decider),
    ]:
        builder.add_node(name, node)
    builder.add_edge(START, "market_data")
    builder.add_edge("market_data", "technical")
    builder.add_edge("market_data", "quality_audit")
    builder.add_edge(["technical", "quality_audit"], "contrarian")
    builder.add_edge("contrarian", "synthesis")
    builder.add_edge("synthesis", END)
    return builder.compile()


async def run_research(
    symbol: str,
    data_provider: MarketDataProvider,
    llm: ReasoningProvider | None = None,
    as_of: datetime | None = None,
    market_profile: MarketProfile = US_EQUITIES,
) -> Report:
    """Run the research workflow for one symbol and return the typed report, including any dissent
    between the technical and contrarian views.
    """
    symbol, as_of = validate_request(symbol, as_of)
    state = await build_graph(data_provider, llm, market_profile).ainvoke(
        {"run_id": str(uuid4()), "symbol": symbol, "as_of": as_of, "trace": []}
    )
    dissent = []
    if state["technical"].classification != state["contrarian"].classification:
        dissent.append(
            f"Technical={state['technical'].classification}; "
            f"contrarian={state['contrarian'].classification}. " + state["contrarian"].summary
        )
    data = state["data"]
    decision = state["synthesis"]
    return Report(
        run_id=state["run_id"],
        symbol=symbol,
        analysis_timestamp=datetime.now(UTC),
        evaluated_as_of=as_of,
        mode="llm" if llm else "deterministic",
        data=data,
        market_data_timestamp=data.candles[-1].timestamp if data.candles else None,
        indicators=state["indicators"],
        technical=state["technical"],
        contrarian=state["contrarian"],
        synthesis=decision,
        classification=decision.classification,
        abstained=decision.abstain,
        limitations=state["limitations"]
        + state["blockers"]
        + [t.error for t in state["trace"] if t.error],
        dissent=dissent,
        trace=state["trace"],
    )
