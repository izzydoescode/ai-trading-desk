import json
import os
import subprocess
import sys

import pytest


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


HISTORICAL_EVALUATION_TIME = "2025-04-23T22:00:00Z"


def run_cli_in_process(monkeypatch, *arguments):
    """Call the CLI entry point directly so coverage sees it, returning its exit code."""
    from trading_desk import cli

    monkeypatch.setattr(sys, "argv", ["trading-desk", *arguments])
    return cli.main()


def test_main_prints_report_json_to_stdout(monkeypatch, capsys):
    exit_code = run_cli_in_process(
        monkeypatch, "analyze", "AAPL", "--as-of", HISTORICAL_EVALUATION_TIME
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 0
    assert report["symbol"] == "AAPL"
    assert report["abstained"] is False
    assert "Classification=" in captured.err


def test_main_writes_json_and_markdown_files(monkeypatch, tmp_path, capsys):
    json_path, markdown_path = tmp_path / "report.json", tmp_path / "report.md"
    exit_code = run_cli_in_process(
        monkeypatch,
        "analyze",
        "AAPL",
        "--scenario",
        "conflict",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--output",
        str(json_path),
        "--markdown",
        str(markdown_path),
        "--verbose",
    )
    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert json.loads(json_path.read_text())["classification"] == "NEUTRAL"
    markdown_text = markdown_path.read_text()
    assert "## Dissent" in markdown_text
    assert "## Measured observations" in markdown_text


def test_main_reports_abstention_with_exit_zero(monkeypatch, capsys):
    exit_code = run_cli_in_process(
        monkeypatch,
        "analyze",
        "AAPL",
        "--scenario",
        "missing",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["abstained"] is True


def test_main_rejects_llm_mode_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    exit_code = run_cli_in_process(monkeypatch, "analyze", "AAPL", "--mode", "llm")
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "OPENAI_API_KEY" in captured.err
    assert captured.out == ""


def test_main_rejects_external_data_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    exit_code = run_cli_in_process(monkeypatch, "analyze", "AAPL", "--source", "alpha-vantage")
    assert exit_code == 2
    assert "ALPHAVANTAGE_API_KEY" in capsys.readouterr().err


def test_main_returns_error_when_output_path_is_unwritable(monkeypatch, tmp_path, capsys):
    missing_directory_path = tmp_path / "no-such-directory" / "report.json"
    exit_code = run_cli_in_process(
        monkeypatch,
        "analyze",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--output",
        str(missing_directory_path),
    )
    assert exit_code == 2
    assert "Could not write report output" in capsys.readouterr().err


def test_scenario_flag_is_rejected_for_external_data(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_cli_in_process(
            monkeypatch,
            "analyze",
            "AAPL",
            "--source",
            "alpha-vantage",
            "--scenario",
            "conflict",
        )
    assert exit_info.value.code == 2
    assert "--scenario applies only to fixture data" in capsys.readouterr().err
