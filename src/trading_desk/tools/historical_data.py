"""Historical daily-bar sources for replay and backtests.

Every source returns the same validated `MarketData` contract as the adapters in
`market_data.py`. Failures are reported as errors on the result; no source ever
substitutes synthetic candles.
"""

import asyncio
import csv
import io
from datetime import UTC, datetime
from pathlib import Path

import httpx

from trading_desk.models import Candle, MarketData

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
