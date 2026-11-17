"""Shared test setup."""

import os

# Tests must never pick up a developer's real API keys from a local .env file.
# Set at import so subprocess-based CLI tests inherit it too.
os.environ["TRADING_DESK_SKIP_ENV_FILE"] = "1"
# Nor a developer's `ant` sign-in: point the Anthropic SDK at a profile folder
# that does not exist, so no test can authenticate or make a paid call.
os.environ["ANTHROPIC_CONFIG_DIR"] = "/nonexistent-trading-desk-test-profile"
os.environ.pop("ANTHROPIC_PROFILE", None)
