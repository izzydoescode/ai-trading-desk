"""HTTP and WebSocket API for the terminal.

Local use only. There is no login, so the server is meant to bind to the
loopback interface, and every state-changing request and WebSocket connection
from a browser must come from a page served on this machine. That stops another
website open in the same browser from steering the desk.

Nothing here can place a real order: the only venue is the paper venue. "Live"
mode means live market data (polled prices and newly completed sessions); its
trades are simulated by the same paper venue as a replay.
"""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, ConfigDict, Field

from trading_desk.backtest.runner import run_backtest
from trading_desk.graph import SYMBOL_PATTERN
from trading_desk.models import Model
from trading_desk.observability import get_logger
from trading_desk.review import review_trades
from trading_desk.server.session import (
    MAXIMUM_BARS_PER_SECOND,
    DeskSession,
    LiveNotAvailableError,
    LiveSettings,
    ReplaySettings,
    SessionBusyError,
    SessionNotRunningError,
)

logger = get_logger("server.app")

LOCAL_ORIGIN_EXPRESSION = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
LOCAL_ORIGIN_PATTERN = re.compile(LOCAL_ORIGIN_EXPRESSION)
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
WEBSOCKET_POLICY_VIOLATION = 1008


def is_trusted_origin(origin: str | None) -> bool:
    """Browsers always send Origin on cross-site writes and WebSockets; tools do not."""
    return origin is None or LOCAL_ORIGIN_PATTERN.fullmatch(origin) is not None


class RequestModel(Model):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StartReplayRequest(RequestModel):
    """Any field left out falls back to the server's configured default."""

    symbols: list[str] | None = Field(default=None, min_length=1, max_length=50)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    bars_per_second: float | None = Field(default=None, gt=0, le=MAXIMUM_BARS_PER_SECOND)
    starting_cash: float | None = Field(default=None, gt=0)


class SpeedRequest(RequestModel):
    bars_per_second: float = Field(gt=0, le=MAXIMUM_BARS_PER_SECOND)


class KillSwitchRequest(RequestModel):
    engaged: bool


class ModeRequest(RequestModel):
    """Switch between replaying history and polling live data. The optional fields apply to
    live mode and fall back to the server's defaults.
    """

    mode: Literal["replay", "live"]
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=50)
    starting_cash: float | None = Field(default=None, gt=0)


def normalise_symbols(symbols: list[str]) -> list[str]:
    normalised_symbols = [symbol.upper().strip() for symbol in symbols]
    if not all(SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalised_symbols):
        raise HTTPException(status_code=422, detail="Invalid ticker symbol")
    return list(dict.fromkeys(normalised_symbols))


def create_app(
    session: DeskSession,
    default_settings: ReplaySettings,
    ui_directory: Path | None = None,
    default_live_settings: LiveSettings | None = None,
) -> FastAPI:
    """Build the API. When `ui_directory` holds a built terminal (`web/dist`), it is
    served at `/` so one process runs the whole desk. `default_live_settings` sets the
    polling intervals for live mode; without it live mode uses the replay's symbols and cash.
    """
    live_defaults = default_live_settings or LiveSettings(
        symbols=default_settings.symbols, starting_cash=default_settings.starting_cash
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await session.stop()

    app = FastAPI(title="AI Trading Desk", version="0.1.0", lifespan=lifespan)
    app.state.session = session

    @app.middleware("http")
    async def reject_writes_from_other_sites(request: Request, call_next):
        """Refuse any state-changing request whose Origin is not a page on this machine."""
        if request.method not in READ_ONLY_METHODS and not is_trusted_origin(
            request.headers.get("origin")
        ):
            logger.warning(
                "Refused %s %s from origin=%s",
                request.method,
                request.url.path,
                request.headers.get("origin"),
            )
            return JSONResponse(
                status_code=403, content={"detail": "Requests from other sites are refused"}
            )
        return await call_next(request)

    # Added last so it wraps the guard above and answers preflight requests itself.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGIN_EXPRESSION,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    def status_response() -> dict:
        return {key: value for key, value in session.snapshot().items() if key in STATUS_KEYS}

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "venue": "paper", "mode": session.mode}

    @app.get("/api/state")
    async def state() -> dict:
        return session.snapshot()

    @app.post("/api/replay/start", status_code=202)
    async def start_replay(request: StartReplayRequest) -> dict:
        """POST /api/replay/start: start a replay using the server's defaults overridden by any
        fields
        supplied. 422 for an invalid symbol or window, 409 if a replay is already running.
        """
        requested_fields = request.model_dump(exclude_none=True)
        if "symbols" in requested_fields:
            requested_fields["symbols"] = normalise_symbols(requested_fields["symbols"])
        try:
            settings = ReplaySettings.model_validate(
                {**default_settings.model_dump(), **requested_fields}
            )
        except ValueError as validation_error:
            raise HTTPException(
                status_code=422, detail="The replay window or settings are invalid"
            ) from validation_error
        try:
            await session.start(settings)
        except SessionBusyError as busy_error:
            raise HTTPException(
                status_code=409, detail="A replay is already running"
            ) from busy_error
        return status_response()

    @app.post("/api/replay/pause")
    async def pause_replay() -> dict:
        try:
            session.pause()
        except SessionNotRunningError as not_running_error:
            raise HTTPException(
                status_code=409, detail="Only a running replay can be paused"
            ) from not_running_error
        return status_response()

    @app.post("/api/replay/resume")
    async def resume_replay() -> dict:
        try:
            session.resume()
        except SessionNotRunningError as not_running_error:
            raise HTTPException(
                status_code=409, detail="Only a paused replay can be resumed"
            ) from not_running_error
        return status_response()

    @app.post("/api/replay/stop")
    async def stop_replay() -> dict:
        await session.stop()
        return status_response()

    @app.post("/api/replay/speed")
    async def set_replay_speed(request: SpeedRequest) -> dict:
        session.set_speed(request.bars_per_second)
        return status_response()

    @app.post("/api/kill-switch")
    async def set_kill_switch(request: KillSwitchRequest) -> dict:
        session.set_kill_switch(request.engaged)
        return status_response()

    @app.post("/api/mode")
    async def set_mode(request: ModeRequest) -> dict:
        """POST /api/mode: "live" starts live polling with a fresh paper account; "replay"
        stops live polling and returns the desk to replay. 409 while a replay is running, 501
        when the data source has no live prices, 422 for an invalid symbol.
        """
        if request.mode == "replay":
            if session.mode == "live":
                await session.stop()
            if session.is_active:
                raise HTTPException(status_code=409, detail="Stop the running replay first")
            session.use_replay_mode()
            return status_response()
        requested_fields = request.model_dump(exclude_none=True, exclude={"mode"})
        if "symbols" in requested_fields:
            requested_fields["symbols"] = normalise_symbols(requested_fields["symbols"])
        live_settings = LiveSettings.model_validate(
            {**live_defaults.model_dump(), **requested_fields}
        )
        try:
            await session.start_live(live_settings)
        except SessionBusyError as busy_error:
            raise HTTPException(
                status_code=409, detail="Stop the current run before going live"
            ) from busy_error
        except LiveNotAvailableError as not_available_error:
            raise HTTPException(
                status_code=501,
                detail="Live polling needs a live data source: set the Alpaca keys and start "
                "the server with --source alpaca. Orders go to the paper venue in every mode.",
            ) from not_available_error
        return status_response()

    @app.get("/api/backtest")
    async def backtest(
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """In-sample score for the configured symbols.

        The out-of-sample window is never evaluated from the terminal; that stays
        a deliberate command-line act.
        """
        window_start, window_end = start or default_settings.start, end or default_settings.end
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise HTTPException(status_code=422, detail="Times must include a timezone")
        try:
            backtest_report = await run_backtest(
                (session.settings or default_settings).symbols,
                session.data_provider,
                start=window_start,
                end=window_end,
                starting_cash=default_settings.starting_cash,
                market_profile=session.market_profile,
                limits=session.limits,
                setup_rules=session.setup_rules,
            )
        except ValueError as window_error:
            raise HTTPException(
                status_code=422, detail="The backtest window is invalid"
            ) from window_error
        return backtest_report.model_dump(mode="json")

    def current_trades_and_saved_decisions() -> tuple[list, dict[int, dict]]:
        """The current run's closed trades and, by position, the saved decision behind each."""
        closed_trades = list(session.venue.closed_trades) if session.venue else []
        saved_decisions: dict[int, dict] = {}
        if session.journal is not None and session.run_id is not None:
            for trade_number, closed_trade in enumerate(closed_trades):
                saved_decision = session.journal.decision_for_trade(session.run_id, closed_trade)
                if saved_decision is not None:
                    saved_decisions[trade_number] = saved_decision
        return closed_trades, saved_decisions

    @app.get("/api/review")
    async def review() -> dict:
        """GET /api/review: results of the current run's closed trades, broken down by strategy,
        symbol, side, exit reason and holding time, with each trade's excursions.
        """
        closed_trades, saved_decisions = current_trades_and_saved_decisions()
        return {
            "run_id": session.run_id,
            "review": review_trades(closed_trades, frozenset(saved_decisions)).model_dump(
                mode="json"
            ),
        }

    @app.get("/api/review/trades/{trade_number}/decision")
    async def trade_decision(trade_number: int) -> dict:
        """GET /api/review/trades/N/decision: the six-agent report saved when trade N (0-based)
        was approved. 404 when there is no such trade or no report was kept for it.
        """
        _, saved_decisions = current_trades_and_saved_decisions()
        if trade_number not in saved_decisions:
            raise HTTPException(status_code=404, detail="No saved decision for that trade")
        return saved_decisions[trade_number]

    @app.websocket("/ws/events")
    async def stream_events(websocket: WebSocket) -> None:
        """WebSocket /ws/events: refuse pages from other sites, then send one snapshot followed by
        every desk message until the client disconnects. A reader that fell behind is sent a fresh
        snapshot instead of a stale backlog.
        """
        if not is_trusted_origin(websocket.headers.get("origin")):
            logger.warning("Refused event stream from origin=%s", websocket.headers.get("origin"))
            await websocket.close(code=WEBSOCKET_POLICY_VIOLATION)
            return
        await websocket.accept()
        subscriber_queue = session.subscribe()
        logger.info("Event stream opened subscribers=%d", len(session._subscribers))
        try:
            await websocket.send_json(session.snapshot())
            client_closed = asyncio.create_task(wait_for_disconnect(websocket))
            try:
                while True:
                    next_message = asyncio.create_task(subscriber_queue.get())
                    await asyncio.wait(
                        {next_message, client_closed}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if client_closed.done():
                        next_message.cancel()
                        break
                    message = next_message.result()
                    if message.get("type") == "resync":
                        message = session.snapshot()
                    await websocket.send_json(message)
            finally:
                client_closed.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            session.unsubscribe(subscriber_queue)
            logger.info("Event stream closed subscribers=%d", len(session._subscribers))

    if ui_directory is not None:
        if not (ui_directory / "index.html").is_file():
            raise ValueError("the terminal UI directory has no index.html; build it first")
        # Mounted last: API and WebSocket routes above always win.
        app.mount("/", StaticFiles(directory=ui_directory, html=True), name="terminal")
    return app


STATUS_KEYS = {
    "status",
    "mode",
    "venue",
    "kill_switch_engaged",
    "bars_per_second",
    "bar_index",
    "bar_count",
    "bar_time",
    "error",
    "run_id",
}


async def wait_for_disconnect(websocket: WebSocket) -> None:
    """Returns when the client goes away. Anything the client sends is ignored."""
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
