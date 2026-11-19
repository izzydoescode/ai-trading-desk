"""Alpaca market data: daily bars, news and latest prices.

Alpaca is the desk's default source because one account covers history, news and
live prices at a generous rate limit (200 requests a minute on the free plan), and
its paper and live trading accounts share the same API for when orders are added.

This module READS data only. Nothing here places, changes or cancels an order.

Two details matter for correctness:

* Alpaca stamps a daily bar with the START of its session (midnight New York time).
  The desk treats a bar's timestamp as the moment its values became known, so each
  bar is re-stamped with its session's CLOSE. Without that, a decision made at noon
  could see a bar containing that afternoon's prices.
* The free plan's prices and volumes come from the IEX exchange only, so volumes
  are a fraction of the consolidated tape. Relative volume (today against its own
  average) is still meaningful; absolute volume is not.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from trading_desk.models import Candle, MarketData, NewsItem
from trading_desk.observability import get_logger
from trading_desk.tools.news import COMMENTARY_SOURCES, WeeklyNewsFeed

logger = get_logger("tools.alpaca")

ALPACA_DATA_URL = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
SNAPSHOTS_PATH = "/v2/stocks/snapshots"
NEWS_PATH = "/v1beta1/news"
NEW_YORK = ZoneInfo("America/New_York")
REGULAR_SESSION_OPEN = time(9, 30)
REGULAR_SESSION_CLOSE = time(16, 0)
# Two years and a margin: enough for a 52-week high plus the indicators' warm-up.
DEFAULT_HISTORY_DAYS = 800
BARS_PAGE_LIMIT = 10_000
MAXIMUM_BAR_PAGES = 5
NEWS_PAGE_LIMIT = 50
MAXIMUM_NEWS_PAGES_PER_WEEK = 4
# 200 requests a minute are allowed; one every 0.35 seconds stays under that.
ALPACA_MINIMUM_REQUEST_INTERVAL_SECONDS = 0.35
PRICE_DECIMAL_PLACES = 4

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


def authentication_headers(key_id: str, secret_key: str) -> dict[str, str]:
    """Alpaca's two credential headers. They never go in a URL."""
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}


def session_close(session_date: date) -> datetime:
    """The regular 16:00 New York close of a session, in UTC.

    Early-close days end sooner, so this is never earlier than the real close and
    can never let a bar be seen before its session finished.
    """
    return datetime.combine(session_date, REGULAR_SESSION_CLOSE, NEW_YORK).astimezone(UTC)


def parse_daily_bars(bars: object) -> list[Candle]:
    """Turn Alpaca's bar objects into candles stamped at their session close.

    A bar missing a price, or with an unreadable date, is skipped rather than guessed.
    """
    if not isinstance(bars, list):
        raise ValueError("unexpected bars payload")
    candles = []
    for bar in bars:
        try:
            session_date = (
                datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(NEW_YORK).date()
            )
            candles.append(
                Candle(
                    timestamp=session_close(session_date),
                    open=round(bar["o"], PRICE_DECIMAL_PLACES),
                    high=round(bar["h"], PRICE_DECIMAL_PLACES),
                    low=round(bar["l"], PRICE_DECIMAL_PLACES),
                    close=round(bar["c"], PRICE_DECIMAL_PLACES),
                    volume=bar.get("v") or 0,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning("Skipped an unreadable Alpaca bar")
    return candles


class AlpacaClient:
    """One authenticated, retrying GET against Alpaca's data API."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        client: httpx.AsyncClient,
        *,
        sleep: Sleeper = asyncio.sleep,
    ):
        self.headers = authentication_headers(key_id, secret_key)
        self.client, self._sleep = client, sleep

    async def get_json(self, path: str, params: dict) -> dict:
        """GET a data endpoint and return its JSON, retrying 429, 5xx and transport errors up
        to three times. Other HTTP errors raise at once.
        """
        for attempt in range(3):
            try:
                response = await self.client.get(
                    ALPACA_DATA_URL + path, params=params, headers=self.headers, timeout=30
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        logger.warning(
                            "Alpaca retry path=%s status=%d attempt=%d",
                            path,
                            response.status_code,
                            attempt + 1,
                        )
                        await self._sleep(0.5 * 2**attempt)
                        continue
                response.raise_for_status()
                return response.json()
            except httpx.TransportError:
                if attempt == 2:
                    raise
                logger.warning(
                    "Alpaca transport error path=%s attempt=%d", path, attempt + 1, exc_info=True
                )
                await self._sleep(0.5 * 2**attempt)
        raise RuntimeError("Alpaca retry budget exhausted")


class AlpacaBarsProvider:
    """Split- and dividend-adjusted daily bars from Alpaca, stamped at each session's close.

    Only completed sessions are returned: a bar whose close is still in the future
    (today's session before 16:00 New York time) is dropped, so the desk never
    decides on a half-formed bar.
    """

    def __init__(
        self,
        alpaca_client: AlpacaClient,
        *,
        history_days: int = DEFAULT_HISTORY_DAYS,
        feed: str = "iex",
        clock: Clock = lambda: datetime.now(UTC),
    ):
        self.alpaca_client, self.history_days, self.feed = alpaca_client, history_days, feed
        self._clock = clock
        self.name = f"alpaca-daily-{feed}"

    async def fetch(self, symbol: str) -> MarketData:
        """Fetch the symbol's daily history. Any failure is reported as an error on the data;
        bars are never invented. An unknown symbol returns no series, not an exception.
        """
        now = self._clock()
        params = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": (now - timedelta(days=self.history_days)).date().isoformat(),
            "limit": BARS_PAGE_LIMIT,
            "adjustment": "all",
            "feed": self.feed,
        }
        raw_bars: list = []
        for _ in range(MAXIMUM_BAR_PAGES):
            payload = await self.alpaca_client.get_json(BARS_PATH, params)
            raw_bars.extend((payload.get("bars") or {}).get(symbol) or [])
            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break
            params = {**params, "page_token": next_page_token}
        completed_candles = [
            candle for candle in parse_daily_bars(raw_bars) if candle.timestamp <= now
        ]
        if not completed_candles:
            return MarketData(
                symbol=symbol,
                source=self.name,
                errors=["Provider returned no daily series (unknown symbol or no data)"],
            )
        return MarketData(symbol=symbol, source=self.name, candles=completed_candles)

    async def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """The latest traded price for each symbol, for display. A symbol with no trade is
        left out rather than given a made-up price.
        """
        if not symbols:
            return {}
        payload = await self.alpaca_client.get_json(
            SNAPSHOTS_PATH, {"symbols": ",".join(symbols), "feed": self.feed}
        )
        latest_prices = {}
        for symbol in symbols:
            price = ((payload.get(symbol) or {}).get("latestTrade") or {}).get("p")
            if isinstance(price, int | float) and price > 0:
                latest_prices[symbol] = float(price)
        return latest_prices


def parse_intraday_bars(bars: object) -> list[Candle]:
    """Turn Alpaca's minute bars into candles stamped with each minute's own start time.

    Unlike daily bars these are only ever compared with each other, in order, so they keep
    Alpaca's timestamps. An unreadable bar is skipped.
    """
    if not isinstance(bars, list):
        raise ValueError("unexpected bars payload")
    candles = []
    for bar in bars:
        try:
            candles.append(
                Candle(
                    timestamp=datetime.fromisoformat(bar["t"].replace("Z", "+00:00")),
                    open=round(bar["o"], PRICE_DECIMAL_PLACES),
                    high=round(bar["h"], PRICE_DECIMAL_PLACES),
                    low=round(bar["l"], PRICE_DECIMAL_PLACES),
                    close=round(bar["c"], PRICE_DECIMAL_PLACES),
                    volume=bar.get("v") or 0,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning("Skipped an unreadable Alpaca minute bar")
    return sorted(candles, key=lambda candle: candle.timestamp)


class AlpacaIntradayBars:
    """One regular session of one-minute bars from Alpaca, for ordering a stop and a target
    that a single daily bar touched together.

    A session is fetched once and kept on disk (when a cache directory is given), because a
    finished session never changes; a backtest that needed it once reads it offline after.
    The free plan's minute bars come from IEX only, so a level reached only on another
    exchange will not show; the venue then falls back to its worst-case assumption.
    """

    def __init__(
        self,
        alpaca_client: AlpacaClient,
        cache_directory: Path | None = None,
        *,
        feed: str = "iex",
    ):
        self.alpaca_client, self.cache_directory, self.feed = alpaca_client, cache_directory, feed
        self.name = f"alpaca-1min-{feed}"

    def _cache_path(self, symbol: str, session_date: date) -> Path | None:
        if self.cache_directory is None:
            return None
        return self.cache_directory / self.name / symbol / f"{session_date.isoformat()}.json"

    async def fetch_session(self, symbol: str, daily_bar_time: datetime) -> list[Candle]:
        """Minute bars from 09:30 to 16:00 New York time on the daily bar's session date,
        oldest first. Raises on a provider failure; the caller decides what that means.
        """
        session_date = daily_bar_time.astimezone(NEW_YORK).date()
        cache_path = self._cache_path(symbol, session_date)
        closing_time = session_close(session_date)

        def within_the_session(candles: list[Candle]) -> list[Candle]:
            # Alpaca includes the minute that starts at the close; it belongs to after-hours.
            return [candle for candle in candles if candle.timestamp < closing_time]

        if cache_path is not None and cache_path.is_file():
            return within_the_session(parse_intraday_bars(json.loads(cache_path.read_text())))
        params = {
            "symbols": symbol,
            "timeframe": "1Min",
            "start": datetime.combine(session_date, REGULAR_SESSION_OPEN, NEW_YORK)
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "end": closing_time.isoformat().replace("+00:00", "Z"),
            "limit": BARS_PAGE_LIMIT,
            "adjustment": "all",
            "feed": self.feed,
        }
        raw_bars: list = []
        for _ in range(MAXIMUM_BAR_PAGES):
            payload = await self.alpaca_client.get_json(BARS_PATH, params)
            raw_bars.extend((payload.get("bars") or {}).get(symbol) or [])
            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break
            params = {**params, "page_token": next_page_token}
        candles = within_the_session(parse_intraday_bars(raw_bars))
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(raw_bars))
        logger.info(
            "Intraday bars fetched symbol=%s session=%s bars=%d", symbol, session_date, len(candles)
        )
        return candles


def parse_alpaca_news(entries: object) -> list[NewsItem]:
    """Turn Alpaca's news objects into news items, dropping unusable entries.

    The publication time is `created_at`. An entry without a headline, source or
    readable time is dropped; nothing is filled in.
    """
    if not isinstance(entries, list):
        raise ValueError("unexpected news payload")
    news_items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        headline, source, created_at = (
            entry.get("headline"),
            entry.get("source"),
            entry.get("created_at"),
        )
        if not (isinstance(headline, str) and headline.strip()):
            continue
        if not (isinstance(source, str) and source.strip()):
            continue
        try:
            published_at = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if published_at.tzinfo is None:
            continue
        url = entry.get("url")
        news_items.append(
            NewsItem(
                published_at=published_at,
                source=source.strip(),
                headline=headline.strip(),
                url=url if isinstance(url, str) and url.startswith("https://") else None,
                is_commentary=source.strip().casefold() in COMMENTARY_SOURCES,
            )
        )
    return news_items


class AlpacaNewsFeed(WeeklyNewsFeed):
    """News from Alpaca, one calendar week per symbol, cached once the week is over."""

    name = "alpaca-news"
    cache_folder = "alpaca"

    def __init__(self, alpaca_client: AlpacaClient, cache_directory: Path | None = None, **options):
        self.alpaca_client = alpaca_client
        super().__init__(alpaca_client.client, cache_directory, **options)

    def default_minimum_request_interval_seconds(self) -> float:
        return ALPACA_MINIMUM_REQUEST_INTERVAL_SECONDS

    async def _load_week(self, symbol: str, week_start: date, week_end: date) -> list[NewsItem]:
        """Read the week's news newest first, following pages up to a fixed cap so a very
        busy symbol cannot run away with the request budget.
        """
        params = {
            "symbols": symbol,
            "start": f"{week_start.isoformat()}T00:00:00Z",
            "end": f"{week_end.isoformat()}T23:59:59Z",
            "limit": NEWS_PAGE_LIMIT,
            "sort": "desc",
        }
        entries: list = []
        for _ in range(MAXIMUM_NEWS_PAGES_PER_WEEK):
            payload = await self._get_json(
                ALPACA_DATA_URL + NEWS_PATH,
                params,
                self.alpaca_client.headers,
                f"symbol={symbol} week={week_start}",
            )
            entries.extend(payload.get("news") or [])
            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break
            params = {**params, "page_token": next_page_token}
        return parse_alpaca_news(entries)
