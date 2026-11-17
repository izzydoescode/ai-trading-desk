from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trading_desk.models import Candle, MarketData
from trading_desk.tools.historical_data import (
    CachingProvider,
    CsvDirectoryProvider,
    FallbackProvider,
    PreloadedProvider,
    YahooChartProvider,
    parse_daily_bar_csv,
    preload_history,
)

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


def chart_payload(timestamps, opens, highs, lows, closes, volumes):
    quote = {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    return {
        "chart": {
            "result": [{"meta": {}, "timestamp": timestamps, "indicators": {"quote": [quote]}}],
            "error": None,
        }
    }


async def fetch_with_handler(handler, symbol="AAPL"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await YahooChartProvider(client, history_range="1mo").fetch(symbol)


async def test_yahoo_request_shape_and_candle_parsing():
    def handler(request):
        assert request.url.path == "/v8/finance/chart/AAPL"
        assert request.url.params["interval"] == "1d"
        assert request.url.params["range"] == "1mo"
        assert request.headers["User-Agent"]
        return httpx.Response(
            200,
            json=chart_payload(
                [1794321000, 1794407400],
                [351.3500061, 349.5499878],
                [353.8699951, 355.9100036],
                [346.4599914, 349.3299865],
                [348.5100097, 355.6300048],
                [29756100, None],
            ),
        )

    market_data = await fetch_with_handler(handler)
    assert market_data.source == "yahoo-chart-daily"
    assert not market_data.errors
    assert [candle.close for candle in market_data.candles] == [348.51, 355.63]
    assert market_data.candles[0].timestamp == datetime.fromtimestamp(1794321000, UTC)
    assert market_data.candles[1].volume == 0


async def test_yahoo_skips_sessions_with_null_prices():
    payload = chart_payload(
        [1794321000, 1794407400, 1794493800],
        [10.0, None, 11.0],
        [11.0, None, 12.0],
        [9.0, None, 10.0],
        [10.5, None, 11.5],
        [100, None, 200],
    )
    market_data = await fetch_with_handler(lambda request: httpx.Response(200, json=payload))
    assert [candle.close for candle in market_data.candles] == [10.5, 11.5]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}}),
        httpx.Response(200, json={"chart": {"result": None, "error": None}}),
        httpx.Response(200, json={"chart": {"result": [{"meta": {}}]}}),
    ],
)
async def test_yahoo_reports_missing_series_as_an_error(response):
    market_data = await fetch_with_handler(lambda request: response)
    assert market_data.candles == []
    assert market_data.errors == ["Provider returned no daily series (unknown symbol or no data)"]


async def test_yahoo_retries_throttling_then_succeeds(monkeypatch):
    monkeypatch.setattr("trading_desk.tools.historical_data.asyncio.sleep", no_sleep)
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=chart_payload([1794321000], [1], [2], [1], [2], [5]))

    market_data = await fetch_with_handler(handler)
    assert len(attempts) == 3
    assert len(market_data.candles) == 1


async def test_yahoo_gives_up_after_three_server_errors(monkeypatch):
    monkeypatch.setattr("trading_desk.tools.historical_data.asyncio.sleep", no_sleep)
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_with_handler(handler)
    assert len(attempts) == 3


async def test_yahoo_does_not_retry_client_errors():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(403)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_with_handler(handler)
    assert len(attempts) == 1


async def test_yahoo_retries_transport_errors_then_raises(monkeypatch):
    monkeypatch.setattr("trading_desk.tools.historical_data.asyncio.sleep", no_sleep)
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("network down")

    with pytest.raises(httpx.ConnectError):
        await fetch_with_handler(handler)
    assert len(attempts) == 3


async def test_yahoo_rejects_incoherent_bars_instead_of_dropping_them():
    payload = chart_payload([1794321000], [10.0], [9.0], [11.0], [10.0], [1])
    with pytest.raises(ValueError):
        await fetch_with_handler(lambda request: httpx.Response(200, json=payload))


async def no_sleep(seconds):
    return None


def one_candle_market_data(symbol="AAPL", source="counting", retrieved_at=None):
    candle = Candle(
        timestamp=datetime(2025, 1, 2, tzinfo=UTC), open=1, high=2, low=1, close=2, volume=5
    )
    extra = {"retrieved_at": retrieved_at} if retrieved_at else {}
    return MarketData(symbol=symbol, source=source, candles=[candle], **extra)


class CountingProvider:
    """Fake provider that records how often it is asked and replays scripted results."""

    def __init__(self, name="counting", results=None):
        self.name = name
        self.fetch_count = 0
        self.results = list(results or [])

    async def fetch(self, symbol):
        self.fetch_count += 1
        result = self.results.pop(0) if self.results else one_candle_market_data(symbol, self.name)
        if isinstance(result, Exception):
            raise result
        return result


async def test_cache_serves_second_request_without_calling_the_provider(tmp_path):
    wrapped_provider = CountingProvider()
    caching_provider = CachingProvider(wrapped_provider, tmp_path)
    first_result = await caching_provider.fetch("AAPL")
    second_result = await caching_provider.fetch("AAPL")
    assert wrapped_provider.fetch_count == 1
    assert second_result == first_result
    assert caching_provider.name == "counting"
    assert (tmp_path / "counting" / "AAPL.json").is_file()


async def test_cache_refetches_when_the_entry_is_too_old(tmp_path):
    stale_retrieval_time = datetime.now(UTC) - timedelta(days=2)
    wrapped_provider = CountingProvider(
        results=[one_candle_market_data(retrieved_at=stale_retrieval_time)]
    )
    caching_provider = CachingProvider(wrapped_provider, tmp_path, timedelta(hours=1))
    await caching_provider.fetch("AAPL")
    await caching_provider.fetch("AAPL")
    assert wrapped_provider.fetch_count == 2


async def test_cache_never_stores_failures(tmp_path):
    failed_result = MarketData(symbol="AAPL", source="counting", errors=["quota"])
    wrapped_provider = CountingProvider(results=[failed_result])
    caching_provider = CachingProvider(wrapped_provider, tmp_path)
    assert (await caching_provider.fetch("AAPL")).errors == ["quota"]
    assert (await caching_provider.fetch("AAPL")).candles
    assert wrapped_provider.fetch_count == 2


async def test_cache_ignores_corrupt_and_mismatched_entries(tmp_path):
    wrapped_provider = CountingProvider()
    caching_provider = CachingProvider(wrapped_provider, tmp_path)
    cache_folder = tmp_path / "counting"
    cache_folder.mkdir()
    (cache_folder / "AAPL.json").write_text("{not json")
    (cache_folder / "MSFT.json").write_text(one_candle_market_data("AAPL").model_dump_json())
    await caching_provider.fetch("AAPL")
    await caching_provider.fetch("MSFT")
    assert wrapped_provider.fetch_count == 2


async def test_fallback_returns_first_usable_result_and_skips_failures():
    raising_provider = CountingProvider("primary", [RuntimeError("https://host/?apikey=SECRET")])
    empty_provider = CountingProvider(
        "secondary", [MarketData(symbol="AAPL", source="secondary", errors=["quota"])]
    )
    working_provider = CountingProvider("tertiary")
    unused_provider = CountingProvider("unused")
    fallback_provider = FallbackProvider(
        [raising_provider, empty_provider, working_provider, unused_provider]
    )
    market_data = await fallback_provider.fetch("AAPL")
    assert market_data.source == "tertiary"
    assert unused_provider.fetch_count == 0


async def test_fallback_reports_every_failure_without_leaking_exception_text():
    raising_provider = CountingProvider("primary", [RuntimeError("https://host/?apikey=SECRET")])
    empty_provider = CountingProvider("secondary", [MarketData(symbol="AAPL", source="secondary")])
    market_data = await FallbackProvider([raising_provider, empty_provider]).fetch("AAPL")
    assert market_data.source == "fallback(primary,secondary)"
    assert market_data.candles == []
    assert market_data.errors == ["primary: RuntimeError", "secondary: returned no candles"]
    assert "SECRET" not in market_data.model_dump_json()


def test_fallback_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        FallbackProvider([])


async def test_preload_fetches_each_symbol_once_and_serves_from_memory():
    wrapped_provider = CountingProvider()
    preloaded_provider = await preload_history(wrapped_provider, ["AAPL", "MSFT"])
    assert wrapped_provider.fetch_count == 2
    for _ in range(3):
        assert (await preloaded_provider.fetch("AAPL")).symbol == "AAPL"
    assert wrapped_provider.fetch_count == 2
    assert preloaded_provider.name == "counting"


async def test_preload_keeps_failures_as_errors_without_leaking_exception_text():
    wrapped_provider = CountingProvider(results=[RuntimeError("apikey=SECRET")])
    preloaded_provider = await preload_history(wrapped_provider, ["AAPL", "MSFT"])
    failed_history = await preloaded_provider.fetch("AAPL")
    assert failed_history.errors == ["Market data retrieval failed: RuntimeError"]
    assert (await preloaded_provider.fetch("MSFT")).candles


async def test_preloaded_provider_reports_symbols_it_was_never_given():
    market_data = await PreloadedProvider("memory", {}).fetch("TSLA")
    assert market_data.candles == []
    assert market_data.errors == ["No preloaded history for TSLA"]
