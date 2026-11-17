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

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from trading_desk.agents.neo import DEFAULT_SETUP_RULES, SetupRules, needs_market_trend
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
    TradeSide,
    VenueEvent,
    VenueEventKind,
)
from trading_desk.observability import get_logger
from trading_desk.paper.journal import Journal
from trading_desk.paper.venue import PaperVenue
from trading_desk.tools.historical_data import preload_history
from trading_desk.tools.indicators import bollinger_bands
from trading_desk.tools.market_data import MarketDataProvider
from trading_desk.tools.news import NewsFeed
from trading_desk.tools.sentiment import SentimentFeed

logger = get_logger("engine")

EventListener = Callable[[DeskEvent], None]


class EquityPoint(Model):
    timestamp: datetime
    equity: float
    has_open_position: bool


@dataclass
class BarUpdate:
    """Everything that happened on one bar, handed to the per-bar hook."""

    bar_index: int
    bar_count: int
    bar_time: datetime
    equity_point: EquityPoint
    candles_by_symbol: dict[str, Candle]
    desk_reports: list[DeskReport]


BarListener = Callable[[BarUpdate], Awaitable[None]]


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

    def __init__(
        self,
        run_id: str,
        journal: Journal | None,
        listener: EventListener | None,
        first_sequence: int = 0,
    ):
        self.run_id, self.journal, self.listener = run_id, journal, listener
        self.first_sequence = first_sequence
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
        """Record one desk event with the next sequence number and pass it to the journal and the
        listener.
        """
        event = DeskEvent(
            run_id=self.run_id,
            sequence=self.first_sequence + len(self.events),
            timestamp=timestamp,
            symbol=symbol,
            agent=agent,
            kind=kind,
            reason=reason,
            detail=detail,
        )
        self.events.append(event)
        # Trades, fills, exits and data errors are worth a line each; routine skips and
        # rejections are only shown when debugging.
        is_notable = agent in {"VENUE", "DATA"} or kind == "TRADE"
        logger.log(
            logging.INFO if is_notable else logging.DEBUG,
            "run_id=%s bar=%s symbol=%s agent=%s kind=%s reason=%s detail=%s",
            self.run_id,
            timestamp.isoformat(),
            symbol,
            agent,
            kind,
            reason,
            detail,
        )
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


def symbols_to_preload(
    symbols: Sequence[str], market_profile: MarketProfile, setup_rules: SetupRules
) -> list[str]:
    """The symbols whose history a run needs: the ones it trades, plus the benchmark when the
    market-trend rule has to read it.
    """
    if not needs_market_trend(setup_rules):
        return list(symbols)
    return list(dict.fromkeys([*symbols, market_profile.benchmark_symbol]))


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
    on_bar: BarListener | None = None,
    news_feed: NewsFeed | None = None,
    sentiment_feed: SentimentFeed | None = None,
    news_tone_feed: SentimentFeed | None = None,
    liquidate_at_end: bool = True,
    run_id: str | None = None,
    first_event_sequence: int = 0,
    maximum_bars: int | None = None,
) -> EngineResult:
    """Drive the desk over every bar in `[start, end]` and return what happened.

    `on_bar` is awaited once per bar after the bar is fully processed. A replay
    uses it to pace, pause and publish; it cannot change what the desk decided.

    Live polling calls this repeatedly with the same venue, each time over the
    window since the last bar it processed. For that it passes its own `run_id`
    and `first_event_sequence` so the calls form one continuous journal, keeps
    positions open with `liquidate_at_end=False`, and on its first call uses
    `maximum_bars=1` to act on the latest completed session only instead of
    trading its way through the look-back window.
    """
    if end < start:
        raise ValueError("the run ends before it starts")
    if maximum_bars is not None and maximum_bars < 1:
        raise ValueError("maximum_bars must be at least 1")
    run_id = run_id or str(uuid4())
    recorder = _EventRecorder(run_id, journal, on_event, first_event_sequence)
    preloaded_provider = await preload_history(
        data_provider, symbols_to_preload(symbols, market_profile, setup_rules)
    )
    desk = Desk(
        preloaded_provider,
        market_profile,
        limits,
        setup_rules,
        news_feed=news_feed,
        sentiment_feed=sentiment_feed,
        news_tone_feed=news_tone_feed,
    )

    bars_by_symbol: dict[str, dict[datetime, Candle]] = {}
    closes_by_symbol: dict[str, list[tuple[datetime, float]]] = {}
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
        closes_by_symbol[symbol] = [(candle.timestamp, candle.close) for candle in history.candles]
    timeline = sorted(
        {
            timestamp
            for symbol_bars in bars_by_symbol.values()
            for timestamp in symbol_bars
            if start <= timestamp <= end
        }
    )
    if maximum_bars is not None:
        timeline = timeline[-maximum_bars:]

    result = EngineResult(run_id=run_id, starting_equity=venue.equity, final_equity=venue.equity)
    trades_already_recorded = len(venue.closed_trades)

    def record_new_trades() -> None:
        nonlocal trades_already_recorded
        for closed_trade in venue.closed_trades[trades_already_recorded:]:
            result.closed_trades.append(closed_trade)
            if journal is not None:
                journal.record_trade(run_id, closed_trade)
        trades_already_recorded = len(venue.closed_trades)

    async def publish_bar(
        bar_index: int,
        bar_time: datetime,
        symbols_with_a_bar: list[str],
        desk_reports: list[DeskReport],
    ) -> None:
        """Hand the finished bar (its candles, equity point and the desk reports acted on) to the
        per-
        bar hook, if there is one.
        """
        if on_bar is None:
            return
        await on_bar(
            BarUpdate(
                bar_index=bar_index,
                bar_count=len(timeline),
                bar_time=bar_time,
                equity_point=result.equity_curve[-1],
                candles_by_symbol={
                    symbol: bars_by_symbol[symbol][bar_time] for symbol in symbols_with_a_bar
                },
                desk_reports=desk_reports,
            )
        )

    for bar_index, bar_time in enumerate(timeline):
        # 1. Settle this bar before thinking about anything new.
        symbols_with_a_bar = [
            symbol for symbol in bars_by_symbol if bar_time in bars_by_symbol[symbol]
        ]
        for symbol in symbols_with_a_bar:
            position = venue.positions.get(symbol)
            if position is not None and position.target_follows_band:
                # "Exit at the next touch of the opposite band" (upper for a long, lower for
                # a short): rest the target at that band as it stood after the PREVIOUS
                # session, a level known before this bar opens.
                closes_before_this_bar = [
                    close for timestamp, close in closes_by_symbol[symbol] if timestamp < bar_time
                ]
                upper_band, lower_band = bollinger_bands(closes_before_this_bar)
                exit_band = upper_band if position.side is TradeSide.LONG else lower_band
                if exit_band is not None and venue.move_target(symbol, exit_band):
                    logger.debug(
                        "run_id=%s bar=%s symbol=%s target moved to its exit band %.4f",
                        run_id,
                        bar_time.isoformat(),
                        symbol,
                        exit_band,
                    )
            for venue_event in venue.process_bar(symbol, bars_by_symbol[symbol][bar_time]):
                recorder.emit_venue_event(venue_event)
        record_new_trades()
        result.equity_curve.append(
            EquityPoint(
                timestamp=bar_time, equity=venue.equity, has_open_position=bool(venue.positions)
            )
        )
        result.bars_processed += 1
        acted_desk_reports: list[DeskReport] = []
        if venue.is_halted:
            await publish_bar(bar_index, bar_time, symbols_with_a_bar, acted_desk_reports)
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
            acted_desk_reports.append(desk_report)
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
                if journal is not None:
                    journal.record_decision(run_id, desk_report)
                submit_event = venue.submit(desk_report.decision.plan, bar_time)
                recorder.emit_venue_event(submit_event)
                has_submitted_this_bar = submit_event.kind is VenueEventKind.SUBMIT
        await publish_bar(bar_index, bar_time, symbols_with_a_bar, acted_desk_reports)

    if timeline and liquidate_at_end:
        for venue_event in venue.liquidate(timeline[-1], ExitReason.END_OF_RUN):
            recorder.emit_venue_event(venue_event)
        record_new_trades()
        result.equity_curve[-1] = EquityPoint(
            timestamp=timeline[-1], equity=venue.equity, has_open_position=False
        )
    result.final_equity = venue.equity
    result.events = recorder.events
    logger.log(
        # A live poll that found no new session is routine; a run that processed bars is not.
        logging.INFO if result.bars_processed else logging.DEBUG,
        "Engine run complete run_id=%s symbols=%d bars=%d events=%d closed_trades=%d "
        "starting_equity=%.2f final_equity=%.2f",
        run_id,
        len(symbols),
        result.bars_processed,
        len(result.events),
        len(result.closed_trades),
        result.starting_equity,
        result.final_equity,
    )
    return result
