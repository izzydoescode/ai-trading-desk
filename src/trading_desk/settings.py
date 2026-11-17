"""Local settings: an optional git-ignored `.env` file for API keys.

Keys belong in the environment, never in code or chat. Values already exported
in the shell win over the file, so a one-off override stays simple.
"""

import os
from pathlib import Path

SKIP_ENV_FILE_VARIABLE = "TRADING_DESK_SKIP_ENV_FILE"
DEFAULT_ENV_FILE = Path(".env")


def load_env_file(env_file_path: Path = DEFAULT_ENV_FILE) -> list[str]:
    """Copy `KEY=VALUE` lines into the environment; return the names that were set.

    Blank values, comments and names that are already set are ignored. Returns
    names only, so callers can log what was loaded without exposing a secret.
    """
    if os.environ.get(SKIP_ENV_FILE_VARIABLE) or not env_file_path.is_file():
        return []
    loaded_names = []
    for line in env_file_path.read_text().splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        name, _, value = stripped_line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip().strip("'\"")
        if name and value and name not in os.environ:
            os.environ[name] = value
            loaded_names.append(name)
    return loaded_names
