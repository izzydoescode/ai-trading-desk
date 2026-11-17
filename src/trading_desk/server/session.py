"""A desk run the terminal can watch and steer.

One `DeskSession` owns one paper venue and at most one running replay. It turns
the engine's events and per-bar updates into plain JSON messages for
subscribers, and exposes the few controls the terminal needs: start, pause,
resume, speed and the kill switch. It never changes what the desk decides; it
only paces the loop and halts the venue.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from trading_desk.agents.neo import DEFAULT_SETUP_RULES, SetupRules
from trading_desk.desk import DEFAULT_RISK_LIMITS
from trading_desk.engine import BarUpdate, run_engine
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import DeskEvent, Model, RiskLimits, VenueEvent
from trading_desk.paper import PaperVenue
from trading_desk.paper.journal import Journal
from trading_desk.tools.market_data import MarketDataProvider

MAXIMUM_BARS_PER_SECOND = 1000.0
RECENT_EVENT_LIMIT = 200
CANDLE_HISTORY_LIMIT = 400
SUBSCRIBER_QUEUE_LIMIT = 2000
# Full candle history and the per-agent trace are large and already shown elsewhere.
REPORT_FIELDS_NOT_PUBLISHED = {"data", "trace"}

Message = dict
Sleeper = Callable[[float], Awaitable[None]]


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


class DeskSession:
    def __init__(
        self,
        data_provider: MarketDataProvider,
        *,
        market_profile: MarketProfile = US_EQUITIES,
        limits: RiskLimits = DEFAULT_RISK_LIMITS,
        setup_rules: SetupRules = DEFAULT_SETUP_RULES,
        journal: Journal | None = None,
        sleep: Sleeper = asyncio.sleep,
    ):
        self.data_provider = data_provider
        self.market_profile, self.limits, self.setup_rules = market_profile, limits, setup_rules
        self.journal = journal
        self._sleep = sleep
        self.status = SessionStatus.IDLE
        self.settings: ReplaySettings | None = None
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
            "mode": "replay",
            "venue": "paper",
            "kill_switch_engaged": self.kill_switch_engaged,
            "bars_per_second": self.bars_per_second,
            "bar_index": self.bar_index,
            "bar_count": self.bar_count,
            "bar_time": self.bar_time.isoformat() if self.bar_time else None,
            "error": self.error,
        }

    def _account_fields(self) -> dict | None:
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
        }

    # -- controls ----------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.status in {SessionStatus.RUNNING, SessionStatus.PAUSED}

    async def start(self, settings: ReplaySettings) -> None:
        if self.is_active:
            raise SessionBusyError("a replay is already running")
        self._reset_run_state()
        self.settings, self.bars_per_second = settings, settings.bars_per_second
        self.error = None
        self.venue = PaperVenue(settings.starting_cash, self.market_profile)
        if self.kill_switch_engaged:
            self.venue.halt(settings.start)
        self._not_paused.set()
        self.status = SessionStatus.RUNNING
        self._publish(self.snapshot())
        self._task = asyncio.create_task(self._run(settings, self.venue))

    def pause(self) -> None:
        if self.status is not SessionStatus.RUNNING:
            raise SessionNotRunningError("only a running replay can be paused")
        self._not_paused.clear()
        self.status = SessionStatus.PAUSED
        self._publish_status()

    def resume(self) -> None:
        if self.status is not SessionStatus.PAUSED:
            raise SessionNotRunningError("only a paused replay can be resumed")
        self.status = SessionStatus.RUNNING
        self._not_paused.set()
        self._publish_status()

    def set_speed(self, bars_per_second: float) -> None:
        if not 0 < bars_per_second <= MAXIMUM_BARS_PER_SECOND:
            raise ValueError(f"speed must be above 0 and at most {MAXIMUM_BARS_PER_SECOND:g}")
        self.bars_per_second = bars_per_second
        self._publish_status()

    def set_kill_switch(self, engaged: bool) -> None:
        """Engage: cancel queued orders and take no new ideas. Open positions keep
        their stops. Release: allow new ideas again from the next bar."""
        self.kill_switch_engaged = engaged
        if self.venue is not None:
            if engaged and not self.venue.is_halted:
                halted_at = self.bar_time or datetime.now(UTC)
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
            )
            self.status = SessionStatus.FINISHED
        except asyncio.CancelledError:
            self.status = SessionStatus.IDLE
            raise
        except Exception as exc:
            # Exception text can carry credential-bearing URLs: keep only the class.
            self.status, self.error = SessionStatus.FAILED, type(exc).__name__
        finally:
            self._not_paused.set()
            self._publish({"type": "account", "account": self._account_fields()})
            self._publish_status()

    def _on_event(self, event: DeskEvent) -> None:
        self.run_id = event.run_id
        event_fields = event.model_dump(mode="json")
        self.recent_events.append(event_fields)
        self._publish({"type": "event", "event": event_fields})

    def _publish_control_event(self, venue_event: VenueEvent) -> None:
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
        self.bar_index, self.bar_count = bar_update.bar_index, bar_update.bar_count
        self.bar_time = bar_update.bar_time
        equity_fields = bar_update.equity_point.model_dump(mode="json")
        self.equity_curve.append(equity_fields)
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
        await self._sleep(1.0 / self.bars_per_second)
        await self._not_paused.wait()
