"""Injectable data adapters. Provider failures never become invented candles."""

import asyncio
import json
from datetime import UTC, datetime
from importlib.resources import files
from typing import Protocol

import httpx

from trading_desk.models import Candle, MarketData


class MarketDataProvider(Protocol):
    name: str

    async def fetch(self, symbol: str) -> MarketData: ...


class FixtureProvider:
    name = "bundled-synthetic-fixture"

    def __init__(self, scenario: str = "trend"):
        if scenario not in {"trend", "conflict", "missing"}:
            raise ValueError("unknown fixture scenario")
        self.scenario = scenario

    async def fetch(self, symbol: str) -> MarketData:
        if symbol != "AAPL":
            return MarketData(
                symbol=symbol,
                source=self.name,
                synthetic=True,
                errors=["Bundled scenarios support AAPL only"],
            )
        raw = json.loads(
            files("trading_desk.fixtures").joinpath(f"{self.scenario}.json").read_text()
        )
        return MarketData(symbol=symbol, source=self.name, synthetic=True, candles=raw)


class AlphaVantageProvider:
    """Daily unadjusted bars; a research input, not a real-time price feed."""

    name = "alpha-vantage-daily-unadjusted"

    def __init__(self, api_key: str, client: httpx.AsyncClient):
        self.api_key, self.client = api_key, client

    async def fetch(self, symbol: str) -> MarketData:
        for attempt in range(3):
            try:
                response = await self.client.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": "TIME_SERIES_DAILY",
                        "symbol": symbol,
                        "apikey": self.api_key,
                        "outputsize": "compact",
                    },
                    timeout=20,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                response.raise_for_status()
                payload = response.json()
                series = payload.get("Time Series (Daily)")
                if not series:
                    # Do not log provider payloads: they may contain sensitive request details.
                    return MarketData(
                        symbol=symbol,
                        source=self.name,
                        errors=["Provider returned no daily series (quota or symbol error)"],
                    )
                candles = [
                    Candle(
                        timestamp=datetime.fromisoformat(day).replace(tzinfo=UTC),
                        open=float(v["1. open"]),
                        high=float(v["2. high"]),
                        low=float(v["3. low"]),
                        close=float(v["4. close"]),
                        volume=int(v["5. volume"]),
                    )
                    for day, v in sorted(series.items())
                ]
                return MarketData(symbol=symbol, source=self.name, candles=candles)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        raise RuntimeError("market data retry budget exhausted")
