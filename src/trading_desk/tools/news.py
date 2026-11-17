"""News feeds for SNOWDEN: dated, sourced items, strictly point-in-time.

A feed returns what was already published inside the window it is asked for and
nothing later, so a replay sees the news exactly as the desk would have on that
bar. Feeds never summarise or invent: an item without a usable headline, source
or timestamp is dropped.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx

from trading_desk.models import NewsItem
from trading_desk.observability import get_logger

logger = get_logger("tools.news")

MAXIMUM_ITEMS_PER_FETCH = 12
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
# Finnhub's free tier allows 60 calls a minute; stay under it during a long replay.
FINNHUB_MINIMUM_REQUEST_INTERVAL_SECONDS = 1.1
# Outlets that publish opinion and screens, not reporting. Their items are shown to
# the desk as commentary and are never counted as evidence.
COMMENTARY_SOURCES = frozenset(
    {"seekingalpha", "chartmill", "motley fool", "zacks", "investorplace"}
)
WEEK = timedelta(days=7)

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class NewsFeed(Protocol):
    name: str

    async def fetch(
        self, symbol: str, start: datetime, end: datetime, limit: int = MAXIMUM_ITEMS_PER_FETCH
    ) -> list[NewsItem]:
        """Up to `limit` items published in `[start, end]`, newest first. Never
        anything after `end`."""
        ...


def parse_finnhub_items(payload: object) -> list[NewsItem]:
    """Turn Finnhub's company-news array into news items, dropping unusable entries."""
    if not isinstance(payload, list):
        raise ValueError("unexpected news payload")
    news_items = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        headline, source = entry.get("headline"), entry.get("source")
        published_seconds = entry.get("datetime")
        if not (isinstance(headline, str) and headline.strip()):
            continue
        if not (isinstance(source, str) and source.strip()):
            continue
        if not isinstance(published_seconds, int | float) or published_seconds <= 0:
            continue
        url = entry.get("url")
        news_items.append(
            NewsItem(
                published_at=datetime.fromtimestamp(published_seconds, UTC),
                source=source.strip(),
                headline=headline.strip(),
                url=url if isinstance(url, str) and url.startswith("https://") else None,
                is_commentary=source.strip().casefold() in COMMENTARY_SOURCES,
            )
        )
    return news_items


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


class WeeklyNewsFeed:
    """Shared machinery for a news feed that is read one calendar week at a time.

    A finished week never changes, so it is stored on disk and a replay costs one
    request per symbol per week, once. The week still in progress is always fetched
    fresh. Requests are spaced to stay under the vendor's rate limit and retried on
    429, 5xx and transport errors. A subclass supplies the vendor call in `_load_week`.
    """

    name = "weekly-news"
    cache_folder = "weekly"

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_directory: Path | None = None,
        *,
        minimum_request_interval_seconds: float | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Clock = lambda: datetime.now(UTC),
    ):
        """Create the feed with its HTTP client and optional disk cache. The request interval,
        sleep and clock are injectable so tests run without waiting.
        """
        self.client = client
        self.cache_directory = Path(cache_directory) if cache_directory else None
        self.minimum_request_interval_seconds = (
            self.default_minimum_request_interval_seconds()
            if minimum_request_interval_seconds is None
            else minimum_request_interval_seconds
        )
        self._sleep, self._clock = sleep, clock
        self._request_lock = asyncio.Lock()
        self._last_request_at: datetime | None = None
        self._weeks_in_memory: dict[tuple[str, date], list[NewsItem]] = {}

    def default_minimum_request_interval_seconds(self) -> float:
        """Seconds to leave between requests unless the caller says otherwise."""
        return 0.0

    async def _load_week(self, symbol: str, week_start: date, week_end: date) -> list[NewsItem]:
        """Ask the vendor for one calendar week of items for one symbol."""
        raise NotImplementedError

    async def fetch(
        self, symbol: str, start: datetime, end: datetime, limit: int = MAXIMUM_ITEMS_PER_FETCH
    ) -> list[NewsItem]:
        """Return up to `limit` items published inside `[start, end]`, newest first, with duplicate
        headlines removed. Reads every calendar week the window touches, then drops anything
        outside the window, so news after `end` can never appear.
        """
        if end < start:
            raise ValueError("the news window ends before it starts")
        week_start = monday_of(start.astimezone(UTC).date())
        last_week_start = monday_of(end.astimezone(UTC).date())
        news_items: list[NewsItem] = []
        while week_start <= last_week_start:
            news_items.extend(await self._week(symbol, week_start))
            week_start += WEEK
        seen_headlines: set[str] = set()
        visible_items = []
        for item in sorted(news_items, key=lambda item: item.published_at, reverse=True):
            # Point-in-time: nothing published after `end` existed yet.
            if not start <= item.published_at <= end:
                continue
            if item.headline.casefold() in seen_headlines:
                continue
            seen_headlines.add(item.headline.casefold())
            visible_items.append(item)
        return visible_items[:limit]

    async def _week(self, symbol: str, week_start: date) -> list[NewsItem]:
        """Return one calendar week of news for a symbol, from memory, then the disk cache, then
        the vendor. Only finished weeks are cached: a week in progress can still gain news.
        """
        cache_key = (symbol, week_start)
        if cache_key in self._weeks_in_memory:
            return self._weeks_in_memory[cache_key]
        week_end = week_start + timedelta(days=6)
        week_is_finished = week_end < self._clock().date()
        cache_path = (
            self.cache_directory / self.cache_folder / symbol / f"{week_start.isoformat()}.json"
            if self.cache_directory
            else None
        )
        if cache_path is not None and week_is_finished and cache_path.is_file():
            try:
                news_items = [
                    NewsItem.model_validate(entry) for entry in json.loads(cache_path.read_text())
                ]
                self._weeks_in_memory[cache_key] = news_items
                return news_items
            except ValueError:
                # A corrupt cache file is ignored and replaced below.
                logger.warning("Corrupt news cache ignored path=%s", cache_path, exc_info=True)
        news_items = await self._load_week(symbol, week_start, week_end)
        if week_is_finished:
            self._weeks_in_memory[cache_key] = news_items
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps([item.model_dump(mode="json") for item in news_items])
                )
        return news_items

    async def _get_json(self, url: str, params: dict, headers: dict, context: str) -> object:
        """Make one rate-limited GET and return its JSON, retrying 429, 5xx and transport
        errors up to three times. `context` names the request in log lines.
        """
        async with self._request_lock:
            for attempt in range(3):
                await self._respect_rate_limit()
                try:
                    response = await self.client.get(
                        url, params=params, headers=headers, timeout=30
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            logger.warning(
                                "%s retry %s status=%d attempt=%d",
                                self.name,
                                context,
                                response.status_code,
                                attempt + 1,
                            )
                            await self._sleep(0.5 * 2**attempt)
                            continue
                    response.raise_for_status()
                    logger.debug("%s fetched %s", self.name, context)
                    return response.json()
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    logger.warning(
                        "%s transport error %s attempt=%d",
                        self.name,
                        context,
                        attempt + 1,
                        exc_info=True,
                    )
                    await self._sleep(0.5 * 2**attempt)
        raise RuntimeError("news retry budget exhausted")

    async def _respect_rate_limit(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            elapsed_seconds = (now - self._last_request_at).total_seconds()
            if elapsed_seconds < self.minimum_request_interval_seconds:
                await self._sleep(self.minimum_request_interval_seconds - elapsed_seconds)
        self._last_request_at = self._clock()


class FinnhubNewsFeed(WeeklyNewsFeed):
    """Company news from Finnhub. The key travels in a header, never in the URL."""

    name = "finnhub-company-news"
    cache_folder = "finnhub"

    def __init__(self, api_key: str, client: httpx.AsyncClient, *arguments, **options):
        self.api_key = api_key
        super().__init__(client, *arguments, **options)

    def default_minimum_request_interval_seconds(self) -> float:
        return FINNHUB_MINIMUM_REQUEST_INTERVAL_SECONDS

    async def _load_week(self, symbol: str, week_start: date, week_end: date) -> list[NewsItem]:
        payload = await self._get_json(
            FINNHUB_COMPANY_NEWS_URL,
            {"symbol": symbol, "from": week_start.isoformat(), "to": week_end.isoformat()},
            {"X-Finnhub-Token": self.api_key},
            f"symbol={symbol} week={week_start}",
        )
        return parse_finnhub_items(payload)


class FallbackNewsFeed:
    """Asks the primary feed and, only if it fails, the backup.

    An empty answer from the primary is an answer and is returned as it is. The backup
    is used when the primary raises, so one vendor's outage does not blind SNOWDEN.
    """

    def __init__(self, primary_feed: NewsFeed, backup_feed: NewsFeed):
        self.primary_feed, self.backup_feed = primary_feed, backup_feed
        self.name = f"{primary_feed.name} (backup {backup_feed.name})"

    async def fetch(
        self, symbol: str, start: datetime, end: datetime, limit: int = MAXIMUM_ITEMS_PER_FETCH
    ) -> list[NewsItem]:
        """Items from the primary feed, or from the backup if the primary raises."""
        try:
            return await self.primary_feed.fetch(symbol, start, end, limit)
        except Exception:
            logger.warning(
                "Primary news feed failed symbol=%s feed=%s; using backup feed=%s",
                symbol,
                self.primary_feed.name,
                self.backup_feed.name,
                exc_info=True,
            )
            return await self.backup_feed.fetch(symbol, start, end, limit)
