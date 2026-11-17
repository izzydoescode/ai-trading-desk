"""The replay session: pacing, pause, speed, kill switch and what it publishes."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trading_desk.engine import run_engine
from trading_desk.markets import US_EQUITIES
from trading_desk.paper import PaperVenue
from trading_desk.server import (
    DeskSession,
    ReplaySettings,
    SessionBusyError,
    SessionNotRunningError,
    SessionStatus,
)
from trading_desk.server import session as session_module
from trading_desk.tools.market_data import FixtureProvider

FIRST_BAR = datetime(2025, 1, 2, 21, tzinfo=UTC)
LAST_BAR = datetime(2025, 4, 23, 21, tzinfo=UTC)
FIXTURE_BAR_COUNT = 80


def replay_settings(**overrides):
    return ReplaySettings(
        **{
            "symbols": ["AAPL"],
            "start": FIRST_BAR,
            "end": LAST_BAR,
            "bars_per_second": 1000,
            **overrides,
        }
    )


async def yield_once(seconds):
    await asyncio.sleep(0)


def session_that(acts_on_bar=None, **options):
    """A session whose pacing sleep returns at once; `acts_on_bar(session, bar_number)`
    is called before each sleep so a test can steer the run at an exact bar."""
    paced_bars = []

    async def sleeper(seconds):
        paced_bars.append(seconds)
        if acts_on_bar is not None:
            acts_on_bar(desk_session, len(paced_bars))
        await asyncio.sleep(0)

    desk_session = DeskSession(FixtureProvider(), sleep=sleeper, **options)
    return desk_session, paced_bars


async def wait_for_status(desk_session, expected_status, timeout_seconds=10.0):
    async def poll():
        while desk_session.status is not expected_status:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout_seconds)


def drain(subscriber_queue):
    messages = []
    while not subscriber_queue.empty():
        messages.append(subscriber_queue.get_nowait())
    return messages


async def test_replay_runs_to_the_end_and_matches_a_plain_engine_run():
    desk_session, paced_bars = session_that()
    await desk_session.start(replay_settings())
    await desk_session.wait_until_finished()
    plain_result = await run_engine(
        ["AAPL"],
        FixtureProvider(),
        PaperVenue(100_000.0, US_EQUITIES),
        start=FIRST_BAR,
        end=LAST_BAR,
    )
    assert desk_session.status is SessionStatus.FINISHED
    assert desk_session.bar_count == FIXTURE_BAR_COUNT == len(paced_bars)
    assert desk_session.bar_index == FIXTURE_BAR_COUNT - 1
    assert desk_session.venue.equity == plain_result.final_equity
    assert len(desk_session.equity_curve) == plain_result.bars_processed
    snapshot = desk_session.snapshot()
    assert snapshot["status"] == "FINISHED"
    assert snapshot["venue"] == "paper"
    assert len(snapshot["candles_by_symbol"]["AAPL"]) == FIXTURE_BAR_COUNT
    assert snapshot["account"]["starting_cash"] == 100_000.0
    assert snapshot["limits"]["max_loss_per_idea_fraction"] == 0.01
    json.dumps(snapshot)  # everything published must be plain JSON


async def test_subscriber_gets_a_snapshot_then_bars_events_and_a_final_status():
    desk_session, _ = session_that()
    subscriber_queue = desk_session.subscribe()
    await desk_session.start(replay_settings())
    await desk_session.wait_until_finished()
    messages = drain(subscriber_queue)
    assert messages[0]["type"] == "snapshot"
    assert messages[-1] == {"type": "status", **desk_session._status_fields()}
    assert messages[-1]["status"] == "FINISHED"
    bar_messages = [message for message in messages if message["type"] == "bar"]
    assert [message["bar_index"] for message in bar_messages] == list(range(FIXTURE_BAR_COUNT))
    assert any(message["type"] == "event" for message in messages)
    published_reports = [
        report for message in bar_messages for report in message["reports_by_symbol"].values()
    ]
    assert published_reports
    assert all("data" not in report and "trace" not in report for report in published_reports)
    assert all(report["decision"]["action"] for report in published_reports)
    desk_session.unsubscribe(subscriber_queue)
    assert subscriber_queue not in desk_session._subscribers


async def test_pause_holds_the_bar_and_resume_finishes_the_run():
    def pause_on_bar_ten(desk_session, bar_number):
        if bar_number == 10:
            desk_session.pause()

    desk_session, _ = session_that(pause_on_bar_ten)
    await desk_session.start(replay_settings())
    await wait_for_status(desk_session, SessionStatus.PAUSED)
    await asyncio.sleep(0.02)  # a paused run must not advance
    assert desk_session.bar_index == 9
    with pytest.raises(SessionBusyError):
        await desk_session.start(replay_settings())
    with pytest.raises(SessionNotRunningError):
        desk_session.pause()
    desk_session.resume()
    with pytest.raises(SessionNotRunningError):
        desk_session.resume()
    await desk_session.wait_until_finished()
    assert desk_session.status is SessionStatus.FINISHED
    assert desk_session.bar_index == FIXTURE_BAR_COUNT - 1


async def test_controls_that_need_a_run_refuse_when_idle():
    desk_session, _ = session_that()
    with pytest.raises(SessionNotRunningError):
        desk_session.pause()
    with pytest.raises(SessionNotRunningError):
        desk_session.resume()
    await desk_session.stop()
    await desk_session.wait_until_finished()
    assert desk_session.status is SessionStatus.IDLE
    assert desk_session.snapshot()["account"] is None


async def test_speed_sets_the_wait_between_bars_and_rejects_nonsense():
    def speed_up_on_bar_three(desk_session, bar_number):
        if bar_number == 3:
            desk_session.set_speed(8)

    desk_session, paced_bars = session_that(speed_up_on_bar_three)
    await desk_session.start(replay_settings(bars_per_second=4))
    await desk_session.wait_until_finished()
    assert paced_bars[:3] == [0.25, 0.25, 0.25]
    assert paced_bars[3] == 0.125
    for bad_speed in (0, -1, session_module.MAXIMUM_BARS_PER_SECOND + 1):
        with pytest.raises(ValueError, match="speed"):
            desk_session.set_speed(bad_speed)


async def test_kill_switch_engaged_before_the_run_means_no_ideas_at_all():
    desk_session, _ = session_that()
    desk_session.set_kill_switch(True)
    await desk_session.start(replay_settings())
    await desk_session.wait_until_finished()
    assert desk_session.snapshot()["kill_switch_engaged"] is True
    assert desk_session.reports_by_symbol == {}
    assert desk_session.venue.closed_trades == []
    assert desk_session.venue.equity == 100_000.0


async def test_kill_switch_mid_run_halts_new_ideas_and_release_allows_them_again():
    observed = {}

    def engage_then_release(desk_session, bar_number):
        if bar_number == 5:
            desk_session.set_kill_switch(True)
            desk_session.set_kill_switch(True)  # engaging twice is harmless
            observed["halted_at"] = desk_session.bar_time.isoformat()
        if bar_number == 60:
            observed["was_halted"] = desk_session.venue.is_halted
            observed["latest_report_at"] = desk_session.reports_by_symbol["AAPL"]["evaluated_as_of"]
            desk_session.set_kill_switch(False)

    desk_session, _ = session_that(engage_then_release)
    subscriber_queue = desk_session.subscribe()
    await desk_session.start(replay_settings())
    await desk_session.wait_until_finished()
    assert observed["was_halted"]
    # No decision was made between the halt and the release 55 bars later...
    assert datetime.fromisoformat(observed["latest_report_at"]) == datetime.fromisoformat(
        observed["halted_at"]
    )
    # ...and they resumed afterwards.
    assert not desk_session.venue.is_halted
    assert datetime.fromisoformat(
        desk_session.reports_by_symbol["AAPL"]["evaluated_as_of"]
    ) > datetime.fromisoformat(observed["halted_at"])
    halt_events = [
        message["event"]
        for message in drain(subscriber_queue)
        if message["type"] == "event" and message["event"]["kind"] == "HALT"
    ]
    assert len(halt_events) == 1
    assert halt_events[0]["agent"] == "VENUE"
    assert halt_events[0]["sequence"] is None


async def test_stop_cancels_a_paused_run():
    def pause_on_bar_five(desk_session, bar_number):
        if bar_number == 5:
            desk_session.pause()

    desk_session, _ = session_that(pause_on_bar_five)
    await desk_session.start(replay_settings())
    await wait_for_status(desk_session, SessionStatus.PAUSED)
    await desk_session.stop()
    assert desk_session.status is SessionStatus.IDLE
    assert desk_session.bar_index == 4
    await desk_session.start(replay_settings())  # a stopped session can start again
    await desk_session.wait_until_finished()
    assert desk_session.status is SessionStatus.FINISHED


async def test_a_failed_run_reports_only_the_error_class(monkeypatch):
    async def failing_engine(*arguments, **options):
        raise RuntimeError("https://vendor.example/bars?apikey=SECRET-VALUE")

    monkeypatch.setattr(session_module, "run_engine", failing_engine)
    desk_session, _ = session_that()
    await desk_session.start(replay_settings())
    await desk_session.wait_until_finished()
    assert desk_session.status is SessionStatus.FAILED
    assert desk_session.error == "RuntimeError"
    assert "SECRET-VALUE" not in json.dumps(desk_session.snapshot())


async def test_a_reader_that_falls_behind_is_told_to_resync(monkeypatch):
    monkeypatch.setattr(session_module, "SUBSCRIBER_QUEUE_LIMIT", 5)
    desk_session, _ = session_that()
    subscriber_queue = desk_session.subscribe()
    await desk_session.start(replay_settings())
    await desk_session.wait_until_finished()
    messages = drain(subscriber_queue)
    assert len(messages) <= 5
    assert messages[0] == {"type": "resync"}


def test_replay_settings_reject_a_backwards_window_and_unknown_fields():
    with pytest.raises(ValidationError, match="ends before it starts"):
        replay_settings(start=LAST_BAR, end=FIRST_BAR)
    with pytest.raises(ValidationError):
        replay_settings(symbols=[])
    with pytest.raises(ValidationError):
        replay_settings(leverage=3)
