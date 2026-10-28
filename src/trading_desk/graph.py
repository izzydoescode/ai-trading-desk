"""Bounded explicit workflow with a parallel deterministic quality-audit branch."""

import asyncio
import logging
import operator
import re
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Annotated, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from trading_desk.agents import contrarian, synthesis, technical
from trading_desk.llm import ReasoningProvider
from trading_desk.models import Analysis, Classification, Indicators, MarketData, Report, Trace
from trading_desk.tools.indicators import calculate
from trading_desk.tools.market_data import MarketDataProvider

logger = logging.getLogger("trading_desk")


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


def build_graph(data_provider: MarketDataProvider, llm: ReasoningProvider | None = None):
    def trace(
        state: State,
        agent: str,
        start: float,
        error: str | None = None,
        provider: str = "python",
        skipped: bool = False,
    ) -> list[Trace]:
        entry = Trace(
            run_id=state["run_id"],
            agent=agent,
            status="skipped" if skipped else "degraded" if error else "ok",
            duration_ms=round((perf_counter() - start) * 1000, 3),
            provider=provider,
            error=error,
        )
        logger.info(entry.model_dump_json())
        return [entry]

    async def retrieve(state: State):
        start, error = perf_counter(), None
        try:
            data = await asyncio.wait_for(data_provider.fetch(state["symbol"]), timeout=65)
            data = MarketData.model_validate(data.model_dump())
            if data.symbol != state["symbol"]:
                raise ValueError("provider symbol mismatch")
            # Never compute indicators using information after the evaluation date.
            data = data.model_copy(
                update={"candles": [c for c in data.candles if c.timestamp <= state["as_of"]]}
            )
            if data.errors:
                error = "Market data provider reported errors"
        except Exception as exc:
            # Exception text can contain credential-bearing URLs; record only the class.
            error = f"Market data retrieval failed: {type(exc).__name__}"
            data = MarketData(symbol=state["symbol"], source=data_provider.name, errors=[error])
        try:
            indicators = calculate(data.candles)
        except (ValueError, ArithmeticError) as exc:
            error = f"Indicator calculation rejected input: {type(exc).__name__}"
            data = data.model_copy(update={"errors": data.errors + [error]})
            indicators = Indicators(candle_count=len(data.candles))
        return {
            "data": data,
            "indicators": indicators,
            "trace": trace(state, "market_data", start, error, data_provider.name),
        }

    def quality(state: State):
        start = perf_counter()
        data = state["data"]
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
        if len(data.candles) < 50:
            blockers.append(
                f"Need 50 valid candles; received {len(data.candles)}; "
                f"missing {max(0, 50 - len(data.candles))} for SMA50."
            )
        if data.candles:
            age = state["as_of"] - data.candles[-1].timestamp
            if age > timedelta(days=5):
                blockers.append(f"Stale daily data: latest candle is {age.days} calendar days old.")
            if any(
                b.timestamp - a.timestamp > timedelta(days=5)
                for a, b in zip(data.candles, data.candles[1:], strict=False)
            ):
                blockers.append("Daily series contains a gap longer than five calendar days.")
        return {
            "blockers": blockers,
            "limitations": limitations,
            "trace": trace(
                state, "quality_audit", start, "Data quality gate failed" if blockers else None
            ),
        }

    async def reason(state: State, role: str, fallback: Analysis):
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
                result = await asyncio.wait_for(llm.reason(role, context), timeout=100)
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
) -> Report:
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
        raise ValueError("Invalid ticker symbol")
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Evaluation timestamp must include a timezone")
    if as_of > datetime.now(UTC):
        raise ValueError("Evaluation timestamp cannot be in the future")
    state = await build_graph(data_provider, llm).ainvoke(
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
