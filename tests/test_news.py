"""The Finnhub news feed, driven through a mock transport. Nothing touches the network."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trading_desk.tools.news import (
    MAXIMUM_ITEMS_PER_FETCH,
    FinnhubNewsFeed,
    monday_of,
    parse_finnhub_items,
)

API_KEY = "test-finnhub-key"
NOW = datetime(2025, 6, 2, 12, tzinfo=UTC)  # a Monday, well after the weeks under test
WEDNESDAY = datetime(2025, 3, 12, 21, tzinfo=UTC)


def make_finnhub_entry(published_at, headline="Apple ships a thing", source="Reuters", **overrides):
    return {
        "category": "company",
        "datetime": int(published_at.timestamp()),
        "headline": headline,
        "id": 1,
        "image": "",
        "related": "AAPL",
        "source": source,
        "summary": "",
        "url": "https://finnhub.io/api/news?id=abc",
        **overrides,
    }


class FakeFinnhub:
    """Answers company-news requests from a list of entries, like the real endpoint:
    only entries inside the requested from/to dates are returned."""

    def __init__(self, entries, failures_before_success=0, failure_status=500):
        self.entries, self.requests = entries, []
        self.failures_left, self.failure_status = failures_before_success, failure_status

    def __call__(self, request):
        self.requests.append(request)
        if self.failures_left:
            self.failures_left -= 1
            return httpx.Response(self.failure_status, json={"error": "try later"})
        first_day = datetime.fromisoformat(request.url.params["from"]).replace(tzinfo=UTC)
        last_day = datetime.fromisoformat(request.url.params["to"]).replace(tzinfo=UTC)
        return httpx.Response(
            200,
            json=[
                entry
                for entry in self.entries
                if first_day.timestamp()
                <= entry["datetime"]
                < (last_day + timedelta(days=1)).timestamp()
            ],
        )


def make_feed(fake_finnhub, cache_directory=None, clock_time=NOW, **options):
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_finnhub))
    feed = FinnhubNewsFeed(
        API_KEY, client, cache_directory, sleep=record_wait, clock=lambda: clock_time, **options
    )
    return feed, waits


async def test_fetch_returns_only_what_was_published_inside_the_window_newest_first():
    fake_finnhub = FakeFinnhub(
        [
            make_finnhub_entry(WEDNESDAY - timedelta(days=9), "Too old"),
            make_finnhub_entry(WEDNESDAY - timedelta(days=6), "Last week"),
            make_finnhub_entry(WEDNESDAY - timedelta(hours=3), "This morning"),
            make_finnhub_entry(WEDNESDAY + timedelta(hours=1), "Not published yet"),
            make_finnhub_entry(WEDNESDAY + timedelta(days=2), "Later this week"),
        ]
    )
    feed, _ = make_feed(fake_finnhub)
    news_items = await feed.fetch("AAPL", WEDNESDAY - timedelta(days=7), WEDNESDAY)
    assert [item.headline for item in news_items] == ["This morning", "Last week"]
    assert all(item.published_at <= WEDNESDAY for item in news_items)
    assert all(item.source == "Reuters" and not item.is_commentary for item in news_items)
    # Two calendar weeks were needed, one request each.
    assert [request.url.params["from"] for request in fake_finnhub.requests] == [
        "2025-03-03",
        "2025-03-10",
    ]


async def test_key_travels_in_a_header_and_never_in_the_url():
    fake_finnhub = FakeFinnhub([])
    feed, _ = make_feed(fake_finnhub)
    await feed.fetch("AAPL", WEDNESDAY, WEDNESDAY)
    (request,) = fake_finnhub.requests
    assert request.headers["X-Finnhub-Token"] == API_KEY
    assert API_KEY not in str(request.url)
    assert request.url.params["symbol"] == "AAPL"


async def test_duplicates_are_dropped_and_the_result_is_capped():
    entries = [
        make_finnhub_entry(WEDNESDAY - timedelta(minutes=minute), f"Headline {minute}")
        for minute in range(30)
    ]
    entries.append(make_finnhub_entry(WEDNESDAY - timedelta(minutes=40), "HEADLINE 0"))
    feed, _ = make_feed(FakeFinnhub(entries))
    news_items = await feed.fetch("AAPL", WEDNESDAY - timedelta(days=1), WEDNESDAY)
    assert len(news_items) == MAXIMUM_ITEMS_PER_FETCH
    assert news_items[0].headline == "Headline 0"
    assert len({item.headline.casefold() for item in news_items}) == MAXIMUM_ITEMS_PER_FETCH


async def test_opinion_outlets_are_marked_as_commentary():
    feed, _ = make_feed(
        FakeFinnhub(
            [
                make_finnhub_entry(
                    WEDNESDAY - timedelta(hours=1), "Why it will moon", "SeekingAlpha"
                ),
                make_finnhub_entry(WEDNESDAY - timedelta(hours=2), "Earnings date set", "CNBC"),
            ]
        )
    )
    news_items = await feed.fetch("AAPL", WEDNESDAY - timedelta(days=1), WEDNESDAY)
    assert [(item.source, item.is_commentary) for item in news_items] == [
        ("SeekingAlpha", True),
        ("CNBC", False),
    ]


async def test_finished_weeks_are_cached_on_disk_and_the_current_week_is_not(tmp_path):
    fake_finnhub = FakeFinnhub([make_finnhub_entry(WEDNESDAY - timedelta(hours=1), "Cached item")])
    feed, _ = make_feed(fake_finnhub, tmp_path)
    await feed.fetch("AAPL", WEDNESDAY - timedelta(days=1), WEDNESDAY)
    await feed.fetch("AAPL", WEDNESDAY - timedelta(days=1), WEDNESDAY)
    assert len(fake_finnhub.requests) == 1  # second call served from memory
    cache_path = tmp_path / "finnhub" / "AAPL" / "2025-03-10.json"
    assert json.loads(cache_path.read_text())[0]["headline"] == "Cached item"

    second_feed, _ = make_feed(fake_finnhub, tmp_path)
    news_items = await second_feed.fetch("AAPL", WEDNESDAY - timedelta(days=1), WEDNESDAY)
    assert [item.headline for item in news_items] == ["Cached item"]
    assert len(fake_finnhub.requests) == 1  # a new process reads the disk cache

    cache_path.write_text("{not json")
    third_feed, _ = make_feed(fake_finnhub, tmp_path)
    await third_feed.fetch("AAPL", WEDNESDAY - timedelta(days=1), WEDNESDAY)
    assert len(fake_finnhub.requests) == 2  # a corrupt file is replaced, not trusted
    assert json.loads(cache_path.read_text())[0]["headline"] == "Cached item"

    # A week still in progress can gain news, so it is fetched fresh every time.
    live_feed, _ = make_feed(fake_finnhub, tmp_path, clock_time=WEDNESDAY)
    await live_feed.fetch("AAPL", WEDNESDAY - timedelta(hours=2), WEDNESDAY)
    await live_feed.fetch("AAPL", WEDNESDAY - timedelta(hours=2), WEDNESDAY)
    assert len(fake_finnhub.requests) == 4


async def test_requests_are_spaced_to_respect_the_rate_limit():
    feed, waits = make_feed(FakeFinnhub([]), minimum_request_interval_seconds=1.1)
    await feed.fetch("AAPL", WEDNESDAY - timedelta(days=20), WEDNESDAY)  # four weeks
    assert waits == [1.1, 1.1, 1.1]


@pytest.mark.parametrize("failure_status", [429, 500, 503])
async def test_rate_limit_and_server_errors_are_retried(failure_status):
    fake_finnhub = FakeFinnhub(
        [make_finnhub_entry(WEDNESDAY - timedelta(hours=1))],
        failures_before_success=2,
        failure_status=failure_status,
    )
    feed, waits = make_feed(fake_finnhub, minimum_request_interval_seconds=0)
    news_items = await feed.fetch("AAPL", WEDNESDAY - timedelta(hours=2), WEDNESDAY)
    assert len(news_items) == 1
    assert len(fake_finnhub.requests) == 3
    assert waits == [0.5, 1.0]


async def test_persistent_failures_and_client_errors_raise():
    always_failing, _ = make_feed(
        FakeFinnhub([], failures_before_success=9), minimum_request_interval_seconds=0
    )
    with pytest.raises(httpx.HTTPStatusError):
        await always_failing.fetch("AAPL", WEDNESDAY, WEDNESDAY)
    unauthorised = FakeFinnhub([], failures_before_success=9, failure_status=401)
    feed, _ = make_feed(unauthorised, minimum_request_interval_seconds=0)
    with pytest.raises(httpx.HTTPStatusError):
        await feed.fetch("AAPL", WEDNESDAY, WEDNESDAY)
    assert len(unauthorised.requests) == 1  # not retried


async def test_transport_errors_are_retried_then_raised():
    attempts = []

    def unreachable(request):
        attempts.append(request)
        raise httpx.ConnectError("no route")

    feed, waits = make_feed(unreachable, minimum_request_interval_seconds=0)
    with pytest.raises(httpx.ConnectError):
        await feed.fetch("AAPL", WEDNESDAY, WEDNESDAY)
    assert len(attempts) == 3
    assert waits == [0.5, 1.0]


async def test_backwards_window_is_rejected():
    feed, _ = make_feed(FakeFinnhub([]))
    with pytest.raises(ValueError, match="ends before it starts"):
        await feed.fetch("AAPL", WEDNESDAY, WEDNESDAY - timedelta(days=1))


def test_unusable_entries_are_dropped_not_invented():
    good = make_finnhub_entry(WEDNESDAY)
    news_items = parse_finnhub_items(
        [
            good,
            "not an object",
            {**good, "headline": "   "},
            {**good, "headline": None},
            {**good, "source": ""},
            {**good, "datetime": 0},
            {**good, "datetime": "yesterday"},
            {**good, "headline": "Insecure link", "url": "http://example.com/x"},
            {**good, "headline": "No link", "url": None},
        ]
    )
    assert [item.headline for item in news_items] == [
        "Apple ships a thing",
        "Insecure link",
        "No link",
    ]
    assert [item.url for item in news_items] == ["https://finnhub.io/api/news?id=abc", None, None]
    with pytest.raises(ValueError, match="unexpected news payload"):
        parse_finnhub_items({"error": "You don't have access to this resource."})


def test_weeks_start_on_monday():
    assert monday_of(WEDNESDAY.date()).isoformat() == "2025-03-10"
    assert monday_of(datetime(2025, 3, 10).date()).isoformat() == "2025-03-10"
    assert monday_of(datetime(2025, 3, 16).date()).isoformat() == "2025-03-10"
