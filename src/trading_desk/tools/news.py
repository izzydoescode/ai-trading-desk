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


class FinnhubNewsFeed:
    """Company news from Finnhub, fetched one calendar week at a time and cached.

    A finished week never changes, so it is stored on disk and a replay costs one
    request per symbol per week, once. The week still in progress is always
    fetched fresh.
    """

    name = "finnhub-company-news"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        cache_directory: Path | None = None,
        *,
        minimum_request_interval_seconds: float | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Clock = lambda: datetime.now(UTC),
    ):
        self.api_key, self.client = api_key, client
        self.cache_directory = Path(cache_directory) if cache_directory else None
        self.minimum_request_interval_seconds = (
            FINNHUB_MINIMUM_REQUEST_INTERVAL_SECONDS
            if minimum_request_interval_seconds is None
            else minimum_request_interval_seconds
        )
        self._sleep, self._clock = sleep, clock
        self._request_lock = asyncio.Lock()
        self._last_request_at: datetime | None = None
        self._weeks_in_memory: dict[tuple[str, date], list[NewsItem]] = {}

    async def fetch(
        self, symbol: str, start: datetime, end: datetime, limit: int = MAXIMUM_ITEMS_PER_FETCH
    ) -> list[NewsItem]:
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
        cache_key = (symbol, week_start)
        if cache_key in self._weeks_in_memory:
            return self._weeks_in_memory[cache_key]
        week_end = week_start + timedelta(days=6)
        week_is_finished = week_end < self._clock().date()
        cache_path = (
            self.cache_directory / "finnhub" / symbol / f"{week_start.isoformat()}.json"
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
                pass  # A corrupt cache file is ignored and replaced below.
        news_items = parse_finnhub_items(await self._request(symbol, week_start, week_end))
        if week_is_finished:
            self._weeks_in_memory[cache_key] = news_items
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps([item.model_dump(mode="json") for item in news_items])
                )
        return news_items

    async def _request(self, symbol: str, week_start: date, week_end: date) -> object:
        async with self._request_lock:
            for attempt in range(3):
                await self._respect_rate_limit()
                try:
                    response = await self.client.get(
                        FINNHUB_COMPANY_NEWS_URL,
                        params={
                            "symbol": symbol,
                            "from": week_start.isoformat(),
                            "to": week_end.isoformat(),
                        },
                        # The key travels in a header, never in the URL.
                        headers={"X-Finnhub-Token": self.api_key},
                        timeout=30,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            await self._sleep(0.5 * 2**attempt)
                            continue
                    response.raise_for_status()
                    return response.json()
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await self._sleep(0.5 * 2**attempt)
        raise RuntimeError("news retry budget exhausted")

    async def _respect_rate_limit(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            elapsed_seconds = (now - self._last_request_at).total_seconds()
            if elapsed_seconds < self.minimum_request_interval_seconds:
                await self._sleep(self.minimum_request_interval_seconds - elapsed_seconds)
        self._last_request_at = self._clock()
