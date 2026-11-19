"""A desk run the terminal can watch and steer.

One `DeskSession` owns one paper venue and at most one run: a replay of history
or live polling. It turns the engine's events and per-bar updates into plain
JSON messages for subscribers, and exposes the few controls the terminal needs:
start, pause, resume, speed and the kill switch. It never changes what the desk
decides; it only paces the loop and halts the venue.

Live polling uses the same engine and the same paper venue as a replay. The desk
still decides on completed daily sessions only: each newly finished session is
evaluated once, and ideas it approves are filled at the following session's open.
Between closes the session only reads the latest traded prices for display. No
order is sent to a broker in either mode.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from trading_desk.agents.neo import DEFAULT_SETUP_RULES, SetupRules
from trading_desk.desk import DEFAULT_RISK_LIMITS
from trading_desk.engine import BarUpdate, run_engine, symbols_to_preload
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import DeskEvent, Model, RiskLimits, VenueEvent
from trading_desk.observability import get_logger
from trading_desk.paper import PaperVenue
from trading_desk.paper.journal import Journal
from trading_desk.tools.historical_data import preload_history
from trading_desk.tools.market_data import MarketDataProvider
from trading_desk.tools.news import NewsFeed
from trading_desk.tools.sentiment import SentimentFeed

logger = get_logger("server.session")

MAXIMUM_BARS_PER_SECOND = 1000.0
RECENT_EVENT_LIMIT = 200
CANDLE_HISTORY_LIMIT = 400
SUBSCRIBER_QUEUE_LIMIT = 2000
# Full candle history and the per-agent trace are large and already shown elsewhere.
REPORT_FIELDS_NOT_PUBLISHED = {"data", "trace"}

# On its first poll the live desk looks this far back for the latest completed session.
# Longer than any market holiday break; a provider with nothing newer is stale.
LIVE_LOOK_BACK = timedelta(days=10)
ONE_MICROSECOND = timedelta(microseconds=1)

Message = dict
Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class SessionStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


class SessionBusyError(RuntimeError):
    """A replay is already running; stop or finish it before starting another."""


class SessionNotRunningError(RuntimeError):
    """The control needs a replay that is running or paused."""


class LiveNotAvailableError(RuntimeError):
    """The configured data source cannot supply live prices (it has no `latest_prices`)."""


class ReplaySettings(Model):
    """What to replay and how fast."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    symbols: list[str] = Field(min_length=1, max_length=50)
    start: AwareDatetime
    end: AwareDatetime
    bars_per_second: float = Field(default=2.0, gt=0, le=MAXIMUM_BARS_PER_SECOND)
    starting_cash: float = Field(default=100_000.0, gt=0)

    @model_validator(mode="after")
    def window_runs_forward(self) -> "ReplaySettings":
        if self.end < self.start:
            raise ValueError("the replay ends before it starts")
        return self


class LiveSettings(Model):
    """What to watch live and how often to ask the data source."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    symbols: list[str] = Field(min_length=1, max_length=50)
    starting_cash: float = Field(default=100_000.0, gt=0)
    # How often the latest traded prices are read for display.
    price_poll_seconds: float = Field(default=15.0, ge=1, le=3600)
    # How often the data source is asked whether a new daily session has completed.
    bar_check_seconds: float = Field(default=300.0, ge=1, le=86_400)


class DeskSession:
    """One desk the terminal can watch and steer: a paper venue and at most one replay.

    It runs the engine as a paced replay, keeps the latest state (bars, reports, equity curve,
    journal lines, account) for snapshots, and publishes every change to subscribers as plain JSON.
    Controls: start, pause, resume, speed, stop and the kill switch. It paces and halts; it never
    changes a decision.
    """

    def __init__(
        self,
        data_provider: MarketDataProvider,
        *,
        market_profile: MarketProfile = US_EQUITIES,
        limits: RiskLimits = DEFAULT_RISK_LIMITS,
        setup_rules: SetupRules = DEFAULT_SETUP_RULES,
        journal: Journal | None = None,
        news_feed: NewsFeed | None = None,
        sentiment_feed: SentimentFeed | None = None,
        news_tone_feed: SentimentFeed | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Clock = lambda: datetime.now(UTC),
    ):
        """Create an idle session bound to its data provider, market, limits, setup rules and
        optional
        journal and feeds. `sleep` is injectable so tests can run a replay instantly.
        """
        self.data_provider = data_provider
        self.news_feed = news_feed
        self.sentiment_feed = sentiment_feed
        self.news_tone_feed = news_tone_feed
        self.market_profile, self.limits, self.setup_rules = market_profile, limits, setup_rules
        self.journal = journal
        self._sleep, self._clock = sleep, clock
        self.status = SessionStatus.IDLE
        self.mode = "replay"
        self.settings: ReplaySettings | LiveSettings | None = None
        self.bars_per_second = 2.0
        self.kill_switch_engaged = False
        self.error: str | None = None
        self.venue: PaperVenue | None = None
        self._task: asyncio.Task | None = None
        self._not_paused = asyncio.Event()
        self._not_paused.set()
        self._subscribers: set[asyncio.Queue] = set()
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self.run_id: str | None = None
        self.bar_index: int | None = None
        self.bar_count: int | None = None
        self.bar_time: datetime | None = None
        self.equity_curve: list[dict] = []
        self.candles_by_symbol: dict[str, deque[dict]] = {}
        self.reports_by_symbol: dict[str, dict] = {}
        self.recent_events: deque[dict] = deque(maxlen=RECENT_EVENT_LIMIT)
        self.latest_prices: dict[str, float] = {}
        self.prices_as_of: datetime | None = None

    # -- subscribers -------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        subscriber_queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers.add(subscriber_queue)
        return subscriber_queue

    def unsubscribe(self, subscriber_queue: asyncio.Queue) -> None:
        self._subscribers.discard(subscriber_queue)

    def _publish(self, message: Message) -> None:
        for subscriber_queue in self._subscribers:
            try:
                subscriber_queue.put_nowait(message)
            except asyncio.QueueFull:
                # A reader that fell this far behind is told to reload the snapshot
                # instead of being fed a stale backlog.
                while not subscriber_queue.empty():
                    subscriber_queue.get_nowait()
                subscriber_queue.put_nowait({"type": "resync"})

    def _publish_status(self) -> None:
        self._publish({"type": "status", **self._status_fields()})

    # -- views -------------------------------------------------------------

    def _status_fields(self) -> dict:
        return {
            "status": self.status.value,
            "mode": self.mode,
            "venue": "paper",
            "kill_switch_engaged": self.kill_switch_engaged,
            "bars_per_second": self.bars_per_second,
            "bar_index": self.bar_index,
            "bar_count": self.bar_count,
            "bar_time": self.bar_time.isoformat() if self.bar_time else None,
            "error": self.error,
        }

    def _account_fields(self) -> dict | None:
        """The account as the terminal shows it: starting cash, equity, cash, open risk, open
        positions, queued symbols and closed trades. None before a replay has started.
        """
        if self.venue is None:
            return None
        return {
            "starting_cash": self.settings.starting_cash,
            **self.venue.account_snapshot().model_dump(mode="json"),
            "positions": [
                position.model_dump(mode="json") for position in self.venue.positions.values()
            ],
            "pending_symbols": sorted(self.venue.pending_orders),
            "closed_trades": [
                closed_trade.model_dump(mode="json") for closed_trade in self.venue.closed_trades
            ],
        }

    def snapshot(self) -> Message:
        """Everything a terminal needs to draw itself from scratch."""
        return {
            "type": "snapshot",
            **self._status_fields(),
            "run_id": self.run_id,
            "market": self.market_profile.name,
            "data_source": self.data_provider.name,
            "news_source": self.news_feed.name if self.news_feed else None,
            "sentiment_source": self.sentiment_feed.name if self.sentiment_feed else None,
            "settings": self.settings.model_dump(mode="json") if self.settings else None,
            "limits": self.limits.model_dump(mode="json"),
            "setup_rules": self.setup_rules.model_dump(mode="json"),
            "account": self._account_fields(),
            # Copies, not the live containers: a queued snapshot must not grow
            # while it waits to be sent, or readers would count bars twice.
            "equity_curve": list(self.equity_curve),
            "candles_by_symbol": {
                symbol: list(candles) for symbol, candles in self.candles_by_symbol.items()
            },
            "reports_by_symbol": dict(self.reports_by_symbol),
            "recent_events": list(self.recent_events),
            "live_available": self.supports_live,
            **self._price_fields(),
        }

    def _price_fields(self) -> dict:
        return {
            "latest_prices": dict(self.latest_prices),
            "prices_as_of": self.prices_as_of.isoformat() if self.prices_as_of else None,
        }

    # -- controls ----------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.status in {SessionStatus.RUNNING, SessionStatus.PAUSED}

    @property
    def supports_live(self) -> bool:
        """Live polling needs a data source that can report the latest traded prices."""
        return callable(getattr(self.data_provider, "latest_prices", None))

    async def start_live(self, settings: LiveSettings) -> None:
        """Begin live polling with a fresh paper account. Refuses if a run is already active or the
        data source has no live prices. If the kill switch is engaged the venue starts halted.
        """
        if self.is_active:
            raise SessionBusyError("a run is already active")
        if not self.supports_live:
            raise LiveNotAvailableError("the data source has no live prices")
        self._reset_run_state()
        self.mode = "live"
        self.settings, self.error = settings, None
        self.run_id = str(uuid4())
        self.venue = PaperVenue(settings.starting_cash, self.market_profile)
        halt_events = self.venue.halt(self._clock()) if self.kill_switch_engaged else []
        self._not_paused.set()
        self.status = SessionStatus.RUNNING
        logger.info(
            "Live polling started run_id=%s symbols=%s data=%s price_poll_seconds=%g "
            "bar_check_seconds=%g starting_cash=%.2f kill_switch=%s",
            self.run_id,
            ",".join(settings.symbols),
            self.data_provider.name,
            settings.price_poll_seconds,
            settings.bar_check_seconds,
            settings.starting_cash,
            self.kill_switch_engaged,
        )
        self._publish(self.snapshot())
        for venue_event in halt_events:
            self._publish_control_event(venue_event)
        self._task = asyncio.create_task(self._run_live(settings, self.venue))

    def use_replay_mode(self) -> None:
        """Label the idle session as a replay desk again. Stop a live run first."""
        if self.is_active:
            raise SessionBusyError("a run is already active")
        self.mode = "replay"
        self._publish_status()

    async def start(self, settings: ReplaySettings) -> None:
        """Begin a new replay with a fresh paper account. Refuses if one is already running. If the
        kill switch is engaged the venue starts halted and the halt is journalled.
        """
        if self.is_active:
            raise SessionBusyError("a replay is already running")
        self._reset_run_state()
        self.mode = "replay"
        self.settings, self.bars_per_second = settings, settings.bars_per_second
        self.error = None
        self.venue = PaperVenue(settings.starting_cash, self.market_profile)
        halt_events = self.venue.halt(settings.start) if self.kill_switch_engaged else []
        self._not_paused.set()
        self.status = SessionStatus.RUNNING
        logger.info(
            "Replay started symbols=%s start=%s end=%s bars_per_second=%g starting_cash=%.2f "
            "kill_switch=%s news=%s sentiment=%s",
            ",".join(settings.symbols),
            settings.start.isoformat(),
            settings.end.isoformat(),
            settings.bars_per_second,
            settings.starting_cash,
            self.kill_switch_engaged,
            self.news_feed.name if self.news_feed else "none",
            self.sentiment_feed.name if self.sentiment_feed else "none",
        )
        self._publish(self.snapshot())
        # Journal the halt so a run that takes no ideas says why.
        for venue_event in halt_events:
            self._publish_control_event(venue_event)
        self._task = asyncio.create_task(self._run(settings, self.venue))

    def pause(self) -> None:
        if self.status is not SessionStatus.RUNNING:
            raise SessionNotRunningError("only a running replay can be paused")
        self._not_paused.clear()
        self.status = SessionStatus.PAUSED
        logger.info("Replay paused run_id=%s bar_index=%s", self.run_id, self.bar_index)
        self._publish_status()

    def resume(self) -> None:
        if self.status is not SessionStatus.PAUSED:
            raise SessionNotRunningError("only a paused replay can be resumed")
        self.status = SessionStatus.RUNNING
        self._not_paused.set()
        logger.info("Replay resumed run_id=%s bar_index=%s", self.run_id, self.bar_index)
        self._publish_status()

    def set_speed(self, bars_per_second: float) -> None:
        if not 0 < bars_per_second <= MAXIMUM_BARS_PER_SECOND:
            raise ValueError(f"speed must be above 0 and at most {MAXIMUM_BARS_PER_SECOND:g}")
        self.bars_per_second = bars_per_second
        logger.info("Replay speed set bars_per_second=%g", bars_per_second)
        self._publish_status()

    def set_kill_switch(self, engaged: bool) -> None:
        """Engage: cancel queued orders and take no new ideas. Open positions keep
        their stops. Release: allow new ideas again from the next bar."""
        self.kill_switch_engaged = engaged
        logger.warning(
            "Kill switch %s run_id=%s bar_time=%s",
            "ENGAGED" if engaged else "released",
            self.run_id,
            self.bar_time.isoformat() if self.bar_time else None,
        )
        if self.venue is not None:
            if engaged and not self.venue.is_halted:
                halted_at = self.bar_time or self._clock()
                for venue_event in self.venue.halt(halted_at):
                    self._publish_control_event(venue_event)
            elif not engaged:
                self.venue.resume()
        self._publish_status()

    async def stop(self) -> None:
        """Cancel the running replay, if any, and wait for it to wind down."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def wait_until_finished(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    # -- the run -----------------------------------------------------------

    async def _run(self, settings: ReplaySettings, venue: PaperVenue) -> None:
        """Run the engine to the end of the replay and set the final status: FINISHED, IDLE if it
        was
        stopped, or FAILED with only the exception's class name.
        """
        try:
            await run_engine(
                settings.symbols,
                self.data_provider,
                venue,
                start=settings.start,
                end=settings.end,
                market_profile=self.market_profile,
                limits=self.limits,
                setup_rules=self.setup_rules,
                journal=self.journal,
                on_event=self._on_event,
                on_bar=self._on_bar,
                news_feed=self.news_feed,
                sentiment_feed=self.sentiment_feed,
                news_tone_feed=self.news_tone_feed,
            )
            self.status = SessionStatus.FINISHED
            logger.info(
                "Replay finished run_id=%s bars=%s final_equity=%.2f closed_trades=%d",
                self.run_id,
                self.bar_count,
                venue.equity,
                len(venue.closed_trades),
            )
        except asyncio.CancelledError:
            self.status = SessionStatus.IDLE
            logger.info("Replay stopped run_id=%s bar_index=%s", self.run_id, self.bar_index)
            raise
        except Exception as exc:
            # Exception text can carry credential-bearing URLs: the published state keeps
            # only the class. The full stack trace goes to the log, which redacts secrets.
            self.status, self.error = SessionStatus.FAILED, type(exc).__name__
            logger.exception(
                "Replay failed run_id=%s bar_index=%s bar_time=%s",
                self.run_id,
                self.bar_index,
                self.bar_time.isoformat() if self.bar_time else None,
            )
        finally:
            self._not_paused.set()
            self._publish({"type": "account", "account": self._account_fields()})
            self._publish_status()

    async def _run_live(self, settings: LiveSettings, venue: PaperVenue) -> None:
        """Poll until stopped: check for a newly completed session every `bar_check_seconds`
        and run the engine over it, and read the latest prices every `price_poll_seconds` in
        between. A failed poll is logged and retried on the next round; it never ends the run.
        """
        last_bar_time: datetime | None = None
        events_recorded = 0
        price_polls_per_bar_check = max(
            1, round(settings.bar_check_seconds / settings.price_poll_seconds)
        )
        try:
            while True:
                await self._not_paused.wait()
                try:
                    last_bar_time, new_event_count = await self._process_new_sessions(
                        settings, venue, last_bar_time, events_recorded
                    )
                    events_recorded += new_event_count
                    if self.error is not None:
                        self.error = None
                        self._publish_status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Class name only in what is published; the redacted trace is in the log.
                    self.error = type(exc).__name__
                    logger.warning(
                        "Live session check failed run_id=%s; retrying on the next poll",
                        self.run_id,
                        exc_info=True,
                    )
                    self._publish_status()
                for _ in range(price_polls_per_bar_check):
                    await self._poll_latest_prices(settings.symbols)
                    await self._sleep(settings.price_poll_seconds)
                    await self._not_paused.wait()
        except asyncio.CancelledError:
            self.status = SessionStatus.IDLE
            logger.info(
                "Live polling stopped run_id=%s sessions_processed=%d equity=%.2f",
                self.run_id,
                len(self.equity_curve),
                venue.equity,
            )
            raise
        finally:
            self._not_paused.set()
            self._publish({"type": "account", "account": self._account_fields()})
            self._publish_status()

    async def _process_new_sessions(
        self,
        settings: LiveSettings,
        venue: PaperVenue,
        last_bar_time: datetime | None,
        events_recorded: int,
    ) -> tuple[datetime | None, int]:
        """Fetch the daily history once and run the engine over any session completed since
        `last_bar_time`. On the first call only the latest completed session is acted on, and
        the earlier history is loaded for the chart without being traded. Returns the newest
        bar time processed so far and how many events this call recorded.
        """
        now = self._clock()
        is_first_check = last_bar_time is None
        history_provider = await preload_history(
            self.data_provider,
            symbols_to_preload(settings.symbols, self.market_profile, self.setup_rules),
        )
        if is_first_check:
            await self._load_chart_history(history_provider, settings.symbols)
        engine_result = await run_engine(
            settings.symbols,
            history_provider,
            venue,
            start=now - LIVE_LOOK_BACK if is_first_check else last_bar_time + ONE_MICROSECOND,
            end=now,
            market_profile=self.market_profile,
            limits=self.limits,
            setup_rules=self.setup_rules,
            journal=self.journal,
            on_event=self._on_event,
            on_bar=self._on_live_bar,
            news_feed=self.news_feed,
            sentiment_feed=self.sentiment_feed,
            news_tone_feed=self.news_tone_feed,
            liquidate_at_end=False,
            run_id=self.run_id,
            first_event_sequence=events_recorded,
            maximum_bars=1 if is_first_check else None,
        )
        if engine_result.equity_curve:
            last_bar_time = engine_result.equity_curve[-1].timestamp
            logger.info(
                "Live session processed run_id=%s bar_time=%s sessions=%d equity=%.2f",
                self.run_id,
                last_bar_time.isoformat(),
                engine_result.bars_processed,
                venue.equity,
            )
        return last_bar_time, len(engine_result.events)

    async def _load_chart_history(self, history_provider, symbols: list[str]) -> None:
        """Fill the chart with each symbol's history up to, but not including, the latest
        completed session; that one arrives as a normal bar when the engine processes it.
        """
        candles_by_symbol = {
            symbol: (await history_provider.fetch(symbol)).candles for symbol in symbols
        }
        latest_bar_times = [
            candles[-1].timestamp for candles in candles_by_symbol.values() if candles
        ]
        if not latest_bar_times:
            return
        latest_bar_time = max(latest_bar_times)
        for symbol, candles in candles_by_symbol.items():
            earlier_candles = [candle for candle in candles if candle.timestamp < latest_bar_time]
            self.candles_by_symbol[symbol] = deque(
                (candle.model_dump(mode="json") for candle in earlier_candles),
                maxlen=CANDLE_HISTORY_LIMIT,
            )
        self._publish(self.snapshot())

    async def _poll_latest_prices(self, symbols: list[str]) -> None:
        """Read the latest traded prices for display and publish them. A failure is logged and
        the previous prices stay; a missing price is never replaced with a guess.
        """
        try:
            latest_prices = await self.data_provider.latest_prices(list(symbols))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Live price poll failed run_id=%s", self.run_id, exc_info=True)
            return
        self.latest_prices.update(latest_prices)
        self.prices_as_of = self._clock()
        logger.debug("Live prices run_id=%s symbols=%d", self.run_id, len(latest_prices))
        self._publish({"type": "prices", **self._price_fields()})

    async def _on_live_bar(self, bar_update: BarUpdate) -> None:
        """Per-bar hook for live polling: store and publish the bar. There is no pacing delay;
        the wait between sessions is the polling loop's.
        """
        self._record_bar(bar_update)

    def _on_event(self, event: DeskEvent) -> None:
        self.run_id = event.run_id
        event_fields = event.model_dump(mode="json")
        self.recent_events.append(event_fields)
        self._publish({"type": "event", "event": event_fields})

    def _publish_control_event(self, venue_event: VenueEvent) -> None:
        """Publish a venue event raised by a control (such as the kill switch) as a journal line."""
        event_fields = {
            "run_id": self.run_id,
            "sequence": None,
            "timestamp": venue_event.timestamp.isoformat(),
            "symbol": venue_event.symbol,
            "agent": "VENUE",
            "kind": venue_event.kind.value,
            "reason": venue_event.reason,
            "detail": {},
        }
        self.recent_events.append(event_fields)
        self._publish({"type": "event", "event": event_fields})

    async def _on_bar(self, bar_update: BarUpdate) -> None:
        """Per-bar hook: store the bar's candles, equity point and reports, publish them, then wait
        for the pacing delay and for any pause to end.
        """
        self._record_bar(bar_update)
        await self._sleep(1.0 / self.bars_per_second)
        await self._not_paused.wait()

    def _record_bar(self, bar_update: BarUpdate) -> None:
        """Store one finished bar (candles, equity point, desk reports) and publish it."""
        self.bar_time = bar_update.bar_time
        equity_fields = bar_update.equity_point.model_dump(mode="json")
        self.equity_curve.append(equity_fields)
        if self.mode == "live":
            # A live run has no known end: count the sessions seen so far.
            self.bar_index, self.bar_count = len(self.equity_curve) - 1, None
        else:
            self.bar_index, self.bar_count = bar_update.bar_index, bar_update.bar_count
        candle_fields_by_symbol = {
            symbol: candle.model_dump(mode="json")
            for symbol, candle in bar_update.candles_by_symbol.items()
        }
        for symbol, candle_fields in candle_fields_by_symbol.items():
            self.candles_by_symbol.setdefault(symbol, deque(maxlen=CANDLE_HISTORY_LIMIT)).append(
                candle_fields
            )
        report_fields_by_symbol = {
            desk_report.symbol: desk_report.model_dump(
                mode="json", exclude=REPORT_FIELDS_NOT_PUBLISHED
            )
            for desk_report in bar_update.desk_reports
        }
        self.reports_by_symbol.update(report_fields_by_symbol)
        self._publish(
            {
                "type": "bar",
                **self._status_fields(),
                "equity_point": equity_fields,
                "candles_by_symbol": candle_fields_by_symbol,
                "reports_by_symbol": report_fields_by_symbol,
                "account": self._account_fields(),
            }
        )
