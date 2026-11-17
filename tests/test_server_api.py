"""The terminal's HTTP and WebSocket API, driven in-process. Nothing touches the network."""

import asyncio
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from trading_desk.server import DeskSession, ReplaySettings, create_app, is_trusted_origin
from trading_desk.tools.market_data import FixtureProvider

FIRST_BAR = datetime(2025, 1, 2, 21, tzinfo=UTC)
LAST_BAR = datetime(2025, 4, 23, 21, tzinfo=UTC)
FIXTURE_BAR_COUNT = 80
DEFAULT_SETTINGS = ReplaySettings(
    symbols=["AAPL"], start=FIRST_BAR, end=LAST_BAR, bars_per_second=1000
)
LOCAL_UI_ORIGIN = "http://localhost:5173"
OTHER_SITE_ORIGIN = "https://evil.example"


async def yield_once(seconds):
    await asyncio.sleep(0)


async def short_real_wait(seconds):
    await asyncio.sleep(0.01)


@pytest.fixture
def client():
    desk_session = DeskSession(FixtureProvider(), sleep=yield_once)
    with TestClient(create_app(desk_session, DEFAULT_SETTINGS)) as test_client:
        yield test_client


@pytest.fixture
def slow_client():
    """A replay slow enough (about a second) to be paused and steered over HTTP."""
    desk_session = DeskSession(FixtureProvider(), sleep=short_real_wait)
    with TestClient(create_app(desk_session, DEFAULT_SETTINGS)) as test_client:
        yield test_client


def wait_for_status(test_client, expected_status, timeout_seconds=15.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = test_client.get("/api/state").json()
        if state["status"] == expected_status:
            return state
        time.sleep(0.01)
    raise AssertionError(f"status never became {expected_status}: {state['status']}")


def test_health_and_idle_state(client):
    assert client.get("/api/health").json() == {"status": "ok", "venue": "paper", "mode": "replay"}
    state = client.get("/api/state").json()
    assert state["type"] == "snapshot"
    assert state["status"] == "IDLE"
    assert state["account"] is None
    assert state["data_source"] == "bundled-synthetic-fixture"
    assert state["limits"]["minimum_reward_to_risk"] == 2.75
    assert state["setup_rules"]["target_reward_multiple"] == 3.0


def test_replay_runs_from_start_request_to_finished_state(client):
    response = client.post("/api/replay/start", json={})
    assert response.status_code == 202
    assert response.json()["status"] == "RUNNING"
    state = wait_for_status(client, "FINISHED")
    assert state["bar_count"] == FIXTURE_BAR_COUNT
    assert len(state["equity_curve"]) == FIXTURE_BAR_COUNT
    assert len(state["candles_by_symbol"]["AAPL"]) == FIXTURE_BAR_COUNT
    assert state["reports_by_symbol"]["AAPL"]["decision"]["action"] in {"TRADE", "WAIT", "REJECT"}
    assert state["recent_events"]
    assert state["account"]["starting_cash"] == 100_000.0
    # A finished session can be started again, with overrides.
    restart = client.post(
        "/api/replay/start",
        json={"symbols": [" aapl "], "starting_cash": 5000, "bars_per_second": 500},
    )
    assert restart.status_code == 202
    assert wait_for_status(client, "FINISHED")["account"]["starting_cash"] == 5000


def test_pause_resume_speed_and_stop_over_http(slow_client):
    assert slow_client.post("/api/replay/start", json={}).status_code == 202
    paused = slow_client.post("/api/replay/pause")
    assert (paused.status_code, paused.json()["status"]) == (200, "PAUSED")
    assert slow_client.post("/api/replay/pause").status_code == 409
    assert slow_client.post("/api/replay/start", json={}).status_code == 409
    held_bar = slow_client.get("/api/state").json()["bar_index"]
    time.sleep(0.05)
    assert slow_client.get("/api/state").json()["bar_index"] == held_bar
    speed = slow_client.post("/api/replay/speed", json={"bars_per_second": 32})
    assert speed.json()["bars_per_second"] == 32
    resumed = slow_client.post("/api/replay/resume")
    assert (resumed.status_code, resumed.json()["status"]) == (200, "RUNNING")
    assert slow_client.post("/api/replay/resume").status_code == 409
    stopped = slow_client.post("/api/replay/stop")
    assert (stopped.status_code, stopped.json()["status"]) == (200, "IDLE")


def test_pause_and_resume_without_a_run_are_conflicts(client):
    assert client.post("/api/replay/pause").status_code == 409
    assert client.post("/api/replay/resume").status_code == 409
    assert client.post("/api/replay/stop").json()["status"] == "IDLE"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/replay/start", {"symbols": ["AAPL; DROP TABLE"]}),
        ("/api/replay/start", {"symbols": []}),
        ("/api/replay/start", {"start": "2025-04-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"}),
        ("/api/replay/start", {"start": "2025-04-01T00:00:00"}),
        ("/api/replay/start", {"leverage": 5}),
        ("/api/replay/start", {"bars_per_second": 0}),
        ("/api/replay/speed", {"bars_per_second": -2}),
        ("/api/replay/speed", {}),
        ("/api/kill-switch", {"engaged": "maybe"}),
        ("/api/mode", {"mode": "margin"}),
    ],
)
def test_bad_requests_are_rejected_and_nothing_starts(client, path, body):
    assert client.post(path, json=body).status_code == 422
    assert client.get("/api/state").json()["status"] == "IDLE"


def test_kill_switch_over_http_blocks_every_idea(client):
    engaged = client.post("/api/kill-switch", json={"engaged": True})
    assert engaged.json()["kill_switch_engaged"] is True
    client.post("/api/replay/start", json={})
    state = wait_for_status(client, "FINISHED")
    assert state["reports_by_symbol"] == {}
    assert state["account"]["closed_trades"] == []
    released = client.post("/api/kill-switch", json={"engaged": False})
    assert released.json()["kill_switch_engaged"] is False


def test_live_mode_is_refused_and_replay_mode_is_a_no_op(client):
    live = client.post("/api/mode", json={"mode": "live"})
    assert live.status_code == 501
    assert "paper venue" in live.json()["detail"]
    replay = client.post("/api/mode", json={"mode": "replay"})
    assert (replay.status_code, replay.json()["mode"]) == (200, "replay")


def test_writes_from_another_site_are_refused(client):
    refused = client.post("/api/replay/start", json={}, headers={"origin": OTHER_SITE_ORIGIN})
    assert refused.status_code == 403
    assert client.get("/api/state").json()["status"] == "IDLE"
    kill_switch = client.post(
        "/api/kill-switch", json={"engaged": True}, headers={"origin": OTHER_SITE_ORIGIN}
    )
    assert kill_switch.status_code == 403
    assert client.get("/api/state").json()["kill_switch_engaged"] is False


def test_local_ui_origin_is_allowed_and_other_sites_get_no_cors_grant(client):
    allowed = client.post(
        "/api/kill-switch", json={"engaged": False}, headers={"origin": LOCAL_UI_ORIGIN}
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == LOCAL_UI_ORIGIN
    preflight = client.options(
        "/api/replay/start",
        headers={
            "origin": LOCAL_UI_ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    other_site_read = client.get("/api/state", headers={"origin": OTHER_SITE_ORIGIN})
    assert "access-control-allow-origin" not in other_site_read.headers


@pytest.mark.parametrize(
    "origin,trusted",
    [
        (None, True),
        ("http://localhost:5173", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("https://localhost", True),
        ("https://evil.example", False),
        ("http://localhost.evil.example", False),
        ("http://evil.example/?http://localhost:5173", False),
        ("null", False),
        ("", False),
    ],
)
def test_only_pages_on_this_machine_are_trusted(origin, trusted):
    assert is_trusted_origin(origin) is trusted


def test_websocket_streams_a_snapshot_then_the_whole_replay(client):
    with client.websocket_connect("/ws/events", headers={"origin": LOCAL_UI_ORIGIN}) as socket:
        first_message = socket.receive_json()
        assert (first_message["type"], first_message["status"]) == ("snapshot", "IDLE")
        client.post("/api/replay/start", json={})
        message_types, bar_indexes = [], []
        while True:
            message = socket.receive_json()
            message_types.append(message["type"])
            if message["type"] == "bar":
                bar_indexes.append(message["bar_index"])
            if message["type"] == "status" and message["status"] == "FINISHED":
                break
    assert message_types[0] == "snapshot"  # the run's opening state
    assert bar_indexes == list(range(FIXTURE_BAR_COUNT))
    assert "event" in message_types
    assert "account" in message_types
    assert client.app.state.session._subscribers == set()


def test_websocket_from_another_site_is_closed_before_any_data(client):
    with pytest.raises(WebSocketDisconnect) as disconnect:
        with client.websocket_connect("/ws/events", headers={"origin": OTHER_SITE_ORIGIN}):
            pass
    assert disconnect.value.code == 1008
    assert client.app.state.session._subscribers == set()


def test_backtest_endpoint_scores_in_sample_only(client):
    response = client.get("/api/backtest")
    assert response.status_code == 200
    backtest_report = response.json()
    assert backtest_report["symbols"] == ["AAPL"]
    assert backtest_report["in_sample"]["bars"] == FIXTURE_BAR_COUNT
    assert backtest_report["out_of_sample"] is None
    narrower = client.get(
        "/api/backtest",
        params={"start": "2025-03-03T00:00:00Z", "end": "2025-04-23T23:00:00Z"},
    ).json()
    assert 0 < narrower["in_sample"]["bars"] < FIXTURE_BAR_COUNT


@pytest.mark.parametrize(
    "query",
    [
        {"start": "2025-03-03T00:00:00"},
        {"start": "2025-04-23T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
        {"start": "yesterday"},
    ],
)
def test_backtest_endpoint_rejects_bad_windows(client, query):
    assert client.get("/api/backtest", params=query).status_code == 422


def test_built_terminal_is_served_at_the_root_without_hiding_the_api(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>AI Trading Desk</title>")
    (tmp_path / "assets" / "app.js").write_text("console.log('desk')")
    desk_session = DeskSession(FixtureProvider(), sleep=yield_once)
    with TestClient(create_app(desk_session, DEFAULT_SETTINGS, tmp_path)) as ui_client:
        home = ui_client.get("/")
        assert home.status_code == 200
        assert "AI Trading Desk" in home.text
        assert ui_client.get("/assets/app.js").text == "console.log('desk')"
        assert ui_client.get("/api/health").json()["status"] == "ok"
        assert ui_client.post("/api/replay/start", json={}).status_code == 202
        assert ui_client.get("/missing.js").status_code == 404
        with ui_client.websocket_connect("/ws/events") as socket:
            assert socket.receive_json()["type"] == "snapshot"


def test_a_ui_directory_without_a_build_is_refused(tmp_path):
    desk_session = DeskSession(FixtureProvider(), sleep=yield_once)
    with pytest.raises(ValueError, match="index.html"):
        create_app(desk_session, DEFAULT_SETTINGS, tmp_path)
