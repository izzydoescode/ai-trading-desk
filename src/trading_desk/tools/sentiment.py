"""Sentiment feeds for FAMA.

The only feed today reads the tone of recent news headlines with a model. It is
news tone, not crowd mood from social posts, and the reading says so in its
source. The model labels headlines and nothing else: code counts the labels, and
FAMA's fixed thresholds turn the counts into a mood score and hype risk. A
one-sided reading can make PICARD wait; it can never create or enlarge a trade.
"""

from datetime import datetime
from typing import Protocol

from trading_desk.agents.fama import SentimentReading
from trading_desk.llm import ReasoningProvider
from trading_desk.models import HeadlineToneLabel
from trading_desk.tools.news import NewsFeed

MAXIMUM_HEADLINES_PER_READING = 40


class SentimentFeed(Protocol):
    name: str

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> SentimentReading:
        """A reading built only from what was public in `[start, end]`."""
        ...


class HeadlineToneFeed:
    """Counts bullish, bearish and neutral headlines as labelled by a model."""

    def __init__(
        self,
        news_feed: NewsFeed,
        llm: ReasoningProvider,
        maximum_headlines: int = MAXIMUM_HEADLINES_PER_READING,
    ):
        self.news_feed, self.llm, self.maximum_headlines = news_feed, llm, maximum_headlines
        self.name = f"headline-tone:{llm.name}"

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> SentimentReading:
        news_items = await self.news_feed.fetch(symbol, start, end, limit=self.maximum_headlines)
        source = (
            f"news tone ({self.llm.name} on {len(news_items)} {self.news_feed.name} headlines; "
            "not social posts)"
        )
        if not news_items:
            # Nothing to label: no model call, and FAMA reports the thin sample.
            return SentimentReading(
                source=source, bullish_post_count=0, bearish_post_count=0, item_label="headline"
            )
        headline_tone = await self.llm.classify_headlines(
            {
                "symbol": symbol,
                "headlines": [
                    {"number": number, "headline": item.headline}
                    for number, item in enumerate(news_items, start=1)
                ],
            }
        )
        labelled_numbers = sorted(label.number for label in headline_tone.labels)
        if labelled_numbers != list(range(1, len(news_items) + 1)):
            # A dropped, repeated or invented number means the counts cannot be trusted.
            raise ValueError("tone labels do not match the headlines supplied")
        tones = [label.tone for label in headline_tone.labels]
        return SentimentReading(
            source=source,
            bullish_post_count=tones.count(HeadlineToneLabel.BULLISH),
            bearish_post_count=tones.count(HeadlineToneLabel.BEARISH),
            neutral_post_count=tones.count(HeadlineToneLabel.NEUTRAL),
            item_label="headline",
        )
