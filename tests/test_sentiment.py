"""The headline-tone sentiment feed, with fakes for the news feed and the model."""

from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.agents import fama
from trading_desk.models import (
    HeadlineTone,
    HeadlineToneLabel,
    HypeRisk,
    NewsItem,
    NumberedHeadlineLabel,
)
from trading_desk.tools.sentiment import MAXIMUM_HEADLINES_PER_READING, HeadlineToneFeed

AS_OF = datetime(2025, 3, 12, 21, tzinfo=UTC)
WEEK_AGO = AS_OF - timedelta(days=7)
BULLISH, BEARISH, NEUTRAL = (
    HeadlineToneLabel.BULLISH,
    HeadlineToneLabel.BEARISH,
    HeadlineToneLabel.NEUTRAL,
)


class FakeNewsFeed:
    name = "fake-news"

    def __init__(self, headlines):
        self.headlines, self.requests = headlines, []

    async def fetch(self, symbol, start, end, limit=12):
        self.requests.append((symbol, start, end, limit))
        return [
            NewsItem(published_at=end - timedelta(hours=index), source="Wire", headline=headline)
            for index, headline in enumerate(self.headlines[:limit])
        ]


def numbered(tones, numbers=None):
    numbers = numbers or range(1, len(tones) + 1)
    return [
        NumberedHeadlineLabel(number=number, tone=tone)
        for number, tone in zip(numbers, tones, strict=True)
    ]


class FakeLabeller:
    name = "fake:labeller"

    def __init__(self, labels):
        self.labels, self.contexts = labels, []

    async def classify_headlines(self, context):
        self.contexts.append(context)
        return HeadlineTone(labels=self.labels)


async def test_reading_counts_the_models_labels_and_says_what_it_is():
    headlines = [f"Headline {index}" for index in range(25)]
    labeller = FakeLabeller(numbered([BULLISH] * 12 + [BEARISH] * 8 + [NEUTRAL] * 5))
    news_feed = FakeNewsFeed(headlines)
    feed = HeadlineToneFeed(news_feed, labeller)
    reading = await feed.fetch("AAPL", WEEK_AGO, AS_OF)
    assert (
        reading.bullish_post_count,
        reading.bearish_post_count,
        reading.neutral_post_count,
    ) == (12, 8, 5)
    assert reading.item_label == "headline"
    assert reading.source == (
        "news tone (fake:labeller on 25 fake-news headlines; not social posts)"
    )
    assert feed.name == "headline-tone:fake:labeller"
    # The model saw the symbol and the numbered headline text only.
    assert labeller.contexts[0]["symbol"] == "AAPL"
    assert labeller.contexts[0]["headlines"][:2] == [
        {"number": 1, "headline": "Headline 0"},
        {"number": 2, "headline": "Headline 1"},
    ]
    assert len(labeller.contexts[0]["headlines"]) == 25
    assert news_feed.requests == [("AAPL", WEEK_AGO, AS_OF, MAXIMUM_HEADLINES_PER_READING)]

    sentiment_report = fama.report(reading)
    assert sentiment_report.mood_score == 60
    assert sentiment_report.hype_risk is HypeRisk.LOW
    assert "20 directional headlines" in sentiment_report.summary
    assert "not social posts" in sentiment_report.summary
    assert sentiment_report.reasons[0] == "12 bullish headlines"


async def test_no_headlines_means_no_model_call_and_a_thin_sample():
    labeller = FakeLabeller([])
    reading = await HeadlineToneFeed(FakeNewsFeed([]), labeller).fetch("AAPL", WEEK_AGO, AS_OF)
    assert labeller.contexts == []
    sentiment_report = fama.report(reading)
    assert sentiment_report.abstain
    assert "Only 0 headline(s)" in sentiment_report.summary


@pytest.mark.parametrize(
    "numbers",
    [[], [1, 2], [1, 2, 3, 4], [1, 2, 2], [1, 2, 4], [2, 3, 4]],
    ids=["none", "one dropped", "one invented", "one repeated", "wrong number", "shifted"],
)
async def test_labels_that_do_not_cover_each_headline_exactly_once_are_rejected(numbers):
    labels = numbered([NEUTRAL] * len(numbers), numbers)
    feed = HeadlineToneFeed(FakeNewsFeed(["A", "B", "C"]), FakeLabeller(labels))
    with pytest.raises(ValueError, match="do not match"):
        await feed.fetch("AAPL", WEEK_AGO, AS_OF)


async def test_labels_may_arrive_in_any_order():
    labels = numbered([BEARISH, BULLISH, BULLISH], [3, 1, 2])
    reading = await HeadlineToneFeed(FakeNewsFeed(["A", "B", "C"]), FakeLabeller(labels)).fetch(
        "AAPL", WEEK_AGO, AS_OF
    )
    assert (reading.bullish_post_count, reading.bearish_post_count) == (2, 1)


async def test_headline_count_is_capped_to_bound_the_cost_of_one_reading():
    news_feed = FakeNewsFeed([f"Headline {index}" for index in range(100)])
    labeller = FakeLabeller(numbered([NEUTRAL] * 5))
    await HeadlineToneFeed(news_feed, labeller, maximum_headlines=5).fetch("AAPL", WEEK_AGO, AS_OF)
    assert len(labeller.contexts[0]["headlines"]) == 5
