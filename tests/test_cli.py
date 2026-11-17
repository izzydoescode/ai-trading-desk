import json
import os
import subprocess
import sys
from datetime import UTC, datetime

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


def test_desk_decide_prints_a_six_agent_report(monkeypatch, capsys):
    exit_code = run_cli_in_process(
        monkeypatch, "desk", "decide", "AAPL", "--as-of", HISTORICAL_EVALUATION_TIME
    )
    captured = capsys.readouterr()
    desk_report = json.loads(captured.out)
    assert exit_code == 0
    assert desk_report["schema_version"] == "2.0"
    assert desk_report["decision"]["action"] == "TRADE"
    assert desk_report["decision"]["plan"] == desk_report["risk"]["plan"]
    assert "decision=TRADE" in captured.err
    assert "Paper trading only" in captured.err


def test_desk_decide_honours_the_equity_option(monkeypatch, capsys):
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "decide",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--equity",
        "100",
    )
    desk_report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert desk_report["account"]["equity"] == 100
    assert desk_report["decision"]["action"] == "REJECT"


def test_desk_scan_writes_ranked_reports_to_a_file(monkeypatch, tmp_path, capsys):
    output_path = tmp_path / "scan.json"
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "scan",
        "MSFT",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--output",
        str(output_path),
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert [report["symbol"] for report in json.loads(output_path.read_text())] == ["AAPL", "MSFT"]
    assert "MSFT: scout=NEED_MORE_DATA" in captured.err


def test_desk_scan_defaults_to_the_market_watchlist(monkeypatch, capsys):
    exit_code = run_cli_in_process(
        monkeypatch, "desk", "scan", "--as-of", HISTORICAL_EVALUATION_TIME
    )
    assert exit_code == 0
    assert len(json.loads(capsys.readouterr().out)) == 10


def test_desk_reads_history_from_a_csv_folder(monkeypatch, tmp_path, capsys):
    rows = ["Date,Open,High,Low,Close,Volume"]
    rows += [
        f"2025-03-{day:02d},{100 + day},{101 + day},{99 + day},{100 + day},1000"
        for day in range(1, 11)
    ]
    (tmp_path / "AAPL.csv").write_text("\n".join(rows) + "\n")
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "decide",
        "AAPL",
        "--source",
        "csv",
        "--csv-dir",
        str(tmp_path),
        "--as-of",
        "2025-03-11T00:00:00Z",
    )
    desk_report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert desk_report["data"]["source"] == "local-csv-daily"
    assert desk_report["indicators"]["candle_count"] == 10
    assert desk_report["decision"]["action"] == "REJECT"


def test_csv_source_requires_a_folder(monkeypatch, capsys):
    exit_code = run_cli_in_process(monkeypatch, "desk", "decide", "AAPL", "--source", "csv")
    assert exit_code == 2
    assert "--csv-dir" in capsys.readouterr().err


def test_desk_reports_unwritable_output(monkeypatch, tmp_path, capsys):
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "decide",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--output",
        str(tmp_path / "missing-folder" / "report.json"),
    )
    assert exit_code == 2
    assert "Could not write report output" in capsys.readouterr().err


BACKTEST_ARGUMENTS = (
    "desk",
    "backtest",
    "AAPL",
    "--start",
    "2025-01-02T00:00:00Z",
    "--end",
    "2025-04-24T00:00:00Z",
)


def test_desk_backtest_reports_in_sample_metrics(monkeypatch, tmp_path, capsys):
    exit_code = run_cli_in_process(
        monkeypatch, *BACKTEST_ARGUMENTS, "--holdout-ledger", str(tmp_path / "ledger.jsonl")
    )
    captured = capsys.readouterr()
    backtest_report = json.loads(captured.out)
    assert exit_code == 0
    assert backtest_report["in_sample"]["bars"] == 80
    assert backtest_report["out_of_sample"] is None
    assert "In-sample: return" in captured.err
    assert "vs SPY n/a" in captured.err
    assert not (tmp_path / "ledger.jsonl").exists()


def test_desk_backtest_guards_the_out_of_sample_window(monkeypatch, tmp_path, capsys):
    holdout_arguments = (
        *BACKTEST_ARGUMENTS,
        "--split",
        "2025-04-01T00:00:00Z",
        "--evaluate-out-of-sample",
        "--holdout-ledger",
        str(tmp_path / "ledger.jsonl"),
        "--output",
        str(tmp_path / "backtest.json"),
    )
    assert run_cli_in_process(monkeypatch, *holdout_arguments) == 0
    first_run = capsys.readouterr()
    assert "Out-of-sample: return" in first_run.err
    assert json.loads((tmp_path / "backtest.json").read_text())["out_of_sample"] is not None
    assert run_cli_in_process(monkeypatch, *holdout_arguments) == 2
    assert "Holdout refused" in capsys.readouterr().err
    assert run_cli_in_process(monkeypatch, *holdout_arguments, "--allow-holdout-reuse") == 0
    assert "contaminated" in capsys.readouterr().err


def test_desk_backtest_rejects_a_bad_split(monkeypatch, tmp_path, capsys):
    exit_code = run_cli_in_process(
        monkeypatch,
        *BACKTEST_ARGUMENTS,
        "--split",
        "2030-01-01T00:00:00Z",
        "--holdout-ledger",
        str(tmp_path / "ledger.jsonl"),
    )
    assert exit_code == 2
    assert "split" in capsys.readouterr().err


@pytest.mark.parametrize(
    "provider,key_name", [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")]
)
def test_desk_provider_requires_its_key(monkeypatch, capsys, provider, key_name):
    monkeypatch.delenv(key_name, raising=False)
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "decide",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--provider",
        provider,
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert key_name in captured.err
    assert captured.out == ""


def test_anthropic_credentials_can_come_from_an_api_key(monkeypatch):
    from trading_desk import cli

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert cli.anthropic_credentials_available()


def test_anthropic_credentials_can_come_from_an_ant_sign_in(monkeypatch):
    from trading_desk import cli

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli.anthropic, "default_credentials", lambda: object())
    assert cli.anthropic_credentials_available()


def test_anthropic_credentials_are_missing_without_a_key_or_a_sign_in(monkeypatch):
    from trading_desk import cli

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # conftest points the SDK at a profile folder that does not exist.
    assert not cli.anthropic_credentials_available()
    monkeypatch.setattr(cli.anthropic, "default_credentials", lambda: None)
    assert not cli.anthropic_credentials_available()


def test_desk_anthropic_review_runs_on_an_ant_sign_in_without_an_api_key(
    monkeypatch, tmp_path, capsys
):
    from types import SimpleNamespace

    from trading_desk import cli
    from trading_desk.models import GateVerdict

    review_requests = []

    class FakeMessages:
        async def parse(self, **request):
            review_requests.append(request)
            return SimpleNamespace(
                parsed_output=GateVerdict(
                    approve=False, rationale="Evidence is thin.", concerns=[]
                ),
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
            )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli.anthropic, "default_credentials", lambda: object())
    monkeypatch.setattr(
        cli.anthropic, "AsyncAnthropic", lambda: SimpleNamespace(messages=FakeMessages())
    )
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "decide",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--provider",
        "anthropic",
        "--model-cache-dir",
        str(tmp_path),
    )
    captured = capsys.readouterr()
    desk_report = json.loads(captured.out)
    assert exit_code == 0
    assert len(review_requests) == 1
    assert review_requests[0]["model"] == "claude-haiku-5-5"
    assert desk_report["model_review"]["approve"] is False
    assert desk_report["decision"]["action"] == "WAIT"


def test_desk_openai_review_is_metered_and_cached(monkeypatch, tmp_path, capsys):
    import httpx

    request_count = 0
    approval_text = json.dumps({"approve": True, "rationale": "Reports agree.", "concerns": []})

    def handler(request):
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": approval_text}]}
                ],
            },
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "trading_desk.cli.httpx.AsyncClient",
        lambda: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    arguments = (
        "desk",
        "decide",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--provider",
        "openai",
        "--model-cache-dir",
        str(tmp_path),
    )
    assert run_cli_in_process(monkeypatch, *arguments) == 0
    first_run = capsys.readouterr()
    desk_report = json.loads(first_run.out)
    assert desk_report["mode"] == "llm"
    assert desk_report["model_review"]["approve"] is True
    assert desk_report["decision"]["action"] == "TRADE"
    assert "calls=1" in first_run.err
    assert "test-key" not in first_run.err + first_run.out
    assert run_cli_in_process(monkeypatch, *arguments) == 0
    assert "calls=0" in capsys.readouterr().err
    assert request_count == 1


class FakeUvicornServer:
    """Stands in for uvicorn so `desk serve` can be tested without opening a port."""

    started_configs: list = []

    def __init__(self, config):
        self.config = config

    async def serve(self):
        FakeUvicornServer.started_configs.append(self.config)


@pytest.fixture
def fake_uvicorn_server(monkeypatch):
    from trading_desk import cli

    FakeUvicornServer.started_configs = []
    monkeypatch.setattr(cli.uvicorn, "Server", FakeUvicornServer)
    return FakeUvicornServer


def test_desk_serve_builds_a_loopback_app_with_fixture_defaults(
    monkeypatch, capsys, fake_uvicorn_server
):
    from fastapi.testclient import TestClient

    assert run_cli_in_process(monkeypatch, "desk", "serve") == 0
    (config,) = fake_uvicorn_server.started_configs
    assert (config.host, config.port) == ("127.0.0.1", 8000)
    assert "http://127.0.0.1:8000" in capsys.readouterr().err
    with TestClient(config.app) as client:
        assert client.get("/api/health").json()["venue"] == "paper"
        assert client.post("/api/replay/start", json={"bars_per_second": 1000}).status_code == 202
        settings = client.get("/api/state").json()["settings"]
    assert settings["symbols"] == ["AAPL"]
    assert settings["start"].startswith("2025-01-02")
    assert settings["end"].startswith("2025-04-23")


def test_desk_serve_takes_symbols_window_speed_port_and_a_journal(
    monkeypatch, tmp_path, fake_uvicorn_server
):
    journal_path = tmp_path / "nested" / "journal.sqlite"
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "serve",
        "aapl",
        "--start",
        "2025-02-03T00:00:00Z",
        "--end",
        "2025-04-01T00:00:00Z",
        "--speed",
        "16",
        "--port",
        "8123",
        "--equity",
        "25000",
        "--journal",
        str(journal_path),
    )
    assert exit_code == 0
    (config,) = fake_uvicorn_server.started_configs
    assert config.port == 8123
    assert journal_path.is_file()


def test_desk_serve_defaults_to_the_watchlist_and_a_trailing_year_for_real_data(
    monkeypatch, tmp_path
):
    from trading_desk import cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["trading-desk", "desk", "serve", "--source", "csv", "--csv-dir", str(tmp_path)],
    )
    settings = cli.build_replay_settings(cli.build_parser().parse_args())
    assert settings.symbols == list(cli.US_EQUITIES.default_watchlist)
    assert settings.end - settings.start == cli.DEFAULT_REPLAY_WINDOW


@pytest.mark.parametrize(
    "extra_arguments,expected_message",
    [
        (["--host", "0.0.0.0"], "only listens on this machine"),  # noqa: S104
        (["--host", "192.168.1.20"], "only listens on this machine"),
        (["--start", "2025-04-01T00:00:00Z", "--end", "2025-01-01T00:00:00Z"], "invalid replay"),
        (["--speed", "0"], "invalid replay"),
        (["--equity", "-5"], "invalid replay"),
    ],
)
def test_desk_serve_refuses_unsafe_or_invalid_configuration(
    monkeypatch, capsys, fake_uvicorn_server, extra_arguments, expected_message
):
    assert run_cli_in_process(monkeypatch, "desk", "serve", *extra_arguments) == 2
    assert expected_message in capsys.readouterr().err
    assert fake_uvicorn_server.started_configs == []


def test_desk_serve_serves_a_built_terminal_when_asked_or_found(
    monkeypatch, tmp_path, capsys, fake_uvicorn_server
):
    from fastapi.testclient import TestClient

    from trading_desk import cli

    built_ui = tmp_path / "dist"
    built_ui.mkdir()
    (built_ui / "index.html").write_text("<!doctype html><title>AI Trading Desk</title>")

    assert run_cli_in_process(monkeypatch, "desk", "serve", "--ui-dir", str(built_ui)) == 0
    assert "Desk terminal on" in capsys.readouterr().err
    with TestClient(fake_uvicorn_server.started_configs[-1].app) as client:
        assert "AI Trading Desk" in client.get("/").text

    monkeypatch.setattr(cli, "DEFAULT_UI_DIRECTORY", built_ui)
    assert run_cli_in_process(monkeypatch, "desk", "serve") == 0
    assert "Desk terminal on" in capsys.readouterr().err

    assert run_cli_in_process(monkeypatch, "desk", "serve", "--no-ui") == 0
    assert "API (no built UI found)" in capsys.readouterr().err
    with TestClient(fake_uvicorn_server.started_configs[-1].app) as client:
        assert client.get("/").status_code == 404


def test_desk_serve_reports_an_unbuilt_ui_directory(monkeypatch, tmp_path, capsys):
    assert run_cli_in_process(monkeypatch, "desk", "serve", "--ui-dir", str(tmp_path)) == 2
    assert "index.html" in capsys.readouterr().err


def test_news_finnhub_requires_its_key(monkeypatch, capsys, fake_uvicorn_server):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    for command in (["decide", "AAPL"], ["scan", "AAPL"], ["serve"]):
        exit_code = run_cli_in_process(
            monkeypatch,
            "desk",
            *command,
            "--as-of",
            HISTORICAL_EVALUATION_TIME,
            "--news",
            "finnhub",
        )
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "FINNHUB_API_KEY" in captured.err
        assert captured.out == ""
    assert fake_uvicorn_server.started_configs == []


def test_desk_decide_reads_dated_news_from_finnhub(monkeypatch, tmp_path, capsys):
    import httpx

    news_requests = []

    def handler(request):
        news_requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "datetime": int(datetime(2025, 4, 22, 14, tzinfo=UTC).timestamp()),
                    "headline": "Supplier confirms shipment schedule",
                    "source": "Reuters",
                    "url": "https://finnhub.io/api/news?id=1",
                },
                {
                    "datetime": int(datetime(2025, 4, 23, 23, tzinfo=UTC).timestamp()),
                    "headline": "Published after the evaluation time",
                    "source": "Reuters",
                    "url": "https://finnhub.io/api/news?id=2",
                },
            ],
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "trading_desk.cli.httpx.AsyncClient",
        lambda: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr("trading_desk.tools.news.FINNHUB_MINIMUM_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setenv("FINNHUB_API_KEY", "test-finnhub-key")
    exit_code = run_cli_in_process(
        monkeypatch,
        "desk",
        "decide",
        "AAPL",
        "--as-of",
        HISTORICAL_EVALUATION_TIME,
        "--news",
        "finnhub",
        "--news-cache-dir",
        str(tmp_path),
    )
    captured = capsys.readouterr()
    desk_report = json.loads(captured.out)
    assert exit_code == 0
    assert [item["headline"] for item in desk_report["news"]["verified_facts"]] == [
        "Supplier confirms shipment schedule"
    ]
    assert "Published after" not in captured.out
    assert news_requests and all("test-finnhub-key" not in str(r.url) for r in news_requests)
    assert "test-finnhub-key" not in captured.out + captured.err
    assert (tmp_path / "finnhub" / "AAPL").is_dir()
