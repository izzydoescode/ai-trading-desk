"""Historical daily-bar sources for replay and backtests.

Every source returns the same validated `MarketData` contract as the adapters in
`market_data.py`. Failures are reported as errors on the result; no source ever
substitutes synthetic candles.
"""

import asyncio
import csv
import io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from trading_desk.models import Candle, MarketData
from trading_desk.tools.market_data import MarketDataProvider

REQUIRED_CSV_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def parse_daily_bar_csv(csv_text: str) -> list[Candle]:
    """Parse `Date,Open,High,Low,Close,Volume` rows into chronological candles.

    Column names are case-insensitive and extra columns are ignored. Dates become
    midnight-UTC session labels, matching the Alpha Vantage adapter. Any malformed
    row raises, so a damaged file fails closed instead of losing rows silently.
    """
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    column_names = {name.strip().lower(): name for name in reader.fieldnames or []}
    missing_columns = [column for column in REQUIRED_CSV_COLUMNS if column not in column_names]
    if missing_columns:
        raise ValueError(f"CSV is missing columns: {', '.join(missing_columns)}")
    candles = [
        Candle(
            timestamp=datetime.fromisoformat(row[column_names["date"]].strip()).replace(tzinfo=UTC),
            open=float(row[column_names["open"]]),
            high=float(row[column_names["high"]]),
            low=float(row[column_names["low"]]),
            close=float(row[column_names["close"]]),
            volume=int(float(row[column_names["volume"]])),
        )
        for row in reader
    ]
    return sorted(candles, key=lambda candle: candle.timestamp)


class CsvDirectoryProvider:
    """Reads `<SYMBOL>.csv` from a local folder, e.g. licensed or exported history."""

    name = "local-csv-daily"

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    async def fetch(self, symbol: str) -> MarketData:
        csv_path = self.directory / f"{symbol}.csv"
        # A symbol must never select a file outside the configured folder.
        if csv_path.resolve().parent != self.directory.resolve() or not csv_path.is_file():
            return MarketData(
                symbol=symbol, source=self.name, errors=[f"No local CSV history for {symbol}"]
            )
        try:
            candles = parse_daily_bar_csv(csv_path.read_text())
        except (ValueError, KeyError, TypeError) as error:
            return MarketData(
                symbol=symbol,
                source=self.name,
                errors=[f"Local CSV history is malformed: {type(error).__name__}"],
            )
        return MarketData(symbol=symbol, source=self.name, candles=candles)


class YahooChartProvider:
    """Daily bars from Yahoo Finance's public chart endpoint; needs no API key.

    The endpoint is unofficial and unversioned: treat it as a research convenience,
    not a licensed feed. Prices are split-adjusted but not dividend-adjusted, and the
    latest bar may be an unfinished session. Each bar's timestamp is its session open.
    """

    name = "yahoo-chart-daily"
    CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    PRICE_DECIMAL_PLACES = 4

    def __init__(self, client: httpx.AsyncClient, history_range: str = "2y"):
        self.client, self.history_range = client, history_range

    async def fetch(self, symbol: str) -> MarketData:
        for attempt in range(3):
            try:
                response = await self.client.get(
                    self.CHART_URL.format(symbol=symbol),
                    params={"range": self.history_range, "interval": "1d"},
                    # The endpoint rejects requests without a browser-style agent string.
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=20,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                if response.status_code == 404:
                    return self._no_series(symbol)
                response.raise_for_status()
                chart_results = (response.json().get("chart") or {}).get("result")
                if not chart_results or not chart_results[0].get("timestamp"):
                    return self._no_series(symbol)
                return MarketData(
                    symbol=symbol, source=self.name, candles=self._candles(chart_results[0])
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        raise RuntimeError("market data retry budget exhausted")

    def _no_series(self, symbol: str) -> MarketData:
        return MarketData(
            symbol=symbol,
            source=self.name,
            errors=["Provider returned no daily series (unknown symbol or no data)"],
        )

    def _candles(self, chart_result: dict) -> list[Candle]:
        quote = chart_result["indicators"]["quote"][0]
        candles = []
        for index, epoch_seconds in enumerate(chart_result["timestamp"]):
            prices = [quote[field][index] for field in ("open", "high", "low", "close")]
            if any(price is None for price in prices):
                continue  # The feed marks sessions without trades (halts, holidays) as null.
            open_price, high_price, low_price, close_price = (
                round(price, self.PRICE_DECIMAL_PLACES) for price in prices
            )
            candles.append(
                Candle(
                    timestamp=datetime.fromtimestamp(epoch_seconds, UTC),
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=quote["volume"][index] or 0,
                )
            )
        return candles


class CachingProvider:
    """Stores another provider's successful results on disk and reuses them while fresh.

    Keeps demos and backtests off the network and inside vendor rate limits. Only
    error-free results with candles are stored, so a failure is always retried.
    """

    def __init__(
        self,
        wrapped_provider: MarketDataProvider,
        cache_directory: Path,
        maximum_cache_age: timedelta = timedelta(hours=12),
    ):
        self.wrapped_provider = wrapped_provider
        self.name = wrapped_provider.name
        self.cache_directory = Path(cache_directory) / wrapped_provider.name
        self.maximum_cache_age = maximum_cache_age

    async def fetch(self, symbol: str) -> MarketData:
        cache_path = self.cache_directory / f"{symbol}.json"
        cached_market_data = self._read(cache_path, symbol)
        if cached_market_data is not None:
            return cached_market_data
        market_data = await self.wrapped_provider.fetch(symbol)
        if market_data.candles and not market_data.errors:
            self.cache_directory.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(market_data.model_dump_json())
        return market_data

    def _read(self, cache_path: Path, symbol: str) -> MarketData | None:
        if (
            cache_path.resolve().parent != self.cache_directory.resolve()
            or not cache_path.is_file()
        ):
            return None
        try:
            market_data = MarketData.model_validate_json(cache_path.read_text())
        except ValueError:
            return None  # A corrupt cache entry is ignored and refetched.
        is_fresh = datetime.now(UTC) - market_data.retrieved_at <= self.maximum_cache_age
        return market_data if is_fresh and market_data.symbol == symbol else None


class FallbackProvider:
    """Tries providers in order and returns the first usable result.

    A provider that raises or returns errors is skipped and its failure recorded.
    If none succeed, the combined errors are returned; nothing is ever invented.
    """

    def __init__(self, providers: Sequence[MarketDataProvider]):
        if not providers:
            raise ValueError("at least one market data provider is required")
        self.providers = tuple(providers)
        self.name = "fallback(" + ",".join(provider.name for provider in self.providers) + ")"

    async def fetch(self, symbol: str) -> MarketData:
        failures: list[str] = []
        for provider in self.providers:
            try:
                market_data = await provider.fetch(symbol)
            except Exception as error:  # noqa: BLE001 - any provider failure moves to the next
                # Exception text can carry credential-bearing URLs; keep only the class.
                failures.append(f"{provider.name}: {type(error).__name__}")
                continue
            if market_data.candles and not market_data.errors:
                return market_data
            failures.extend(
                f"{provider.name}: {message}"
                for message in market_data.errors or ["returned no candles"]
            )
        return MarketData(symbol=symbol, source=self.name, errors=failures)


class PreloadedProvider:
    """Serves history that was fetched once up front, for replay and backtests.

    A backtest asks for the same symbols thousands of times; this keeps every
    request in memory and off the network. Symbols that failed to load keep
    their original errors.
    """

    def __init__(self, name: str, history_by_symbol: dict[str, MarketData]):
        self.name = name
        self.history_by_symbol = dict(history_by_symbol)

    async def fetch(self, symbol: str) -> MarketData:
        market_data = self.history_by_symbol.get(symbol)
        if market_data is None:
            return MarketData(
                symbol=symbol, source=self.name, errors=[f"No preloaded history for {symbol}"]
            )
        return market_data


async def preload_history(
    data_provider: MarketDataProvider, symbols: Sequence[str]
) -> PreloadedProvider:
    """Fetch each symbol once. A provider exception is recorded by class name only."""
    history_by_symbol: dict[str, MarketData] = {}
    for symbol in symbols:
        try:
            history_by_symbol[symbol] = await data_provider.fetch(symbol)
        except Exception as error:  # noqa: BLE001 - one bad symbol must not stop the run
            history_by_symbol[symbol] = MarketData(
                symbol=symbol,
                source=data_provider.name,
                errors=[f"Market data retrieval failed: {type(error).__name__}"],
            )
    return PreloadedProvider(data_provider.name, history_by_symbol)
