"""CLI with explicit data/reasoning modes and machine-readable stdout."""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import httpx
import uvicorn

from trading_desk.agents.neo import SetupRules
from trading_desk.backtest.runner import (
    BacktestReport,
    HoldoutAlreadyUsedError,
    HoldoutLedger,
    run_backtest,
)
from trading_desk.desk import run_desk_decision, scan_watchlist
from trading_desk.graph import run_research
from trading_desk.llm import (
    AnthropicProvider,
    CachingReasoningProvider,
    OpenAIProvider,
    ReasoningProvider,
    UsageMeter,
)
from trading_desk.markets import US_EQUITIES
from trading_desk.models import AccountSnapshot, DeskReport, Report, TradeSide
from trading_desk.observability import (
    DEFAULT_SERVER_LOG_FILE,
    LOG_LEVELS,
    configure_logging,
    get_logger,
)
from trading_desk.paper.journal import Journal
from trading_desk.server import DeskSession, ReplaySettings, create_app
from trading_desk.settings import load_env_file
from trading_desk.tools.alpaca import AlpacaBarsProvider, AlpacaClient, AlpacaNewsFeed
from trading_desk.tools.historical_data import (
    CachingProvider,
    CsvDirectoryProvider,
    YahooChartProvider,
)
from trading_desk.tools.market_data import (
    AlphaVantageProvider,
    FixtureProvider,
    MarketDataProvider,
)
from trading_desk.tools.news import FallbackNewsFeed, FinnhubNewsFeed, NewsFeed
from trading_desk.tools.sentiment import HeadlineToneFeed, SentimentFeed

logger = get_logger("cli")

# "auto" uses Alpaca when its keys are configured and the bundled fixture otherwise.
DATA_SOURCES = ["auto", "alpaca", "fixture", "alpha-vantage", "yahoo", "csv"]
DEFAULT_PAPER_EQUITY = 100_000.0
DEFAULT_CACHE_DIRECTORY = Path("data/market_cache")
DEFAULT_HOLDOUT_LEDGER = Path("research/holdout_log.jsonl")
DEFAULT_MODEL_CACHE_DIRECTORY = Path("data/model_cache")
DEFAULT_NEWS_CACHE_DIRECTORY = Path("data/news_cache")
# "auto" uses Alpaca news with Finnhub as its backup, whichever of them is configured.
NEWS_SOURCES = ["auto", "alpaca", "finnhub", "none"]
SIDE_CHOICES = {
    "both": (TradeSide.LONG, TradeSide.SHORT),
    "long": (TradeSide.LONG,),
    "short": (TradeSide.SHORT,),
}
SENTIMENT_SOURCES = ["none", "headline-tone"]
DEFAULT_MAXIMUM_MODEL_COST_USD = 0.50
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
# The server has no login, so it only ever listens on this machine.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_SERVER_PORT = 8000
DEFAULT_REPLAY_BARS_PER_SECOND = 2.0
DEFAULT_REPLAY_WINDOW = timedelta(days=365)
# The bundled synthetic candles only cover this window and this symbol.
FIXTURE_REPLAY_START = datetime(2025, 1, 2, tzinfo=UTC)
FIXTURE_REPLAY_END = datetime(2025, 4, 23, 22, tzinfo=UTC)
FIXTURE_SYMBOLS = ["AAPL"]
DEFAULT_UI_DIRECTORY = Path("web/dist")
# Every replay is kept by default so trades can be reviewed later. data/ is git-ignored.
DEFAULT_JOURNAL_FILE = Path("data/journal.sqlite")


def markdown(report: Report) -> str:
    """Render a research report as a short Markdown document for people to read."""
    i = report.indicators
    lines = [
        f"# {report.symbol} — {report.classification}",
        "",
        f"Mode: {report.mode} | Source: {report.data.source} | Synthetic: {report.data.synthetic}",
        f"Evaluated as of: {report.evaluated_as_of.isoformat()}",
        f"Market data timestamp: {report.market_data_timestamp}",
        f"Abstained: {report.abstained}",
        "",
        "## Measured observations",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines += [f"| {k} | {v} |" for k, v in i.model_dump().items()]
    for name in ("technical", "contrarian", "synthesis"):
        analysis = getattr(report, name)
        lines += ["", f"## {name.title()}", "", analysis.summary, ""]
        lines += [f"- {r}" for r in analysis.risks]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in report.limitations]
    lines += ["", "## Dissent", ""] + [f"- {x}" for x in report.dissent]
    lines += [
        "",
        "## Execution trace",
        "",
        "| Step | Status | Provider | ms |",
        "|---|---|---|---:|",
    ]
    lines += [f"| {t.agent} | {t.status} | {t.provider} | {t.duration_ms} |" for t in report.trace]
    return "\n".join(lines + ["", report.purpose, ""])


def alpaca_keys() -> tuple[str, str] | None:
    """The Alpaca key id and secret from the environment, or None if either is missing."""
    key_id, secret_key = (
        os.environ.get("ALPACA_API_KEY_ID"),
        os.environ.get("ALPACA_API_SECRET_KEY"),
    )
    return (key_id, secret_key) if key_id and secret_key else None


def build_alpaca_client(client: httpx.AsyncClient) -> AlpacaClient:
    """An authenticated Alpaca data client, or a clear error naming the missing keys."""
    keys = alpaca_keys()
    if keys is None:
        raise ValueError(
            "Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env or the shell for Alpaca"
        )
    return AlpacaClient(*keys, client)


def resolve_automatic_sources(args) -> None:
    """Replace "auto" with a concrete choice: Alpaca when its keys are configured, and the
    offline fallbacks otherwise, so the desk works out of the box either way.

    A fixture scenario asked for by name always means the fixture.
    """
    has_alpaca = alpaca_keys() is not None
    if args.source == "auto":
        args.source = "alpaca" if has_alpaca and args.scenario == "trend" else "fixture"
    if getattr(args, "news", None) == "auto":
        if args.source == "fixture":
            # Real headlines beside synthetic prices would mislead.
            args.news = "none"
        elif has_alpaca:
            args.news = "alpaca"
        else:
            args.news = "finnhub" if os.environ.get("FINNHUB_API_KEY") else "none"


def build_data_provider(args, client: httpx.AsyncClient) -> MarketDataProvider:
    """Create the market data source the user asked for, failing loudly if unconfigured."""
    if args.source == "alpaca":
        return AlpacaBarsProvider(build_alpaca_client(client))
    if args.source == "alpha-vantage":
        key = os.environ.get("ALPHAVANTAGE_API_KEY")
        if not key:
            raise ValueError("Export ALPHAVANTAGE_API_KEY for alpha-vantage data")
        return AlphaVantageProvider(key, client)
    if args.source == "yahoo":
        return CachingProvider(YahooChartProvider(client), args.cache_dir)
    if args.source == "csv":
        if args.csv_dir is None:
            raise ValueError("Pass --csv-dir with a folder of <SYMBOL>.csv files for csv data")
        return CsvDirectoryProvider(args.csv_dir)
    return FixtureProvider(args.scenario)


def build_setup_rules(args) -> SetupRules:
    """NEO's rules for this run: the default strategies, limited to the sides asked for."""
    return SetupRules(allowed_sides=SIDE_CHOICES[args.sides])


def build_news_feed(args, client: httpx.AsyncClient) -> NewsFeed | None:
    """Create the news feed SNOWDEN reads, or None so it abstains for lack of a source."""
    if args.news == "none":
        return None
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if args.news == "finnhub":
        if not finnhub_key:
            raise ValueError("Set FINNHUB_API_KEY in .env or the shell for --news finnhub")
        return FinnhubNewsFeed(finnhub_key, client, args.news_cache_dir)
    # Alpaca is the main feed; Finnhub covers for it when Alpaca fails, if it is configured.
    alpaca_feed = AlpacaNewsFeed(build_alpaca_client(client), args.news_cache_dir)
    if not finnhub_key:
        return alpaca_feed
    return FallbackNewsFeed(alpaca_feed, FinnhubNewsFeed(finnhub_key, client, args.news_cache_dir))


def build_sentiment_feed(
    args, news_feed: NewsFeed | None, llm: ReasoningProvider | None
) -> SentimentFeed | None:
    """Create FAMA's sentiment feed, or None so it abstains for lack of a source."""
    if args.sentiment == "none":
        return None
    if news_feed is None or llm is None:
        raise ValueError(
            "--sentiment headline-tone needs headlines and a model: add --news finnhub and "
            "--provider anthropic or openai"
        )
    return HeadlineToneFeed(news_feed, llm)


def build_news_tone_feed(
    args, news_feed: NewsFeed | None, llm: ReasoningProvider | None
) -> SentimentFeed | None:
    """Create the headline-tone reader that lets SNOWDEN call news bullish or bearish."""
    if not args.news_tone:
        return None
    if news_feed is None or llm is None:
        raise ValueError(
            "--news-tone needs headlines and a model: add --news finnhub and "
            "--provider anthropic or openai"
        )
    return HeadlineToneFeed(news_feed, llm)


def anthropic_credentials_available() -> bool:
    """True when the Anthropic SDK can authenticate on this machine.

    Either an API key is in the environment, or the user has signed in with the
    `ant` command-line tool and the SDK can load that profile. The credential
    itself is never read here.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        return anthropic.default_credentials() is not None
    except anthropic.CredentialsError:
        return False


def build_reasoning_provider(
    args, client: httpx.AsyncClient, usage_meter: UsageMeter
) -> ReasoningProvider | None:
    """Create the model reviewer the user asked for, or None for rules only."""
    if args.provider == "none":
        return None
    if args.provider == "anthropic":
        if not anthropic_credentials_available():
            raise ValueError(
                "For --provider anthropic, set ANTHROPIC_API_KEY in .env or the shell, "
                "or sign in with `ant auth login`"
            )
        provider: ReasoningProvider = AnthropicProvider(
            anthropic.AsyncAnthropic(),
            os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
            usage_meter,
        )
    else:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("Set OPENAI_API_KEY in .env or the shell for --provider openai")
        provider = OpenAIProvider(
            key, os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL), client, usage_meter
        )
    return CachingReasoningProvider(provider, args.model_cache_dir)


def parse_evaluation_time(raw_value: str | None) -> datetime | None:
    return datetime.fromisoformat(raw_value.replace("Z", "+00:00")) if raw_value else None


async def execute(args) -> Report:
    async with httpx.AsyncClient() as client:
        provider = build_data_provider(args, client)
        llm = None
        if args.mode == "llm":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ValueError(
                    "Export OPENAI_API_KEY for LLM mode; no silent simulation fallback"
                )
            llm = OpenAIProvider(key, os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), client)
        return await run_research(args.symbol, provider, llm, parse_evaluation_time(args.as_of))


async def execute_desk(args) -> list[DeskReport]:
    """Run the six-agent desk for one symbol (`decide`) or a watchlist (`scan`)."""
    account = AccountSnapshot(equity=args.equity, cash=args.equity)
    evaluation_time = parse_evaluation_time(args.as_of)
    usage_meter = UsageMeter(args.max_model_cost)
    async with httpx.AsyncClient() as client:
        provider = build_data_provider(args, client)
        llm = build_reasoning_provider(args, client, usage_meter)
        news_feed = build_news_feed(args, client)
        feeds = {
            "news_feed": news_feed,
            "sentiment_feed": build_sentiment_feed(args, news_feed, llm),
            "news_tone_feed": build_news_tone_feed(args, news_feed, llm),
        }
        if args.desk_command == "decide":
            desk_reports = [
                await run_desk_decision(
                    args.symbol,
                    provider,
                    account,
                    evaluation_time,
                    llm=llm,
                    setup_rules=build_setup_rules(args),
                    **feeds,
                )
            ]
        else:
            symbols = args.symbols or list(US_EQUITIES.default_watchlist)
            desk_reports = await scan_watchlist(
                symbols,
                provider,
                account,
                evaluation_time,
                llm=llm,
                setup_rules=build_setup_rules(args),
                **feeds,
            )
    if llm is not None:
        print(
            f"Model={llm.name}; calls={usage_meter.call_count}; "
            f"cost=${usage_meter.cost_usd:.4f} of ${args.max_model_cost:.2f} cap",
            file=sys.stderr,
        )
    return desk_reports


async def execute_backtest(args) -> BacktestReport:
    """Run the backtest described by the command-line arguments and return its report."""
    start = parse_evaluation_time(args.start)
    end = parse_evaluation_time(args.end) or datetime.now(UTC)
    journal = None
    if args.journal is not None:
        args.journal.parent.mkdir(parents=True, exist_ok=True)
        journal = Journal(args.journal)
    try:
        async with httpx.AsyncClient() as client:
            return await run_backtest(
                args.symbols or list(US_EQUITIES.default_watchlist),
                build_data_provider(args, client),
                start=start,
                end=end,
                split=parse_evaluation_time(args.split),
                evaluate_out_of_sample=args.evaluate_out_of_sample,
                holdout_ledger=HoldoutLedger(args.holdout_ledger),
                allow_holdout_reuse=args.allow_holdout_reuse,
                starting_cash=args.equity,
                setup_rules=build_setup_rules(args),
                journal=journal,
            )
    finally:
        if journal is not None:
            journal.close()


def build_replay_settings(args) -> ReplaySettings:
    """Default replay for `desk serve`: the terminal can override it per run."""
    is_fixture = args.source == "fixture"
    end = parse_evaluation_time(args.end) or (
        FIXTURE_REPLAY_END if is_fixture else datetime.now(UTC)
    )
    start = parse_evaluation_time(args.start) or (
        FIXTURE_REPLAY_START if is_fixture else end - DEFAULT_REPLAY_WINDOW
    )
    default_symbols = FIXTURE_SYMBOLS if is_fixture else list(US_EQUITIES.default_watchlist)
    return ReplaySettings(
        symbols=[symbol.upper() for symbol in args.symbols] or default_symbols,
        start=start,
        end=end,
        bars_per_second=args.speed,
        starting_cash=args.equity,
    )


def resolve_ui_directory(args) -> Path | None:
    """The built terminal to serve: the one asked for, else `web/dist` if it exists."""
    if args.no_ui:
        return None
    if args.ui_dir is not None:
        return args.ui_dir
    return DEFAULT_UI_DIRECTORY if (DEFAULT_UI_DIRECTORY / "index.html").is_file() else None


async def serve_desk(args, replay_settings: ReplaySettings) -> None:
    """Start the local desk server and keep it running until interrupted.

    Builds the data provider, optional journal, news feed, model provider and tone feeds from the
    arguments, creates the session and the API (serving the built UI when one is found), and runs
    uvicorn. On shutdown it closes the journal and prints the metered model cost.
    """
    journal = None
    if not args.no_journal:
        journal_path = args.journal or DEFAULT_JOURNAL_FILE
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal = Journal(journal_path)
        logger.info("Journal open path=%s", journal_path)
    async with httpx.AsyncClient() as client:
        usage_meter = UsageMeter(args.max_model_cost)
        news_feed = build_news_feed(args, client)
        llm = build_reasoning_provider(args, client, usage_meter)
        desk_session = DeskSession(
            build_data_provider(args, client),
            setup_rules=build_setup_rules(args),
            journal=journal,
            news_feed=news_feed,
            sentiment_feed=build_sentiment_feed(args, news_feed, llm),
            news_tone_feed=build_news_tone_feed(args, news_feed, llm),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(desk_session, replay_settings, resolve_ui_directory(args)),
                host=args.host,
                port=args.port,
                log_level="warning",
            )
        )
        try:
            await server.serve()
        finally:
            if journal is not None:
                journal.close()
            if llm is not None:
                print(
                    f"Model={llm.name}; calls={usage_meter.call_count}; "
                    f"cost=${usage_meter.cost_usd:.4f} of ${args.max_model_cost:.2f} cap",
                    file=sys.stderr,
                )


def run_serve_command(args) -> int:
    """Validate the `desk serve` arguments and run the server. Returns the exit code.

    Refuses any host that is not this machine, because the server has no login, and refuses an
    invalid replay window, speed or equity before anything starts.
    """
    if args.host not in LOOPBACK_HOSTS:
        print(
            "Configuration/output error: the desk server has no login and only listens on "
            "this machine; use --host 127.0.0.1",
            file=sys.stderr,
        )
        return 2
    try:
        replay_settings = build_replay_settings(args)
    except ValueError:
        print(
            "Configuration/output error: invalid replay window, symbols, speed or equity",
            file=sys.stderr,
        )
        return 2
    ui_directory = resolve_ui_directory(args)
    print(
        f"Desk {'terminal' if ui_directory else 'API (no built UI found)'} on "
        f"http://{args.host}:{args.port} (replay of {len(replay_settings.symbols)} "
        "symbol(s), paper venue). Press Ctrl+C to stop.",
        file=sys.stderr,
    )
    try:
        asyncio.run(serve_desk(args, replay_settings))
    except (ValueError, OSError) as exc:
        logger.error("Configuration or output error", exc_info=True)
        print(f"Configuration/output error: {exc}", file=sys.stderr)
        return 2
    return 0


def describe_metrics(label: str, metrics) -> str:
    """Format one backtest scorecard as a single line for the terminal."""

    def percent(value: float | None) -> str:
        return "n/a" if value is None else f"{value:+.2%}"

    def number(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    def dollars(value: float | None) -> str:
        return "n/a" if value is None else f"${value:,.2f}"

    return (
        f"{label}: return {percent(metrics.total_return)} vs {metrics.benchmark_symbol} "
        f"{percent(metrics.benchmark_return)}; max drawdown {percent(metrics.max_drawdown)}; "
        f"trades {metrics.trade_count}; win rate {percent(metrics.win_rate)}; "
        f"profit factor {number(metrics.profit_factor)}; Sharpe {number(metrics.sharpe_ratio)}; "
        f"average win {dollars(metrics.average_win_amount)}; "
        f"average loss {dollars(metrics.average_loss_amount)}"
    )


def add_data_arguments(command: argparse.ArgumentParser) -> None:
    """Add the options every data-reading command shares: source, scenario, CSV and cache folders,
    evaluation time, output path and verbosity.
    """
    command.add_argument(
        "--source",
        choices=DATA_SOURCES,
        default="auto",
        help="Market data (default: alpaca when its keys are set, else the bundled fixture)",
    )
    command.add_argument("--scenario", choices=["trend", "conflict", "missing"], default="trend")
    command.add_argument("--csv-dir", type=Path, help="Folder of <SYMBOL>.csv daily bars")
    command.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIRECTORY,
        help="Where downloaded history is cached",
    )
    command.add_argument("--as-of", help="Timezone-aware historical evaluation timestamp")
    command.add_argument("--output", type=Path, help="JSON output path (stdout if omitted)")
    command.add_argument("--verbose", action="store_true", help="Shorthand for --log-level DEBUG")
    command.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        help="Console and file log detail (default: WARNING, or INFO for desk serve)",
    )
    command.add_argument(
        "--log-file",
        type=Path,
        help="Also write logs to this file (desk serve defaults to logs/trading-desk.log)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Define the whole command line: `analyze`, and `desk decide`, `scan`, `backtest` and `serve`
    with their data, model, news, sentiment and equity options.
    """
    parser = argparse.ArgumentParser(description="Research-only market intelligence desk")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Technical thesis with independent review")
    analyze.add_argument("symbol")
    add_data_arguments(analyze)
    analyze.add_argument("--mode", choices=["deterministic", "llm"], default="deterministic")
    analyze.add_argument("--markdown", type=Path, help="Optional Markdown output path")

    desk = sub.add_parser("desk", help="Six-agent paper trading desk")
    desk_commands = desk.add_subparsers(dest="desk_command", required=True)
    decide = desk_commands.add_parser("decide", help="One six-agent decision for one symbol")
    decide.add_argument("symbol")
    scan = desk_commands.add_parser("scan", help="Decide and rank a watchlist")
    scan.add_argument("symbols", nargs="*", help="Symbols to scan (default: market watchlist)")
    backtest = desk_commands.add_parser("backtest", help="Score the desk over history")
    backtest.add_argument("symbols", nargs="*", help="Symbols (default: market watchlist)")
    backtest.add_argument("--start", required=True, help="First bar time, timezone-aware")
    backtest.add_argument("--end", help="Last bar time (default: now)")
    backtest.add_argument("--split", help="Start of the out-of-sample window")
    backtest.add_argument(
        "--evaluate-out-of-sample",
        action="store_true",
        help="Score the out-of-sample window; recorded in the holdout ledger",
    )
    backtest.add_argument(
        "--allow-holdout-reuse",
        action="store_true",
        help="Permit a repeated out-of-sample look (recorded as contaminated)",
    )
    backtest.add_argument("--holdout-ledger", type=Path, default=DEFAULT_HOLDOUT_LEDGER)
    backtest.add_argument(
        "--journal",
        type=Path,
        help="SQLite file to keep every event, trade and trade decision of this backtest",
    )
    serve = desk_commands.add_parser(
        "serve", help="Local API server for the terminal (replay, paper venue only)"
    )
    serve.add_argument("symbols", nargs="*", help="Symbols to replay (default: market watchlist)")
    serve.add_argument("--start", help="First replay bar (default: one year before --end)")
    serve.add_argument("--end", help="Last replay bar (default: now)")
    serve.add_argument(
        "--speed", type=float, default=DEFAULT_REPLAY_BARS_PER_SECOND, help="Replay bars per second"
    )
    serve.add_argument("--host", default="127.0.0.1", help="Loopback address to listen on")
    serve.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    serve.add_argument(
        "--journal",
        type=Path,
        help="SQLite file that keeps every event, trade and trade decision "
        "(default: data/journal.sqlite)",
    )
    serve.add_argument("--no-journal", action="store_true", help="Keep nothing on disk")
    serve.add_argument(
        "--ui-dir", type=Path, help="Built terminal to serve (default: web/dist if present)"
    )
    serve.add_argument("--no-ui", action="store_true", help="Serve the API only")
    for model_command in (decide, scan, serve):
        model_command.add_argument(
            "--provider",
            choices=["none", "anthropic", "openai"],
            default="none",
            help="Model for trade review (decide, scan) and headline tone (default: rules only)",
        )
        model_command.add_argument(
            "--max-model-cost",
            type=float,
            default=DEFAULT_MAXIMUM_MODEL_COST_USD,
            help="Spending cap in US dollars for this run",
        )
        model_command.add_argument(
            "--model-cache-dir", type=Path, default=DEFAULT_MODEL_CACHE_DIRECTORY
        )
    for news_command in (decide, scan, serve):
        news_command.add_argument(
            "--news",
            choices=NEWS_SOURCES,
            default="auto",
            help="News feed for SNOWDEN (default: alpaca with finnhub as backup when "
            "configured; none with fixture data)",
        )
        news_command.add_argument(
            "--news-cache-dir", type=Path, default=DEFAULT_NEWS_CACHE_DIRECTORY
        )
        news_command.add_argument(
            "--news-tone",
            action="store_true",
            help="Let SNOWDEN call the news bullish or bearish from model-labelled headlines",
        )
        news_command.add_argument(
            "--sentiment",
            choices=SENTIMENT_SOURCES,
            default="none",
            help="Sentiment for FAMA: headline-tone labels news headlines with the model",
        )
    for desk_command in (decide, scan, backtest, serve):
        add_data_arguments(desk_command)
        desk_command.add_argument(
            "--equity", type=float, default=DEFAULT_PAPER_EQUITY, help="Paper account equity"
        )
        desk_command.add_argument(
            "--sides",
            choices=sorted(SIDE_CHOICES),
            default="both",
            help="Sides NEO may trade: both (longs in uptrends, shorts in downtrends), "
            "long only, or short only",
        )
    return parser


def write_output(content: str, output_path: Path | None) -> None:
    if output_path:
        output_path.write_text(content)
    else:
        print(content, end="")


def run_backtest_command(args) -> int:
    """Run `desk backtest`, write the report, and print the scorecards. Returns the exit code (2
    for a
    refused holdout or a configuration error).
    """
    try:
        backtest_report = asyncio.run(execute_backtest(args))
        write_output(backtest_report.model_dump_json(indent=2) + "\n", args.output)
    except HoldoutAlreadyUsedError as exc:
        print(f"Holdout refused: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        logger.error("Configuration or output error", exc_info=True)
        print(f"Configuration/output error: {exc}", file=sys.stderr)
        return 2
    print(describe_metrics("In-sample", backtest_report.in_sample), file=sys.stderr)
    if backtest_report.out_of_sample is not None:
        print(describe_metrics("Out-of-sample", backtest_report.out_of_sample), file=sys.stderr)
    print(f"Out-of-sample: {backtest_report.out_of_sample_note}", file=sys.stderr)
    return 0


def run_desk_command(args) -> int:
    """Dispatch a `desk` subcommand. For `decide` and `scan` it runs the desk, writes the report or
    reports as JSON, and prints one summary line per symbol. Returns the exit code.
    """
    print(f"Data={args.source}; desk={args.desk_command}. Paper trading only.", file=sys.stderr)
    if args.desk_command == "backtest":
        return run_backtest_command(args)
    if args.desk_command == "serve":
        return run_serve_command(args)
    try:
        desk_reports = asyncio.run(execute_desk(args))
    except (ValueError, OSError) as exc:
        logger.error("Configuration or output error", exc_info=True)
        print(f"Configuration/output error: {exc}", file=sys.stderr)
        return 2
    if args.desk_command == "decide":
        content = desk_reports[0].model_dump_json(indent=2) + "\n"
    else:
        serialised_reports = [desk_report.model_dump(mode="json") for desk_report in desk_reports]
        content = json.dumps(serialised_reports, indent=2) + "\n"
    try:
        write_output(content, args.output)
    except OSError:
        print("Could not write report output", file=sys.stderr)
        return 2
    for desk_report in desk_reports:
        score = desk_report.scout.score
        print(
            f"{desk_report.symbol}: scout={desk_report.scout.verdict}"
            f"{'' if score is None else f' ({score:.2f})'}; "
            f"risk={desk_report.risk.verdict}; decision={desk_report.decision.action}",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    """Program entry point: load `.env`, parse the arguments, run the chosen command and return its
    exit code. Exit 0 means a valid report was produced, including an abstention; exit 2 means a
    configuration or output error.
    """
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()
    resolve_automatic_sources(args)
    if args.source != "fixture" and args.scenario != "trend":
        parser.error("--scenario applies only to fixture data")
    is_server = args.command == "desk" and args.desk_command == "serve"
    log_level = args.log_level or ("DEBUG" if args.verbose else "INFO" if is_server else "WARNING")
    log_file = args.log_file or (DEFAULT_SERVER_LOG_FILE if is_server else None)
    try:
        configure_logging(log_level, log_file)
    except OSError as exc:
        print(f"Configuration/output error: cannot write the log file: {exc}", file=sys.stderr)
        return 2
    logger.info(
        "Command started command=%s source=%s log_level=%s log_file=%s",
        " ".join(filter(None, [args.command, getattr(args, "desk_command", None)])),
        args.source,
        log_level,
        log_file,
    )
    if args.command == "desk":
        return run_desk_command(args)
    print(f"Data={args.source}; reasoning={args.mode}. Research only.", file=sys.stderr)
    try:
        report = asyncio.run(execute(args))
    except (ValueError, OSError) as exc:
        logger.error("Configuration or output error", exc_info=True)
        print(f"Configuration/output error: {exc}", file=sys.stderr)
        return 2
    content = report.model_dump_json(indent=2) + "\n"
    try:
        write_output(content, args.output)
        if args.markdown:
            args.markdown.write_text(markdown(report))
    except OSError:
        print("Could not write report output", file=sys.stderr)
        return 2
    print(f"Classification={report.classification}; abstained={report.abstained}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
