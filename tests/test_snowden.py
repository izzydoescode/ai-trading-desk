from datetime import UTC, datetime, timedelta

from trading_desk.agents import snowden
from trading_desk.models import Classification, NewsItem

AS_OF = datetime(2025, 4, 23, 22, tzinfo=UTC)


def news_item(days_before=1, headline="Company reports quarter", is_commentary=False):
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
        [news_item(1), news_item(2, "Analyst thinks shares are cheap", is_commentary=True)], AS_OF
    )
    assert not news_report.abstain
    assert news_report.verdict is Classification.NEUTRAL
    assert len(news_report.verified_facts) == 1
    assert len(news_report.commentary) == 1


def test_snowden_never_uses_items_published_after_the_evaluation_time():
    news_report = snowden.report([news_item(days_before=-1, headline="Tomorrow's news")], AS_OF)
    assert news_report.abstain
    assert news_report.verified_facts == []


def test_snowden_flags_stale_and_duplicated_items():
    news_report = snowden.report([news_item(1), news_item(2), news_item(30, "Old story")], AS_OF)
    assert any("older than 7 days" in flag for flag in news_report.red_flags)
    assert any("duplicated" in flag for flag in news_report.red_flags)
    assert len(news_report.verified_facts) == 2


def test_snowden_abstains_when_only_commentary_is_supplied():
    news_report = snowden.report([news_item(1, is_commentary=True)], AS_OF)
    assert news_report.abstain
    assert len(news_report.commentary) == 1
