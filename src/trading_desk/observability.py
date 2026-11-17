"""Logging for the desk: one configuration, full stack traces, secrets removed.

Reports and traces deliberately carry only an exception's class name, because
exception text can contain a credential-bearing URL. That keeps secrets out of
anything the desk publishes, but on its own it makes a failure impossible to
debug. The log is where the detail goes: every handled failure is logged with
its stack trace, and every line passes through a redactor that removes secrets
before it reaches the console or the log file.
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

LOGGER_NAME = "trading_desk"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
DEFAULT_SERVER_LOG_FILE = Path("logs/trading-desk.log")
LOG_FILE_MAXIMUM_BYTES = 5_000_000
LOG_FILE_BACKUP_COUNT = 3
REDACTED = "[REDACTED]"
# Environment variables whose values must never appear in a log line.
SECRET_ENVIRONMENT_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)(_ID)?$")
MINIMUM_SECRET_LENGTH = 8
# Secrets recognised by shape, for values that never came from this environment.
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd)"
    r"(\"?'?\s*[=:]\s*\"?'?)([^\s&\"',;}]+)"
)
BEARER_CREDENTIAL = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
PROVIDER_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
CREDENTIALS_IN_URL = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")


def secret_values_in_environment() -> list[str]:
    """Current values of every environment variable that looks like a credential."""
    return sorted(
        {
            value
            for name, value in os.environ.items()
            if SECRET_ENVIRONMENT_NAME.search(name) and len(value) >= MINIMUM_SECRET_LENGTH
        },
        key=len,
        reverse=True,
    )


def redact(text: str) -> str:
    """Remove credentials from a piece of text.

    Removes the value of any credential-like environment variable wherever it
    appears, then anything shaped like a secret: `apikey=...`, `token: ...`,
    `Bearer ...`, `sk-...` keys and `user:password@` in a URL.
    """
    for secret_value in secret_values_in_environment():
        text = text.replace(secret_value, REDACTED)
    text = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    text = BEARER_CREDENTIAL.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    text = PROVIDER_KEY.sub(REDACTED, text)
    return CREDENTIALS_IN_URL.sub(lambda match: f"{match.group(1)}{REDACTED}@", text)


class RedactingFormatter(logging.Formatter):
    """Formats a record, stack trace included, then removes any secret from the result."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(
    level: str = "WARNING", log_file: Path | None = None, stream: TextIO | None = None
) -> logging.Logger:
    """Set up the desk's logger: console always, a rotating file when asked.

    Safe to call more than once: handlers from an earlier call are replaced, not
    stacked. Every handler uses the redacting formatter, so stack traces can be
    logged in full. Returns the configured logger.
    """
    if level not in LOG_LEVELS:
        raise ValueError(f"log level must be one of {', '.join(LOG_LEVELS)}")
    desk_logger = logging.getLogger(LOGGER_NAME)
    for existing_handler in list(desk_logger.handlers):
        desk_logger.removeHandler(existing_handler)
        existing_handler.close()
    formatter = RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(stream)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=LOG_FILE_MAXIMUM_BYTES,
                backupCount=LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        desk_logger.addHandler(handler)
    desk_logger.setLevel(level)
    # The desk owns its output; without this, lines would repeat on the root logger.
    desk_logger.propagate = False
    return desk_logger


def get_logger(component: str) -> logging.Logger:
    """The logger for one part of the desk, for example `get_logger("server.session")`."""
    return logging.getLogger(f"{LOGGER_NAME}.{component}")
