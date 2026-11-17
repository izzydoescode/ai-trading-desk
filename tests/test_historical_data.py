from datetime import UTC, datetime

import pytest

from trading_desk.tools.historical_data import CsvDirectoryProvider, parse_daily_bar_csv

VALID_CSV = """Date,Open,High,Low,Close,Volume
2025-01-03,101,103,100,102,1500
2025-01-02,100,102,99,101,1000.0
"""


def test_csv_rows_become_chronological_utc_candles():
    candles = parse_daily_bar_csv(VALID_CSV)
    assert [candle.timestamp for candle in candles] == [
        datetime(2025, 1, 2, tzinfo=UTC),
        datetime(2025, 1, 3, tzinfo=UTC),
    ]
    assert candles[0].close == 101
    assert candles[0].volume == 1000


def test_csv_header_is_case_insensitive_and_ignores_extra_columns():
    csv_text = "date,OPEN,High,low,Close,Adj Close,VOLUME\n2025-01-02,100,102,99,101,100.5,10\n"
    assert parse_daily_bar_csv(csv_text)[0].high == 102


@pytest.mark.parametrize(
    "malformed_csv",
    [
        "Date,Open,High,Low,Close\n2025-01-02,100,102,99,101\n",
        "Date,Open,High,Low,Close,Volume\n2025-01-02,100,102,99,not-a-number,10\n",
        "Date,Open,High,Low,Close,Volume\n2025-01-02,100,90,99,101,10\n",
        "Date,Open,High,Low,Close,Volume\nnot-a-date,100,102,99,101,10\n",
        "",
    ],
)
def test_malformed_csv_fails_closed(malformed_csv):
    with pytest.raises(ValueError):
        parse_daily_bar_csv(malformed_csv)


async def test_directory_provider_reads_the_symbol_file(tmp_path):
    (tmp_path / "AAPL.csv").write_text(VALID_CSV)
    market_data = await CsvDirectoryProvider(tmp_path).fetch("AAPL")
    assert market_data.source == "local-csv-daily"
    assert not market_data.synthetic
    assert not market_data.errors
    assert len(market_data.candles) == 2


async def test_directory_provider_reports_missing_history_without_inventing_candles(tmp_path):
    market_data = await CsvDirectoryProvider(tmp_path).fetch("MSFT")
    assert market_data.candles == []
    assert market_data.errors == ["No local CSV history for MSFT"]


async def test_directory_provider_reports_malformed_file_by_error_class(tmp_path):
    (tmp_path / "AAPL.csv").write_text("Date,Open\n2025-01-02,100\n")
    market_data = await CsvDirectoryProvider(tmp_path).fetch("AAPL")
    assert market_data.candles == []
    assert market_data.errors == ["Local CSV history is malformed: ValueError"]


async def test_directory_provider_never_reads_outside_its_folder(tmp_path):
    history_directory = tmp_path / "history"
    history_directory.mkdir()
    (tmp_path / "SECRET.csv").write_text(VALID_CSV)
    market_data = await CsvDirectoryProvider(history_directory).fetch("../SECRET")
    assert market_data.candles == []
    assert market_data.errors
