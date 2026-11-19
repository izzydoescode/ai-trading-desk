"""Alpaca bars, prices and news, driven through a mock transport. Nothing touches the network."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trading_desk.tools.alpaca import (
    AlpacaBarsProvider,
    AlpacaClient,
    AlpacaNewsFeed,
    parse_alpaca_news,
    parse_daily_bars,
    session_close,
)
from trading_desk.tools.news import FallbackNewsFeed

KEY_ID, SECRET_KEY = "PKTESTKEYID0000000001", "test-secret-key-000000000000000000000001"
# Tuesday 17 November 2026, 15:00 UTC: 10:00 in New York, the session is open.
NOW = datetime(2026, 11, 17, 15, tzinfo=UTC)


def make_bar(session_date, close=100.0, volume=1_000_000):
    """Test helper (not a test): builds an Alpaca bar stamped, as Alpaca does, at midnight
    New York time on its session date.
    """
    return {
        "t": f"{session_date}T05:00:00Z",
        "o": close - 1,
        "h": close + 2,
        "l": close - 2,
        "c": close,
        "v": volume,
        "n": 100,
        "vw": close,
    }


def make_alpaca(handler, clock_time=NOW, **provider_options):
    """Test helper (not a test): builds a bars provider and its client over a mock transport,
    with retries that do not wait.
    """
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    alpaca_client = AlpacaClient(KEY_ID, SECRET_KEY, http_client, sleep=record_wait)
    provider = AlpacaBarsProvider(alpaca_client, clock=lambda: clock_time, **provider_options)
    return provider, alpaca_client, waits


def test_a_bar_is_stamped_with_its_session_close_not_its_start():
    """Verifies: Alpaca's midnight stamp becomes 16:00 New York time, in winter and in summer,
    so a bar's timestamp is the moment its values were final.
    """
    assert session_close(datetime(2026, 11, 16).date()) == datetime(2026, 11, 16, 21, tzinfo=UTC)
    assert session_close(datetime(2026, 7, 15).date()) == datetime(2026, 7, 15, 20, tzinfo=UTC)
    winter_bar, summer_bar = parse_daily_bars(
        [make_bar("2026-11-16"), {**make_bar("2026-07-15"), "t": "2026-07-15T04:00:00Z"}]
    )
    assert winter_bar.timestamp == datetime(2026, 11, 16, 21, tzinfo=UTC)
    assert summer_bar.timestamp == datetime(2026, 7, 15, 20, tzinfo=UTC)
    assert (winter_bar.open, winter_bar.high, winter_bar.low, winter_bar.close) == (
        99.0,
        102.0,
        98.0,
        100.0,
    )


def test_unreadable_bars_are_skipped_not_guessed():
    """Verifies: a bar missing a price or with a bad date is dropped, and a payload that is
    not a list is an error.
    """
    good = make_bar("2026-11-16")
    candles = parse_daily_bars(
        [good, {**good, "t": "not a date"}, {"t": "2026-11-13T05:00:00Z"}, "junk", None]
    )
    assert len(candles) == 1
    with pytest.raises(ValueError, match="unexpected bars payload"):
        parse_daily_bars({"error": "forbidden"})


async def test_fetch_sends_credentials_in_headers_and_asks_for_adjusted_daily_bars():
    """Verifies: the request authenticates by header only and asks for adjusted daily bars
    from the IEX feed over the configured history window.
    """
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"bars": {"AAPL": [make_bar("2026-11-13")]}, "next_page_token": None}
        )

    provider, _, _ = make_alpaca(handler, history_days=400)
    market_data = await provider.fetch("AAPL")
    (request,) = requests
    assert request.headers["APCA-API-KEY-ID"] == KEY_ID
    assert request.headers["APCA-API-SECRET-KEY"] == SECRET_KEY
    assert KEY_ID not in str(request.url) and SECRET_KEY not in str(request.url)
    assert dict(request.url.params) == {
        "symbols": "AAPL",
        "timeframe": "1Day",
        "start": "2025-10-13",
        "limit": "10000",
        "adjustment": "all",
        "feed": "iex",
    }
    assert market_data.source == "alpaca-daily-iex"
    assert market_data.errors == []
    assert len(market_data.candles) == 1


async def test_todays_unfinished_session_is_never_returned():
    """Verifies: while the market is open, today's forming bar is dropped; once the close has
    passed, the same bar is included.
    """

    def handler(request):
        return httpx.Response(
            200,
            json={"bars": {"AAPL": [make_bar("2026-11-16"), make_bar("2026-11-17", 105.0)]}},
        )

    during_session, _, _ = make_alpaca(handler)
    candles = (await during_session.fetch("AAPL")).candles
    assert [candle.timestamp.date().isoformat() for candle in candles] == ["2026-11-16"]

    after_close, _, _ = make_alpaca(handler, clock_time=datetime(2026, 11, 17, 21, 1, tzinfo=UTC))
    candles = (await after_close.fetch("AAPL")).candles
    assert [candle.close for candle in candles] == [100.0, 105.0]


async def test_fetch_follows_pages_until_the_history_is_complete():
    """Verifies: a long history split across pages is stitched together in order."""
    pages = {
        None: {"bars": {"AAPL": [make_bar("2026-11-12")]}, "next_page_token": "page-2"},
        "page-2": {"bars": {"AAPL": [make_bar("2026-11-13")]}, "next_page_token": "page-3"},
        "page-3": {"bars": {"AAPL": [make_bar("2026-11-16")]}, "next_page_token": None},
    }
    tokens_seen = []

    def handler(request):
        token = request.url.params.get("page_token")
        tokens_seen.append(token)
        return httpx.Response(200, json=pages[token])

    provider, _, _ = make_alpaca(handler)
    candles = (await provider.fetch("AAPL")).candles
    assert tokens_seen == [None, "page-2", "page-3"]
    assert [candle.timestamp.day for candle in candles] == [12, 13, 16]


async def test_an_unknown_symbol_is_reported_as_no_data_not_as_a_crash():
    """Verifies: Alpaca answers an unknown symbol with an empty series; the desk reports that
    as a data error so the quality gate abstains.
    """

    def handler(request):
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    provider, _, _ = make_alpaca(handler)
    market_data = await provider.fetch("ZZZZ")
    assert market_data.candles == []
    assert market_data.errors == ["Provider returned no daily series (unknown symbol or no data)"]


@pytest.mark.parametrize("failure_status", [429, 500, 503])
async def test_rate_limits_and_server_errors_are_retried(failure_status):
    """Verifies: a 429 or 5xx is retried with backoff and the third attempt's answer is used."""
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(failure_status, json={"message": "try later"})
        return httpx.Response(200, json={"bars": {"AAPL": [make_bar("2026-11-16")]}})

    provider, _, waits = make_alpaca(handler)
    assert len((await provider.fetch("AAPL")).candles) == 1
    assert len(attempts) == 3
    assert waits == [0.5, 1.0]


async def test_rejected_credentials_fail_at_once_without_retrying():
    """Verifies: a 401 is not retried; it raises so the caller records the failure."""
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(401, json={"message": "unauthorized"})

    provider, _, _ = make_alpaca(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.fetch("AAPL")
    assert len(attempts) == 1


async def test_transport_errors_are_retried_then_raised():
    """Verifies: a network failure is tried three times and then raised."""
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("no route")

    provider, _, waits = make_alpaca(handler)
    with pytest.raises(httpx.ConnectError):
        await provider.fetch("AAPL")
    assert len(attempts) == 3 and waits == [0.5, 1.0]


async def test_latest_prices_come_from_the_last_trade_and_missing_ones_are_left_out():
    """Verifies: live prices are read from each symbol's latest trade, and a symbol without
    one gets no price rather than a zero.
    """
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "AAPL": {"latestTrade": {"p": 352.66}},
                "MSFT": {"latestTrade": {"p": 0}},
                "NVDA": {"dailyBar": {"c": 190.0}},
            },
        )

    provider, _, _ = make_alpaca(handler)
    assert await provider.latest_prices(["AAPL", "MSFT", "NVDA", "TSLA"]) == {"AAPL": 352.66}
    assert requests[0].url.params["symbols"] == "AAPL,MSFT,NVDA,TSLA"
    assert await provider.latest_prices([]) == {}
    assert len(requests) == 1


def make_news_entry(created_at, headline="Apple ships a thing", source="benzinga", **overrides):
    """Test helper (not a test): builds one Alpaca news object."""
    return {
        "id": 1,
        "headline": headline,
        "author": "",
        "created_at": created_at,
        "updated_at": created_at,
        "summary": "",
        "url": "https://example.com/story",
        "symbols": ["AAPL"],
        "source": source,
        **overrides,
    }


def test_news_entries_without_a_headline_source_or_time_are_dropped():
    """Verifies: only usable, dated, sourced items become news; nothing is filled in, and
    opinion outlets are marked as commentary.
    """
    good = make_news_entry("2026-11-12T14:00:00Z")
    news_items = parse_alpaca_news(
        [
            good,
            "junk",
            {**good, "headline": "  "},
            {**good, "source": None},
            {**good, "created_at": "yesterday"},
            {**good, "created_at": "2026-11-12T14:00:00"},
            {**good, "headline": "Opinion piece", "source": "SeekingAlpha"},
            {**good, "headline": "Plain link", "url": "http://insecure.example"},
        ]
    )
    assert [(item.headline, item.is_commentary, item.url) for item in news_items] == [
        ("Apple ships a thing", False, "https://example.com/story"),
        ("Opinion piece", True, "https://example.com/story"),
        ("Plain link", False, None),
    ]
    assert news_items[0].published_at == datetime(2026, 11, 12, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="unexpected news payload"):
        parse_alpaca_news({"message": "forbidden"})


def make_news_feed(handler, cache_directory=None, clock_time=NOW):
    """Test helper (not a test): builds an Alpaca news feed over a mock transport."""

    async def no_wait(seconds):
        return None

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    alpaca_client = AlpacaClient(KEY_ID, SECRET_KEY, http_client, sleep=no_wait)
    return AlpacaNewsFeed(
        alpaca_client,
        cache_directory,
        minimum_request_interval_seconds=0,
        sleep=no_wait,
        clock=lambda: clock_time,
    )


async def test_news_is_point_in_time_paged_and_cached_once_the_week_is_over(tmp_path):
    """Verifies: the feed asks for the calendar week, follows pages, never returns an item
    published after the window's end, and serves a finished week from disk afterwards.
    """
    as_of = datetime(2026, 11, 5, 21, tzinfo=UTC)  # a Thursday in a finished week
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params.get("page_token") == "next":
            return httpx.Response(
                200,
                json={"news": [make_news_entry("2026-11-03T12:00:00Z", "Older item")]},
            )
        return httpx.Response(
            200,
            json={
                "news": [
                    make_news_entry("2026-11-06T12:00:00Z", "Published after the window"),
                    make_news_entry("2026-11-05T15:00:00Z", "Same day item"),
                ],
                "next_page_token": "next",
            },
        )

    feed = make_news_feed(handler, tmp_path)
    news_items = await feed.fetch("AAPL", as_of - timedelta(days=3), as_of)
    assert [item.headline for item in news_items] == ["Same day item", "Older item"]
    first_request = requests[0]
    assert first_request.headers["APCA-API-KEY-ID"] == KEY_ID
    assert first_request.url.params["start"] == "2026-11-02T00:00:00Z"
    assert first_request.url.params["end"] == "2026-11-08T23:59:59Z"
    assert first_request.url.params["sort"] == "desc"
    assert len(requests) == 2
    cached = json.loads((tmp_path / "alpaca" / "AAPL" / "2026-11-02.json").read_text())
    assert len(cached) == 3

    second_feed = make_news_feed(handler, tmp_path)
    await second_feed.fetch("AAPL", as_of - timedelta(days=3), as_of)
    assert len(requests) == 2  # served from the disk cache


class ScriptedNewsFeed:
    """Test double (not a test): a news feed that returns fixed items or raises."""

    def __init__(self, name, headlines=(), error=None):
        self.name, self.headlines, self.error, self.call_count = name, headlines, error, 0

    async def fetch(self, symbol, start, end, limit=12):
        from trading_desk.models import NewsItem

        self.call_count += 1
        if self.error is not None:
            raise self.error
        return [
            NewsItem(published_at=end, source=self.name, headline=headline)
            for headline in self.headlines
        ]


async def test_the_backup_feed_is_used_only_when_the_primary_fails():
    """Verifies: Finnhub stands in when Alpaca raises, an empty answer from Alpaca is
    respected as an answer, and a failure of both is raised to the caller.
    """
    window = (NOW - timedelta(days=7), NOW)
    working_primary = FallbackNewsFeed(
        ScriptedNewsFeed("alpaca-news", ["From Alpaca"]), backup := ScriptedNewsFeed("finnhub")
    )
    assert [item.headline for item in await working_primary.fetch("AAPL", *window)] == [
        "From Alpaca"
    ]
    assert backup.call_count == 0
    assert working_primary.name == "alpaca-news (backup finnhub)"

    quiet_primary = FallbackNewsFeed(
        ScriptedNewsFeed("alpaca-news", []), backup := ScriptedNewsFeed("finnhub", ["Unused"])
    )
    assert await quiet_primary.fetch("AAPL", *window) == []
    assert backup.call_count == 0

    failing_primary = FallbackNewsFeed(
        ScriptedNewsFeed("alpaca-news", error=RuntimeError("503")),
        ScriptedNewsFeed("finnhub", ["From Finnhub"]),
    )
    assert [item.headline for item in await failing_primary.fetch("AAPL", *window)] == [
        "From Finnhub"
    ]

    both_down = FallbackNewsFeed(
        ScriptedNewsFeed("alpaca-news", error=RuntimeError("503")),
        ScriptedNewsFeed("finnhub", error=ConnectionError("down")),
    )
    with pytest.raises(ConnectionError):
        await both_down.fetch("AAPL", *window)


def make_minute_bar(start_time, low, high):
    """Test helper (not a test): builds one Alpaca minute bar."""
    return {"t": start_time, "o": low, "h": high, "l": low, "c": high, "v": 500, "n": 5, "vw": low}


def make_intraday_bars(handler, cache_directory=None):
    """Test helper (not a test): builds the minute-bar reader over a mock transport."""
    from trading_desk.tools.alpaca import AlpacaIntradayBars

    async def no_wait(seconds):
        return None

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    alpaca_client = AlpacaClient(KEY_ID, SECRET_KEY, http_client, sleep=no_wait)
    return AlpacaIntradayBars(alpaca_client, cache_directory)


async def test_minute_bars_cover_one_regular_session_and_are_kept_on_disk(tmp_path):
    """Verifies: the request asks for one-minute adjusted IEX bars from 09:30 to 16:00 New York
    time on the daily bar's date, pages are followed, bars come back oldest first, and a
    second request for the same session is served from disk without calling Alpaca.
    """
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params.get("page_token") == "page-2":
            return httpx.Response(
                200, json={"bars": {"AAPL": [make_minute_bar("2026-11-16T14:30:00Z", 100, 101)]}}
            )
        return httpx.Response(
            200,
            json={
                "bars": {
                    "AAPL": [
                        make_minute_bar("2026-11-16T14:32:00Z", 102, 103),
                        make_minute_bar("2026-11-16T14:31:00Z", 101, 102),
                        # The minute starting at the close is after-hours and is left out.
                        make_minute_bar("2026-11-16T21:00:00Z", 500, 501),
                        {"t": "broken"},
                    ]
                },
                "next_page_token": "page-2",
            },
        )

    daily_bar_time = datetime(2026, 11, 16, 21, tzinfo=UTC)
    intraday_bars = make_intraday_bars(handler, tmp_path)
    candles = await intraday_bars.fetch_session("AAPL", daily_bar_time)
    assert [candle.timestamp.minute for candle in candles] == [30, 31, 32]
    assert [candle.high for candle in candles] == [101.0, 102.0, 103.0]
    first_request = requests[0]
    assert first_request.headers["APCA-API-KEY-ID"] == KEY_ID
    assert dict(first_request.url.params) == {
        "symbols": "AAPL",
        "timeframe": "1Min",
        "start": "2026-11-16T14:30:00Z",
        "end": "2026-11-16T21:00:00Z",
        "limit": "10000",
        "adjustment": "all",
        "feed": "iex",
    }
    assert len(requests) == 2
    assert (tmp_path / "alpaca-1min-iex" / "AAPL" / "2026-11-16.json").is_file()

    again = await make_intraday_bars(handler, tmp_path).fetch_session("AAPL", daily_bar_time)
    assert again == candles
    assert len(requests) == 2


async def test_minute_bars_without_a_cache_are_fetched_each_time_and_failures_raise():
    """Verifies: with no cache folder every call goes to Alpaca, a summer session uses the
    summer clock (13:30 UTC open), an empty answer is an empty list, a rejected request
    raises for the caller to handle, and a payload that is not a list is refused.
    """
    from trading_desk.tools.alpaca import parse_intraday_bars

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"bars": {}})

    intraday_bars = make_intraday_bars(handler)
    summer_close = datetime(2026, 7, 15, 20, tzinfo=UTC)
    assert await intraday_bars.fetch_session("AAPL", summer_close) == []
    assert await intraday_bars.fetch_session("AAPL", summer_close) == []
    assert len(requests) == 2
    assert requests[0].url.params["start"] == "2026-07-15T13:30:00Z"
    assert requests[0].url.params["end"] == "2026-07-15T20:00:00Z"

    rejected = make_intraday_bars(lambda request: httpx.Response(403, json={"message": "no"}))
    with pytest.raises(httpx.HTTPStatusError):
        await rejected.fetch_session("AAPL", summer_close)
    with pytest.raises(ValueError, match="unexpected bars payload"):
        parse_intraday_bars({"message": "forbidden"})
