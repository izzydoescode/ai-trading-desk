"""Historical daily-bar sources for replay and backtests.

Every source returns the same validated `MarketData` contract as the adapters in
`market_data.py`. Failures are reported as errors on the result; no source ever
substitutes synthetic candles.
"""

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

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
