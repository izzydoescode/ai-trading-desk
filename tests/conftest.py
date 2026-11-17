"""Shared test setup."""

import os

# Tests must never pick up a developer's real API keys from a local .env file.
# Set at import so subprocess-based CLI tests inherit it too.
os.environ["TRADING_DESK_SKIP_ENV_FILE"] = "1"
# Nor a developer's `ant` sign-in: point the Anthropic SDK at a profile folder
# that does not exist, so no test can authenticate or make a paid call.
os.environ["ANTHROPIC_CONFIG_DIR"] = "/nonexistent-trading-desk-test-profile"
os.environ.pop("ANTHROPIC_PROFILE", None)

import logging  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch, tmp_path):
    """Test fixture (not a test): keeps every test's logging to itself.

    `desk serve` logs to a file by default; here that file goes to a temporary
    folder instead of the repository. Handlers a test installs are removed
    afterwards so open log files and levels never leak into the next test.
    """
    from trading_desk import cli
    from trading_desk.observability import LOGGER_NAME

    monkeypatch.setattr(cli, "DEFAULT_SERVER_LOG_FILE", tmp_path / "logs" / "trading-desk.log")
    yield
    desk_logger = logging.getLogger(LOGGER_NAME)
    for handler in list(desk_logger.handlers):
        desk_logger.removeHandler(handler)
        handler.close()
    desk_logger.setLevel(logging.NOTSET)
