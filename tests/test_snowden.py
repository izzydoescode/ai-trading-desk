from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.agents import snowden
from trading_desk.models import Classification, NewsItem

AS_OF = datetime(2025, 4, 23, 22, tzinfo=UTC)


def make_news_item(days_before=1, headline="Company reports quarter", is_commentary=False):
    return NewsItem(
        published_at=AS_OF - timedelta(days=days_before),
        source="Example Wire",
        headline=headline,
        is_commentary=is_commentary,
    )


def test_snowden_abstains_and_names_what_is_missing_without_a_source():
    news_report = snowden.report(None, AS_OF)
    assert news_report.abstain
    assert news_report.verdict is Classification.INSUFFICIENT_DATA
    assert news_report.missing_information == ["Dated news items for the symbol"]
    assert news_report.verified_facts == []


def test_snowden_separates_facts_from_commentary():
    news_report = snowden.report(
        [
            make_news_item(1),
            make_news_item(2, "Analyst thinks shares are cheap", is_commentary=True),
        ],
        AS_OF,
    )
    assert not news_report.abstain
    assert news_report.verdict is Classification.NEUTRAL
    assert len(news_report.verified_facts) == 1
    assert len(news_report.commentary) == 1


def test_snowden_never_uses_items_published_after_the_evaluation_time():
    news_report = snowden.report(
        [make_news_item(days_before=-1, headline="Tomorrow's news")], AS_OF
    )
    assert news_report.abstain
    assert news_report.verified_facts == []


def test_snowden_flags_stale_and_duplicated_items():
    news_report = snowden.report(
        [make_news_item(1), make_news_item(2), make_news_item(30, "Old story")], AS_OF
    )
    assert any("older than 7 days" in flag for flag in news_report.red_flags)
    assert any("duplicated" in flag for flag in news_report.red_flags)
    assert len(news_report.verified_facts) == 2


def test_snowden_abstains_when_only_commentary_is_supplied():
    news_report = snowden.report([make_news_item(1, is_commentary=True)], AS_OF)
    assert news_report.abstain
    assert len(news_report.commentary) == 1


def make_headline_tone(bullish, bearish, neutral=0):
    from trading_desk.agents.fama import SentimentReading

    return SentimentReading(
        source="test tone",
        bullish_post_count=bullish,
        bearish_post_count=bearish,
        neutral_post_count=neutral,
        item_label="headline",
    )


def test_without_a_tone_reading_snowden_only_lists_items_and_stays_neutral():
    news_report = snowden.report([make_news_item()], AS_OF)
    assert news_report.verdict is Classification.NEUTRAL
    assert news_report.bullish_headline_count is None
    assert "do not judge market impact" in news_report.summary


@pytest.mark.parametrize(
    "bullish,bearish,expected",
    [
        (6, 4, Classification.BULLISH),  # exactly 60% bullish
        (9, 1, Classification.BULLISH),
        (4, 6, Classification.BEARISH),  # exactly 40% bullish
        (0, 7, Classification.BEARISH),
        (5, 5, Classification.NEUTRAL),
        (11, 9, Classification.NEUTRAL),
    ],
)
def test_tone_sets_the_verdict_at_fixed_thresholds(bullish, bearish, expected):
    news_report = snowden.report(
        [make_news_item()], AS_OF, make_headline_tone(bullish, bearish, neutral=20)
    )
    assert news_report.verdict is expected
    assert not news_report.abstain
    assert (
        news_report.bullish_headline_count,
        news_report.bearish_headline_count,
        news_report.neutral_headline_count,
    ) == (bullish, bearish, 20)
    assert f"{bullish} bullish, {bearish} bearish, 20 neutral (test tone)" in news_report.summary
    assert "not a forecast" in news_report.summary


@pytest.mark.parametrize("bullish,bearish", [(0, 0), (4, 0), (0, 4), (2, 2)])
def test_too_few_directional_headlines_cannot_call_the_tone(bullish, bearish):
    news_report = snowden.report(
        [make_news_item()], AS_OF, make_headline_tone(bullish, bearish, neutral=30)
    )
    assert news_report.verdict is Classification.NEUTRAL
    assert f"Only {bullish + bearish} headline(s) lean either way" in news_report.summary


def test_tone_never_rescues_a_report_with_no_usable_items():
    assert snowden.report(None, AS_OF, make_headline_tone(9, 1)).abstain
    assert snowden.report(
        [make_news_item(is_commentary=True)], AS_OF, make_headline_tone(9, 1)
    ).abstain
    assert snowden.report([make_news_item(days_before=30)], AS_OF, make_headline_tone(9, 1)).abstain
