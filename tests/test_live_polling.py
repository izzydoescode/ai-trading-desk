"""Live polling: the session watches a data source, acts once on each newly completed
session and shows the latest prices in between. A scripted provider stands in for Alpaca;
nothing touches the network and no test waits on a real clock.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trading_desk.engine import run_engine
from trading_desk.markets import US_EQUITIES
from trading_desk.models import MarketData
from trading_desk.paper import PaperVenue
from trading_desk.paper.journal import Journal
from trading_desk.server import (
    DeskSession,
    LiveNotAvailableError,
    LiveSettings,
    ReplaySettings,
    SessionBusyError,
    SessionStatus,
    create_app,
)
from trading_desk.tools.market_data import FixtureProvider

# The bundled uptrend fixture approves a long on its 51st bar and fills it on the 52nd.
BARS_UP_TO_THE_APPROVED_IDEA = 51


class ScriptedLiveProvider:
    """Test double (not a test): serves the fixture's first `visible_bar_count` candles as if
    they were all the sessions completed so far, plus scripted latest prices.
    """

    name = "scripted-live"

    def __init__(self, all_candles, visible_bar_count):
        self.all_candles, self.visible_bar_count = all_candles, visible_bar_count
        self.latest_price, self.price_error, self.fetch_error = 117.25, None, None
        self.fetch_count = 0

    @property
    def visible_candles(self):
        return self.all_candles[: self.visible_bar_count]

    async def fetch(self, symbol):
        self.fetch_count += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return MarketData(symbol=symbol, source=self.name, candles=self.visible_candles)

    async def latest_prices(self, symbols):
        if self.price_error is not None:
            raise self.price_error
        return {symbol: self.latest_price for symbol in symbols}


class LiveHarness:
    """Test helper (not a test): a live session whose polling wait is released one round at a
    time by the test, with a clock one hour after the latest visible session's close.
    """

    def __init__(self, provider, **session_options):
        self.provider = provider
        self.rounds_started, self._release = 0, asyncio.Semaphore(0)
        self.session = DeskSession(
            provider, sleep=self._wait_for_release, clock=self.now, **session_options
        )

    def now(self):
        return self.provider.visible_candles[-1].timestamp + timedelta(hours=1)

    async def _wait_for_release(self, seconds):
        self.rounds_started += 1
        await self._release.acquire()

    async def wait_for_round(self, round_number):
        """Wait until the session has finished its work for that round and is waiting again."""

        async def poll():
            while self.rounds_started < round_number:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(poll(), 10)

    async def next_round(self):
        """Let the session's wait end, then wait for it to finish the following round."""
        target_round = self.rounds_started + 1
        self._release.release()
        await self.wait_for_round(target_round)


async def make_live_harness(visible_bar_count=BARS_UP_TO_THE_APPROVED_IDEA, **session_options):
    """Test helper (not a test): builds a harness over the bundled AAPL fixture."""
    all_candles = (await FixtureProvider().fetch("AAPL")).candles
    return LiveHarness(ScriptedLiveProvider(all_candles, visible_bar_count), **session_options)


def make_live_settings(**overrides):
    """Test helper (not a test): live settings that check for a new session every round."""
    return LiveSettings(
        **{"symbols": ["AAPL"], "price_poll_seconds": 1, "bar_check_seconds": 1, **overrides}
    )


def drain(subscriber_queue):
    """Test helper (not a test): empties a subscriber queue and returns its messages."""
    messages = []
    while not subscriber_queue.empty():
        messages.append(subscriber_queue.get_nowait())
    return messages


async def test_going_live_acts_on_the_latest_completed_session_only():
    """Verifies: the first poll evaluates only the newest finished session (queueing the idea it
    approves), loads the earlier history for the chart without trading through it, and
    publishes live prices.
    """
    harness = await make_live_harness()
    subscriber_queue = harness.session.subscribe()
    await harness.session.start_live(make_live_settings())
    await harness.wait_for_round(1)
    session, latest_candle = harness.session, harness.provider.visible_candles[-1]

    assert (session.status, session.mode) == (SessionStatus.RUNNING, "live")
    assert session.bar_time == latest_candle.timestamp
    assert (session.bar_index, session.bar_count) == (0, None)
    assert len(session.equity_curve) == 1
    venue_event_kinds = [
        event["kind"] for event in session.recent_events if event["agent"] == "VENUE"
    ]
    assert venue_event_kinds == ["SUBMIT"]
    assert session.venue.closed_trades == [] and not session.venue.positions
    assert sorted(session.venue.pending_orders) == ["AAPL"]

    snapshot = session.snapshot()
    chart_candles = snapshot["candles_by_symbol"]["AAPL"]
    assert len(chart_candles) == BARS_UP_TO_THE_APPROVED_IDEA
    assert len({candle["timestamp"] for candle in chart_candles}) == len(chart_candles)
    assert snapshot["mode"] == "live" and snapshot["venue"] == "paper"
    assert snapshot["live_available"] is True
    assert snapshot["latest_prices"] == {"AAPL": 117.25}
    assert snapshot["prices_as_of"] == harness.now().isoformat()
    json.dumps(snapshot)

    message_types = [message["type"] for message in drain(subscriber_queue)]
    assert message_types[:2] == ["snapshot", "snapshot"]  # started, then chart history loaded
    assert "bar" in message_types and message_types[-1] == "prices"
    await session.stop()
    assert session.status is SessionStatus.IDLE


async def test_a_newly_completed_session_fills_the_queued_idea_at_its_open():
    """Verifies: when the next session completes, the same paper venue fills the queued order
    at that session's open, and the run stays one journal: one run id, unbroken sequence
    numbers, and the same result as a plain engine run over the same two bars.
    """
    journal = Journal()
    harness = await make_live_harness(journal=journal)
    await harness.session.start_live(make_live_settings())
    await harness.wait_for_round(1)
    await harness.next_round()  # no new session yet: nothing changes
    assert len(harness.session.equity_curve) == 1

    harness.provider.visible_bar_count += 1
    await harness.next_round()
    session, new_candle = harness.session, harness.provider.visible_candles[-1]
    assert session.bar_time == new_candle.timestamp
    assert (session.bar_index, len(session.equity_curve)) == (1, 2)
    position = session.venue.positions["AAPL"]
    assert position.entry_price > new_candle.open  # the open plus slippage against the desk
    assert position.entry_price == pytest.approx(new_candle.open, rel=0.01)
    assert len(session.candles_by_symbol["AAPL"]) == BARS_UP_TO_THE_APPROVED_IDEA + 1

    recorded_events = journal.events(session.run_id)
    assert [event.sequence for event in recorded_events] == list(range(len(recorded_events)))
    assert {event.run_id for event in recorded_events} == {session.run_id}
    assert [event.kind for event in recorded_events if event.agent == "VENUE"] == [
        "SUBMIT",
        "FILL",
    ]

    plain_venue = PaperVenue(100_000.0, US_EQUITIES)
    await run_engine(
        ["AAPL"],
        harness.provider,
        plain_venue,
        start=harness.provider.visible_candles[-2].timestamp,
        end=new_candle.timestamp,
        liquidate_at_end=False,
    )
    assert session.venue.equity == plain_venue.equity
    await session.stop()


async def test_a_failed_poll_is_reported_by_class_name_and_retried():
    """Verifies: a data failure during live polling does not end the run or leak the error
    text; the status shows the class name, and the next good poll clears it. A failed price
    poll keeps the previous prices.
    """
    harness = await make_live_harness()
    await harness.session.start_live(make_live_settings())
    await harness.wait_for_round(1)

    async def failing_preload(*arguments):
        raise ConnectionError("https://user:secret-password@example.com failed")

    harness.provider.price_error = TimeoutError("apikey=secret-key")
    harness.provider.latest_price = 999.0
    original_process = harness.session._process_new_sessions
    harness.session._process_new_sessions = failing_preload
    await harness.next_round()
    session = harness.session
    assert (session.status, session.error) == (SessionStatus.RUNNING, "ConnectionError")
    assert "secret" not in json.dumps(session.snapshot())
    assert session.latest_prices == {"AAPL": 117.25}

    harness.session._process_new_sessions = original_process
    harness.provider.price_error = None
    await harness.next_round()
    assert (session.status, session.error) == (SessionStatus.RUNNING, None)
    assert session.latest_prices == {"AAPL": 999.0}
    await session.stop()


async def test_a_symbol_whose_data_fails_is_journalled_and_the_run_continues():
    """Verifies: when the provider raises for a symbol, the live run records a data error by
    class name and keeps polling instead of failing.
    """
    harness = await make_live_harness()
    harness.provider.fetch_error = RuntimeError("apikey=secret-key")
    await harness.session.start_live(make_live_settings())
    await harness.wait_for_round(1)
    session = harness.session
    assert session.status is SessionStatus.RUNNING
    assert session.equity_curve == []
    (data_event,) = session.recent_events
    assert (data_event["agent"], data_event["kind"]) == ("DATA", "DATA_ERROR")
    assert "RuntimeError" in data_event["reason"] and "secret" not in data_event["reason"]
    await session.stop()


async def test_the_kill_switch_and_pause_apply_to_a_live_run():
    """Verifies: with the kill switch engaged before going live the approved idea is never
    queued, and a paused live run does no work until resumed.
    """
    harness = await make_live_harness()
    harness.session.set_kill_switch(True)
    await harness.session.start_live(make_live_settings())
    await harness.wait_for_round(1)
    session = harness.session
    assert session.venue.is_halted and not session.venue.pending_orders
    assert [event["kind"] for event in session.recent_events if event["agent"] == "VENUE"] == [
        "HALT"
    ]

    session.pause()
    fetches_before_pause = harness.provider.fetch_count
    harness._release.release()
    await asyncio.sleep(0.02)
    assert harness.provider.fetch_count == fetches_before_pause
    assert session.status is SessionStatus.PAUSED
    session.resume()
    await harness.wait_for_round(2)
    assert harness.provider.fetch_count > fetches_before_pause
    await session.stop()


async def test_live_needs_a_live_data_source_and_an_idle_session():
    """Verifies: a provider without live prices cannot go live, a second run cannot start
    over a live one, and live settings reject nonsense intervals.
    """
    fixture_session = DeskSession(FixtureProvider())
    assert fixture_session.supports_live is False
    with pytest.raises(LiveNotAvailableError):
        await fixture_session.start_live(make_live_settings())
    assert fixture_session.status is SessionStatus.IDLE

    harness = await make_live_harness()
    await harness.session.start_live(make_live_settings())
    await harness.wait_for_round(1)
    with pytest.raises(SessionBusyError):
        await harness.session.start_live(make_live_settings())
    with pytest.raises(SessionBusyError):
        harness.session.use_replay_mode()
    await harness.session.stop()
    harness.session.use_replay_mode()
    assert harness.session.mode == "replay"

    with pytest.raises(ValidationError):
        make_live_settings(price_poll_seconds=0)
    with pytest.raises(ValidationError):
        make_live_settings(symbols=[])


async def short_real_wait(seconds):
    """Test helper (not a test): a polling wait short enough for an HTTP test."""
    await asyncio.sleep(0.005)


def make_live_client(provider):
    """Test helper (not a test): an API client over a session with the given provider."""
    candles = provider.visible_candles if hasattr(provider, "visible_candles") else None
    desk_session = DeskSession(
        provider,
        sleep=short_real_wait,
        **({"clock": lambda: candles[-1].timestamp + timedelta(hours=1)} if candles else {}),
    )
    replay_defaults = ReplaySettings(
        symbols=["AAPL"],
        start="2025-01-02T21:00:00Z",
        end="2025-04-23T21:00:00Z",
        bars_per_second=1000,
    )
    return TestClient(create_app(desk_session, replay_defaults))


def test_the_mode_endpoint_starts_and_stops_live_polling():
    """Verifies: POST /api/mode live starts polling on the paper venue, a replay cannot start
    over it, an invalid symbol is refused, and switching back to replay stops it.
    """
    all_candles = asyncio.run(FixtureProvider().fetch("AAPL")).candles
    provider = ScriptedLiveProvider(all_candles, BARS_UP_TO_THE_APPROVED_IDEA)
    with make_live_client(provider) as client:
        assert client.get("/api/state").json()["live_available"] is True
        bad_symbol = client.post("/api/mode", json={"mode": "live", "symbols": ["not a symbol"]})
        assert bad_symbol.status_code == 422
        live = client.post("/api/mode", json={"mode": "live", "starting_cash": 50_000})
        assert (live.status_code, live.json()["mode"], live.json()["venue"]) == (
            200,
            "live",
            "paper",
        )
        assert client.get("/api/health").json()["mode"] == "live"
        assert client.post("/api/mode", json={"mode": "live"}).status_code == 409
        assert client.post("/api/replay/start", json={}).status_code == 409
        state = client.get("/api/state").json()
        assert state["status"] == "RUNNING"
        assert state["account"]["starting_cash"] == 50_000
        replay = client.post("/api/mode", json={"mode": "replay"})
        assert (replay.status_code, replay.json()["mode"], replay.json()["status"]) == (
            200,
            "replay",
            "IDLE",
        )


def test_live_mode_is_refused_without_a_live_data_source():
    """Verifies: on the bundled fixture, going live answers 501 and says orders are paper only;
    switching to replay is refused while a replay is running.
    """
    with make_live_client(FixtureProvider()) as client:
        assert client.get("/api/state").json()["live_available"] is False
        live = client.post("/api/mode", json={"mode": "live"})
        assert live.status_code == 501
        assert "paper venue" in live.json()["detail"]
        assert client.get("/api/state").json()["status"] == "IDLE"
        assert client.post("/api/replay/start", json={"bars_per_second": 20}).status_code == 202
        assert client.post("/api/mode", json={"mode": "replay"}).status_code == 409
        client.post("/api/replay/stop")
        assert client.post("/api/mode", json={"mode": "replay"}).json()["mode"] == "replay"
