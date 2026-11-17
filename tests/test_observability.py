"""Logging: configuration, secret redaction, and that failures leave a usable trail."""

import asyncio
import io
import logging
from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.desk import run_desk_decision
from trading_desk.models import AccountSnapshot
from trading_desk.observability import (
    LOGGER_NAME,
    REDACTED,
    RedactingFormatter,
    configure_logging,
    get_logger,
    redact,
)
from trading_desk.server import DeskSession, ReplaySettings, SessionStatus
from trading_desk.server import session as session_module
from trading_desk.tools.market_data import FixtureProvider

AS_OF = datetime(2025, 4, 23, 22, tzinfo=UTC)
SECRET = "SUPER-SECRET-VALUE-123"  # noqa: S105 - a fake value the tests try to leak


@pytest.mark.parametrize(
    "text",
    [
        f"GET https://vendor.example/bars?symbol=AAPL&apikey={SECRET}",
        f"GET https://vendor.example/bars?token={SECRET}&symbol=AAPL",
        f'{{"api_key": "{SECRET}"}}',
        f"password={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"https://user:{SECRET}@vendor.example/feed",
        "key sk-ant-abcdefghijklmnop rejected",
    ],
)
def test_secrets_recognised_by_their_shape_are_removed(text):
    """Verifies: credentials in URLs, JSON, headers and provider key formats never survive
    redaction, wherever they came from.
    """
    redacted = redact(text)
    assert SECRET not in redacted
    assert "sk-ant-abcdefghijklmnop" not in redacted
    assert REDACTED in redacted


def test_the_value_of_any_credential_in_the_environment_is_removed_wherever_it_appears(
    monkeypatch,
):
    """Verifies: a key loaded from .env is removed from a log line even when nothing around
    it looks like a credential.
    """
    monkeypatch.setenv("FINNHUB_API_KEY", SECRET)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PK-PAPER-KEY-ID-9")
    monkeypatch.setenv("SHORT_TOKEN", "abc")  # too short to be a real secret; left alone
    redacted = redact(f"request failed for {SECRET} and PK-PAPER-KEY-ID-9 near abc")
    assert redacted == f"request failed for {REDACTED} and {REDACTED} near abc"


def test_ordinary_text_passes_through_unchanged():
    """Verifies: redaction does not damage normal log lines, prices or symbols."""
    line = "run_id=1f2e symbol=AAPL kind=FILL reason=filled_at_next_open price=352.78 units=58"
    assert redact(line) == line


def test_stack_traces_are_logged_in_full_but_without_the_secret():
    """Verifies: a logged exception keeps its type and traceback for debugging while the
    credential inside its message is removed.
    """
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        raise RuntimeError(f"GET https://vendor.example/bars?apikey={SECRET} returned 500")
    except RuntimeError:
        get_logger("tools.news").warning("Feed failed symbol=%s", "AAPL", exc_info=True)
    output = stream.getvalue()
    assert "WARNING" in output and "trading_desk.tools.news | Feed failed symbol=AAPL" in output
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError: GET https://vendor.example/bars?apikey=[REDACTED] returned 500" in output
    assert SECRET not in output


def test_log_level_filters_what_is_written():
    """Verifies: the configured level decides which lines appear."""
    stream = io.StringIO()
    configure_logging("WARNING", stream=stream)
    logger = get_logger("engine")
    logger.debug("debug line")
    logger.info("info line")
    logger.warning("warning line")
    assert stream.getvalue().count("\n") == 1
    assert "warning line" in stream.getvalue()


def test_configuring_twice_replaces_handlers_instead_of_doubling_every_line():
    """Verifies: calling configure_logging again does not stack handlers."""
    first_stream, second_stream = io.StringIO(), io.StringIO()
    configure_logging("INFO", stream=first_stream)
    configure_logging("INFO", stream=second_stream)
    get_logger("cli").info("once")
    assert len(logging.getLogger(LOGGER_NAME).handlers) == 1
    assert first_stream.getvalue() == ""
    assert second_stream.getvalue().count("once") == 1


def test_log_file_is_created_with_its_folder_and_receives_redacted_lines(tmp_path):
    """Verifies: the optional log file is written, its folder is created, and the file
    gets the same redaction as the console.
    """
    log_file = tmp_path / "nested" / "desk.log"
    configure_logging("INFO", log_file, stream=io.StringIO())
    get_logger("server.session").info("token=%s", SECRET)
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        handler.flush()
    written = log_file.read_text()
    assert "trading_desk.server.session | token=[REDACTED]" in written
    assert SECRET not in written


def test_an_unknown_log_level_is_rejected():
    """Verifies: a mistyped level is an error, not a silent default."""
    with pytest.raises(ValueError, match="log level must be one of"):
        configure_logging("LOUD")


def test_the_formatter_redacts_the_message_as_well_as_the_traceback():
    """Verifies: redaction covers the formatted message arguments, not only exceptions."""
    record = logging.LogRecord(
        "trading_desk.llm", logging.INFO, __file__, 1, "calling with apikey=%s", (SECRET,), None
    )
    assert RedactingFormatter("%(message)s").format(record) == "calling with apikey=[REDACTED]"


class FailingNewsFeed:
    """Test double (not a test): a news feed whose request fails with a secret in the error."""

    name = "failing-news-feed"

    async def fetch(self, symbol, start, end, limit=12):
        raise RuntimeError(f"GET https://vendor.example/news?token={SECRET} returned 503")


async def test_a_failed_news_feed_leaves_a_traceback_in_the_log_and_no_secret_anywhere():
    """Verifies: when a feed fails, the report shows only the exception class, while the log
    explains what failed, for which symbol and feed, with the stack trace and no secret.
    """
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    report = await run_desk_decision(
        "AAPL",
        FixtureProvider(),
        AccountSnapshot(equity=100_000.0, cash=100_000.0),
        AS_OF,
        news_feed=FailingNewsFeed(),
    )
    output = stream.getvalue()
    assert report.news.abstain
    assert SECRET not in report.model_dump_json()
    assert "News feed failed symbol=AAPL feed=failing-news-feed; SNOWDEN abstains" in output
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError: GET https://vendor.example/news?token=[REDACTED] returned 503" in output
    assert SECRET not in output


async def test_a_replay_logs_its_start_its_trades_and_its_finish():
    """Verifies: a normal replay can be followed from the log alone: start with its settings,
    each order, fill and exit with its reason, and the final equity.
    """
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    async def no_wait(seconds):
        await asyncio.sleep(0)

    desk_session = DeskSession(FixtureProvider(), sleep=no_wait)
    await desk_session.start(
        ReplaySettings(
            symbols=["AAPL"],
            start=AS_OF - timedelta(days=120),
            end=AS_OF,
            bars_per_second=1000,
        )
    )
    await desk_session.wait_until_finished()
    output = stream.getvalue()
    assert "Replay started symbols=AAPL" in output
    for venue_event in ("kind=SUBMIT", "kind=FILL", "kind=EXIT"):
        assert venue_event in output
    assert "kind=TRADE reason=approved" in output
    assert "Engine run complete" in output
    assert f"Replay finished run_id={desk_session.run_id}" in output
    assert "final_equity=101676.04 closed_trades=1" in output
    # Routine skips and rejections are debug detail, not shown at INFO.
    assert "kind=REJECT" not in output


async def test_a_failed_replay_is_logged_as_an_error_with_where_it_stopped(monkeypatch):
    """Verifies: a crashed replay writes an ERROR with the traceback and the bar it reached,
    while the published state still carries only the exception class.
    """

    async def failing_engine(*arguments, **options):
        raise RuntimeError(f"upstream said apikey={SECRET} is invalid")

    monkeypatch.setattr(session_module, "run_engine", failing_engine)
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    async def no_wait(seconds):
        await asyncio.sleep(0)

    desk_session = DeskSession(FixtureProvider(), sleep=no_wait)
    await desk_session.start(
        ReplaySettings(symbols=["AAPL"], start=AS_OF - timedelta(days=30), end=AS_OF)
    )
    await desk_session.wait_until_finished()
    output = stream.getvalue()
    assert desk_session.status is SessionStatus.FAILED
    assert desk_session.error == "RuntimeError"
    assert "ERROR" in output and "Replay failed run_id=None bar_index=None" in output
    assert "RuntimeError: upstream said apikey=[REDACTED] is invalid" in output
    assert SECRET not in output


def test_kill_switch_and_model_spending_are_logged(monkeypatch):
    """Verifies: engaging the kill switch and every metered model call leave a log line, and
    a refused call at the spending cap is a warning.
    """
    from trading_desk.llm import CostCapExceededError, UsageMeter

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    desk_session = DeskSession(FixtureProvider())
    desk_session.set_kill_switch(True)
    usage_meter = UsageMeter(maximum_cost_usd=0.0001)
    usage_meter.record("claude-haiku-5-5", 2700, 790)
    with pytest.raises(CostCapExceededError):
        usage_meter.check_budget()
    output = stream.getvalue()
    assert "WARNING" in output and "Kill switch ENGAGED" in output
    assert "Model call model=claude-haiku-5-5 input_tokens=2700 output_tokens=790" in output
    assert "call_cost=$0.0006" in output
    assert "Model spending cap reached cap=$0.00" in output
