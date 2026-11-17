"""The single desk loop, shared by backtest, replay and (later) live polling.

For every bar, in order:

1. the venue settles the bar: queued orders fill at its open, stops and targets exit;
2. the six agents decide each symbol using only bars up to and including this one;
3. approved ideas are queued for the NEXT bar's open.

The loop's clock is the bar timestamp, never the wall clock, so the same history
always produces the same result. Only the data provider and the venue change
between modes. Every decision, skip, fill and exit is emitted as a `DeskEvent`
with its reason.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from trading_desk.agents.neo import DEFAULT_SETUP_RULES, SetupRules
from trading_desk.desk import DEFAULT_RISK_LIMITS, Desk
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import (
    Candle,
    ClosedTrade,
    DecisionAction,
    DeskEvent,
    DeskReport,
    ExitReason,
    Model,
    RiskLimits,
    VenueEvent,
    VenueEventKind,
)
from trading_desk.paper.journal import Journal
from trading_desk.paper.venue import PaperVenue
from trading_desk.tools.historical_data import preload_history
from trading_desk.tools.market_data import MarketDataProvider

EventListener = Callable[[DeskEvent], None]


class EquityPoint(Model):
    timestamp: datetime
    equity: float
    has_open_position: bool


@dataclass
class EngineResult:
    run_id: str
    starting_equity: float
    final_equity: float
    equity_curve: list[EquityPoint] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    events: list[DeskEvent] = field(default_factory=list)
    bars_processed: int = 0


class _EventRecorder:
    """Numbers events, keeps them, and forwards them to the journal and listener."""

    def __init__(self, run_id: str, journal: Journal | None, listener: EventListener | None):
        self.run_id, self.journal, self.listener = run_id, journal, listener
        self.events: list[DeskEvent] = []

    def emit(
        self,
        timestamp: datetime,
        symbol: str,
        agent: str,
        kind: str,
        reason: str,
        **detail: str | float | int | bool | None,
    ) -> None:
        event = DeskEvent(
            run_id=self.run_id,
            sequence=len(self.events),
            timestamp=timestamp,
            symbol=symbol,
            agent=agent,
            kind=kind,
            reason=reason,
            detail=detail,
        )
        self.events.append(event)
        if self.journal is not None:
            self.journal.record_event(event)
        if self.listener is not None:
            self.listener(event)

    def emit_venue_event(self, venue_event: VenueEvent) -> None:
        self.emit(
            venue_event.timestamp,
            venue_event.symbol,
            "VENUE",
            venue_event.kind.value,
            venue_event.reason,
            price=venue_event.price,
            units=venue_event.units,
            profit_and_loss=venue_event.profit_and_loss,
        )


def _decision_reason(desk_report: DeskReport) -> str:
    decision = desk_report.decision
    if decision.action is DecisionAction.REJECT:
        return decision.failed_rule
    if decision.action is DecisionAction.WAIT:
        return decision.summary
    return "approved"


async def run_engine(
    symbols: Sequence[str],
    data_provider: MarketDataProvider,
    venue: PaperVenue,
    *,
    start: datetime,
    end: datetime,
    market_profile: MarketProfile = US_EQUITIES,
    limits: RiskLimits = DEFAULT_RISK_LIMITS,
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
    journal: Journal | None = None,
    on_event: EventListener | None = None,
    liquidate_at_end: bool = True,
) -> EngineResult:
    """Drive the desk over every bar in `[start, end]` and return what happened."""
    if end < start:
        raise ValueError("the run ends before it starts")
    run_id = str(uuid4())
    recorder = _EventRecorder(run_id, journal, on_event)
    preloaded_provider = await preload_history(data_provider, symbols)
    desk = Desk(preloaded_provider, market_profile, limits, setup_rules)

    bars_by_symbol: dict[str, dict[datetime, Candle]] = {}
    for symbol in symbols:
        history = await preloaded_provider.fetch(symbol)
        if history.errors or not history.candles:
            recorder.emit(
                start,
                symbol,
                "DATA",
                "DATA_ERROR",
                "; ".join(history.errors) or "no candles returned",
            )
            continue
        bars_by_symbol[symbol] = {candle.timestamp: candle for candle in history.candles}
    timeline = sorted(
        {
            timestamp
            for symbol_bars in bars_by_symbol.values()
            for timestamp in symbol_bars
            if start <= timestamp <= end
        }
    )

    result = EngineResult(run_id=run_id, starting_equity=venue.equity, final_equity=venue.equity)
    trades_already_recorded = len(venue.closed_trades)

    def record_new_trades() -> None:
        nonlocal trades_already_recorded
        for closed_trade in venue.closed_trades[trades_already_recorded:]:
            result.closed_trades.append(closed_trade)
            if journal is not None:
                journal.record_trade(run_id, closed_trade)
        trades_already_recorded = len(venue.closed_trades)

    for bar_time in timeline:
        # 1. Settle this bar before thinking about anything new.
        symbols_with_a_bar = [
            symbol for symbol in bars_by_symbol if bar_time in bars_by_symbol[symbol]
        ]
        for symbol in symbols_with_a_bar:
            for venue_event in venue.process_bar(symbol, bars_by_symbol[symbol][bar_time]):
                recorder.emit_venue_event(venue_event)
        record_new_trades()
        result.equity_curve.append(
            EquityPoint(
                timestamp=bar_time, equity=venue.equity, has_open_position=bool(venue.positions)
            )
        )
        result.bars_processed += 1
        if venue.is_halted:
            continue

        # 2. Decide every symbol we are not already exposed to.
        candidates = []
        for symbol in symbols_with_a_bar:
            if venue.has_exposure(symbol):
                recorder.emit(bar_time, symbol, "ENGINE", "SKIP", "position_open")
            else:
                candidates.append(symbol)
        desk_reports = await desk.scan(candidates, venue.account_snapshot(), bar_time)

        # 3. Act on approvals in rank order, re-checking risk after each submission.
        has_submitted_this_bar = False
        for desk_report in desk_reports:
            if desk_report.decision.action is DecisionAction.TRADE and has_submitted_this_bar:
                # The account changed when the earlier idea was queued: ask ODIN again.
                desk_report = await desk.decide(
                    desk_report.symbol, venue.account_snapshot(), bar_time
                )
            recorder.emit(
                bar_time,
                desk_report.symbol,
                "PICARD",
                desk_report.decision.action.value,
                _decision_reason(desk_report),
                scout_verdict=desk_report.scout.verdict.value,
                scout_score=desk_report.scout.score,
                risk_verdict=desk_report.risk.verdict.value,
            )
            if desk_report.decision.action is DecisionAction.TRADE:
                submit_event = venue.submit(desk_report.decision.plan, bar_time)
                recorder.emit_venue_event(submit_event)
                has_submitted_this_bar = submit_event.kind is VenueEventKind.SUBMIT

    if timeline and liquidate_at_end:
        for venue_event in venue.liquidate(timeline[-1], ExitReason.END_OF_RUN):
            recorder.emit_venue_event(venue_event)
        record_new_trades()
        result.equity_curve[-1] = EquityPoint(
            timestamp=timeline[-1], equity=venue.equity, has_open_position=False
        )
    result.final_equity = venue.equity
    result.events = recorder.events
    return result
