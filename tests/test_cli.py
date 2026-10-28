import json
import os
import subprocess
import sys


def test_cli_outputs_valid_json_and_markdown(tmp_path):
    output, markdown = tmp_path / "r.json", tmp_path / "r.md"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_desk.cli",
            "analyze",
            "AAPL",
            "--as-of",
            "2025-04-23T22:00:00Z",
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "reasoning=deterministic" in result.stderr
    assert json.loads(output.read_text())["data"]["synthetic"]
    assert "Execution trace" in markdown.read_text()


def test_cli_requires_explicit_llm_credentials():
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "trading_desk.cli", "analyze", "AAPL", "--mode", "llm"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "OPENAI_API_KEY" in result.stderr
    assert not result.stdout
