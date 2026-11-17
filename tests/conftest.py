"""Shared test setup."""

import os

# Tests must never pick up a developer's real API keys from a local .env file.
# Set at import so subprocess-based CLI tests inherit it too.
os.environ["TRADING_DESK_SKIP_ENV_FILE"] = "1"
