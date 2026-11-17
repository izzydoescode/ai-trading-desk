"""Integration tests: all six agents wired through the decision graph."""

from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.agents.fama import SentimentReading
from trading_desk.desk import run_desk_decision, scan_watchlist
from trading_desk.models import (
    AccountSnapshot,
    Classification,
    DecisionAction,
    DeskReport,
    GateVerdict,
    HypeRisk,
    NewsItem,
    RiskLimits,
    RiskVerdict,
    ScoutVerdict,
)
from trading_desk.tools.market_data import FixtureProvider

AS_OF = datetime(2025, 4, 23, 22, tzinfo=UTC)
EXPECTED_AGENTS = {
    "market_data",
    "quality_audit",
    "hawkeye",
    "neo",
    "snowden",
    "fama",
    "odin",
    "picard",
}


def make_account_snapshot(**overrides):
    return AccountSnapshot(**{"equity": 100_000.0, "cash": 100_000.0, **overrides})


async def decide(scenario="trend", symbol="AAPL", as_of=AS_OF, account_snapshot=None, **options):
    return await run_desk_decision(
        symbol,
        FixtureProvider(scenario),
        account_snapshot or make_account_snapshot(),
        as_of,
        **options,
    )


async def test_clean_trend_produces_an_approved_sized_trade():
    report = await decide()
    assert report.schema_version == "3.0"
    assert report.mode == "deterministic"
    assert report.scout.verdict is ScoutVerdict.WATCH
    assert report.risk.verdict is RiskVerdict.APPROVE
    assert report.decision.action is DecisionAction.TRADE
    assert report.decision.plan == report.risk.plan
    assert report.decision.plan.risk_amount <= 1000.0
    assert {entry.agent for entry in report.trace} == EXPECTED_AGENTS
    assert all(entry.run_id == report.run_id for entry in report.trace)
    assert DeskReport.model_validate_json(report.model_dump_json()) == report


async def test_missing_news_and_sentiment_are_recorded_as_dissent_not_hidden():
    report = await decide()
    assert report.news.abstain
    assert report.sentiment.abstain
    assert any(line.startswith("SNOWDEN=INSUFFICIENT_DATA") for line in report.decision.dissent)
    assert any(line.startswith("FAMA=INSUFFICIENT_DATA") for line in report.decision.dissent)


async def test_contested_thesis_waits_even_though_risk_approves():
    report = await decide("conflict")
    assert report.chart.counter_case.classification is Classification.BEARISH
    assert report.risk.verdict is RiskVerdict.APPROVE
    assert report.decision.action is DecisionAction.WAIT
    assert report.decision.plan is None
    assert "RSI14" in report.decision.required_confirmation


async def test_insufficient_history_is_rejected_by_risk():
    report = await decide("missing")
    assert report.scout.verdict is ScoutVerdict.NEED_MORE_DATA
    assert report.decision.action is DecisionAction.REJECT
    assert "Data quality" in report.decision.failed_rule
    assert any("missing 42" in text for text in report.limitations)


async def test_stale_data_can_never_reach_a_trade():
    report = await decide(as_of=AS_OF + timedelta(days=10))
    assert report.decision.action is DecisionAction.REJECT
    assert any("Stale" in text for text in report.limitations)


async def test_decision_never_uses_bars_after_the_evaluation_time():
    report = await decide(as_of=AS_OF - timedelta(days=20))
    assert all(candle.timestamp <= report.evaluated_as_of for candle in report.data.candles)
    assert report.market_data_timestamp <= report.evaluated_as_of


async def test_unsupported_symbol_is_rejected_without_relabelled_data():
    report = await decide(symbol="MSFT")
    assert report.data.candles == []
    assert report.decision.action is DecisionAction.REJECT


@pytest.mark.parametrize(
    "limits,account_overrides,expected_failed_check",
    [
        (RiskLimits(kill_switch_engaged=True), {}, "Kill switch"),
        (RiskLimits(), {"open_idea_count": 1}, "Open ideas"),
        (RiskLimits(), {"realized_loss_today": 1_000.0}, "Daily loss budget"),
    ],
)
async def test_hard_limits_veto_an_otherwise_tradable_idea(
    limits, account_overrides, expected_failed_check
):
    report = await decide(
        account_snapshot=make_account_snapshot(**account_overrides), limits=limits
    )
    assert report.scout.verdict is ScoutVerdict.WATCH
    assert report.risk.verdict is RiskVerdict.REJECT
    assert expected_failed_check in report.risk.failed_checks
    assert report.decision.action is DecisionAction.REJECT
    assert report.decision.plan is None


async def test_reward_to_risk_floor_turns_a_trade_into_a_wait():
    report = await decide(limits=RiskLimits(minimum_reward_to_risk=3.0))
    assert report.risk.verdict is RiskVerdict.REVISE
    assert report.decision.action is DecisionAction.WAIT


async def test_supplied_news_is_filtered_to_the_evaluation_time():
    past_item = NewsItem(
        published_at=AS_OF - timedelta(days=1), source="Example Wire", headline="Past event"
    )
    future_item = NewsItem(
        published_at=AS_OF + timedelta(days=1), source="Example Wire", headline="Future event"
    )
    report = await decide(news_items=[past_item, future_item])
    assert [item.headline for item in report.news.verified_facts] == ["Past event"]


async def test_one_sided_crowd_makes_the_desk_wait():
    reading = SentimentReading(source="example-feed", bullish_post_count=95, bearish_post_count=5)
    report = await decide(sentiment_reading=reading)
    assert report.sentiment.hype_risk is HypeRisk.HIGH
    assert report.risk.verdict is RiskVerdict.APPROVE
    assert report.decision.action is DecisionAction.WAIT


async def test_invalid_requests_are_refused_before_any_agent_runs():
    with pytest.raises(ValueError):
        await decide(symbol="not a ticker")
    with pytest.raises(ValueError):
        await decide(as_of=datetime(2025, 4, 23, 22))
    with pytest.raises(ValueError):
        await decide(as_of=datetime.now(UTC) + timedelta(days=1))


class FailingProvider:
    name = "failing"

    async def fetch(self, symbol):
        raise RuntimeError("https://vendor.example/?apikey=SECRET-TOKEN")


async def test_provider_failure_is_rejected_and_never_leaks_exception_text():
    report = await run_desk_decision("AAPL", FailingProvider(), make_account_snapshot(), AS_OF)
    assert report.decision.action is DecisionAction.REJECT
    assert "SECRET-TOKEN" not in report.model_dump_json()
    assert any("RuntimeError" in text for text in report.limitations)


async def test_scan_ranks_reports_with_watched_symbols_first():
    reports = await scan_watchlist(
        ["MSFT", "AAPL"], FixtureProvider(), make_account_snapshot(), AS_OF
    )
    assert [report.symbol for report in reports] == ["AAPL", "MSFT"]
    assert reports[0].scout.verdict is ScoutVerdict.WATCH
    assert reports[1].scout.verdict is ScoutVerdict.NEED_MORE_DATA


class ScriptedReviewer:
    """Fake model reviewer: returns a scripted verdict or raises a scripted error."""

    name = "fake:reviewer"

    def __init__(self, verdict=None, error=None):
        self.verdict, self.error, self.contexts = verdict, error, []

    async def review_trade(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.verdict


async def test_model_approval_leaves_the_plan_untouched():
    reviewer = ScriptedReviewer(GateVerdict(approve=True, rationale="Reports agree."))
    report = await decide(llm=reviewer)
    assert report.mode == "llm"
    assert report.decision.action is DecisionAction.TRADE
    assert report.decision.plan == report.risk.plan
    assert report.model_review.approve
    picard_trace = next(entry for entry in report.trace if entry.agent == "picard")
    assert picard_trace.provider == "fake:reviewer"
    context = reviewer.contexts[0]
    assert context["plan"]["units"] == report.risk.plan.units
    assert "run_id" not in context and "as_of" not in context


async def test_model_veto_turns_an_approved_trade_into_a_wait():
    reviewer = ScriptedReviewer(GateVerdict(approve=False, rationale="Evidence is thin."))
    report = await decide(llm=reviewer)
    assert report.risk.verdict is RiskVerdict.APPROVE
    assert report.decision.action is DecisionAction.WAIT
    assert report.decision.plan is None
    assert "Evidence is thin." in report.decision.summary
    assert "MODEL REVIEW blocked" in report.decision.sources


async def test_model_failure_means_no_trade_and_leaks_nothing():
    reviewer = ScriptedReviewer(error=RuntimeError("Bearer sk-SECRET"))
    report = await decide(llm=reviewer)
    assert report.decision.action is DecisionAction.WAIT
    assert report.model_review is None
    assert "sk-SECRET" not in report.model_dump_json()
    assert any("Model review unavailable: RuntimeError" in text for text in report.limitations)


async def test_model_is_never_asked_about_ideas_the_rules_did_not_approve():
    reviewer = ScriptedReviewer(GateVerdict(approve=True, rationale="Fine."))
    for scenario in ("conflict", "missing"):
        report = await decide(scenario, llm=reviewer)
        assert report.decision.action is not DecisionAction.TRADE
        assert report.model_review is None
    assert reviewer.contexts == []


async def test_model_cannot_approve_what_the_risk_manager_rejected():
    reviewer = ScriptedReviewer(GateVerdict(approve=True, rationale="Looks great."))
    report = await decide(llm=reviewer, limits=RiskLimits(kill_switch_engaged=True))
    assert report.decision.action is DecisionAction.REJECT
    assert reviewer.contexts == []


class RecordingNewsFeed:
    """A news feed double that remembers the windows it was asked for."""

    name = "recording-news-feed"

    def __init__(self, news_items=None, error=None, delay_seconds=0.0):
        self.news_items, self.error, self.delay_seconds = news_items or [], error, delay_seconds
        self.requests = []

    async def fetch(self, symbol, start, end):
        import asyncio

        self.requests.append((symbol, start, end))
        await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return self.news_items


def make_news_item(published_at, headline="Guidance raised", **overrides):
    return NewsItem(
        **{"published_at": published_at, "source": "Reuters", "headline": headline, **overrides}
    )


def trace_entry(report, agent):
    return next(entry for entry in report.trace if entry.agent == agent)


async def test_news_feed_is_asked_for_the_week_ending_at_the_evaluation_time():
    news_feed = RecordingNewsFeed([make_news_item(AS_OF - timedelta(hours=5))])
    report = await decide(news_feed=news_feed)
    assert news_feed.requests == [("AAPL", AS_OF - timedelta(days=7), AS_OF)]
    assert not report.news.abstain
    assert [item.headline for item in report.news.verified_facts] == ["Guidance raised"]
    assert trace_entry(report, "snowden").provider == "recording-news-feed"
    assert trace_entry(report, "snowden").status == "ok"


async def test_a_feed_that_returns_future_news_cannot_leak_it_into_the_report():
    news_feed = RecordingNewsFeed(
        [
            make_news_item(AS_OF + timedelta(hours=1), "Tomorrow's headline"),
            make_news_item(AS_OF - timedelta(hours=1), "Today's headline"),
        ]
    )
    report = await decide(news_feed=news_feed)
    assert [item.headline for item in report.news.verified_facts] == ["Today's headline"]
    assert "Tomorrow" not in report.model_dump_json()


async def test_a_failing_news_feed_makes_snowden_abstain_and_hides_the_error_text():
    news_feed = RecordingNewsFeed(error=RuntimeError("https://vendor.example/?token=SECRET-VALUE"))
    report = await decide(news_feed=news_feed)
    assert report.news.abstain
    assert report.news.summary == "No news source was supplied to the desk."
    snowden_trace = trace_entry(report, "snowden")
    assert snowden_trace.status == "degraded"
    assert snowden_trace.error == "News retrieval failed: RuntimeError"
    assert "News retrieval failed: RuntimeError" in report.limitations
    assert "SECRET-VALUE" not in report.model_dump_json()
    # A broken feed is a missing input, not a reason to trade or to veto.
    assert report.decision.action is DecisionAction.TRADE


async def test_a_slow_news_feed_times_out_and_snowden_abstains(monkeypatch):
    monkeypatch.setattr("trading_desk.desk.NEWS_TIMEOUT_SECONDS", 0.01)
    report = await decide(news_feed=RecordingNewsFeed(delay_seconds=0.5))
    assert report.news.abstain
    assert trace_entry(report, "snowden").error == "News retrieval failed: TimeoutError"


async def test_items_supplied_directly_win_over_the_feed():
    news_feed = RecordingNewsFeed([make_news_item(AS_OF - timedelta(hours=1), "From the feed")])
    report = await decide(
        news_feed=news_feed, news_items=[make_news_item(AS_OF - timedelta(hours=2), "Supplied")]
    )
    assert news_feed.requests == []
    assert [item.headline for item in report.news.verified_facts] == ["Supplied"]


async def test_watchlist_scan_asks_the_feed_once_per_symbol():
    news_feed = RecordingNewsFeed([make_news_item(AS_OF - timedelta(hours=5))])
    reports = await scan_watchlist(
        ["AAPL", "MSFT"], FixtureProvider(), make_account_snapshot(), AS_OF, news_feed=news_feed
    )
    assert sorted(symbol for symbol, _, _ in news_feed.requests) == ["AAPL", "MSFT"]
    assert len(reports) == 2


class ScriptedSentimentFeed:
    name = "scripted-sentiment"

    def __init__(self, bullish=0, bearish=0, neutral=0, error=None, delay_seconds=0.0):
        self.counts, self.error, self.delay_seconds = (
            (bullish, bearish, neutral),
            error,
            delay_seconds,
        )
        self.requests = []

    async def fetch(self, symbol, start, end):
        import asyncio

        self.requests.append((symbol, start, end))
        await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        bullish, bearish, neutral = self.counts
        return SentimentReading(
            source="scripted tone",
            bullish_post_count=bullish,
            bearish_post_count=bearish,
            neutral_post_count=neutral,
            item_label="headline",
        )


async def test_sentiment_feed_gives_fama_a_point_in_time_reading():
    sentiment_feed = ScriptedSentimentFeed(bullish=12, bearish=8, neutral=5)
    report = await decide(sentiment_feed=sentiment_feed)
    assert sentiment_feed.requests == [("AAPL", AS_OF - timedelta(days=7), AS_OF)]
    assert report.sentiment.mood_score == 60
    assert report.sentiment.hype_risk is HypeRisk.LOW
    assert trace_entry(report, "fama").provider == "scripted-sentiment"
    assert report.decision.action is DecisionAction.TRADE


async def test_one_sided_headline_tone_can_only_make_the_desk_wait():
    report = await decide(sentiment_feed=ScriptedSentimentFeed(bullish=19, bearish=1))
    assert report.sentiment.hype_risk is HypeRisk.HIGH
    assert report.risk.verdict is RiskVerdict.APPROVE
    assert report.decision.action is DecisionAction.WAIT
    assert report.decision.plan is None
    assert "hype" in report.decision.summary


async def test_a_failing_sentiment_feed_makes_fama_abstain_and_hides_the_error_text():
    sentiment_feed = ScriptedSentimentFeed(error=RuntimeError("sk-SECRET-VALUE rejected"))
    report = await decide(sentiment_feed=sentiment_feed)
    assert report.sentiment.abstain
    fama_trace = trace_entry(report, "fama")
    assert (fama_trace.status, fama_trace.error) == (
        "degraded",
        "Sentiment retrieval failed: RuntimeError",
    )
    assert "SECRET-VALUE" not in report.model_dump_json()
    assert report.decision.action is DecisionAction.TRADE


async def test_a_slow_sentiment_feed_times_out_and_fama_abstains(monkeypatch):
    monkeypatch.setattr("trading_desk.desk.SENTIMENT_TIMEOUT_SECONDS", 0.01)
    report = await decide(sentiment_feed=ScriptedSentimentFeed(delay_seconds=0.5))
    assert report.sentiment.abstain
    assert trace_entry(report, "fama").error == "Sentiment retrieval failed: TimeoutError"


async def test_a_reading_supplied_directly_wins_over_the_feed():
    sentiment_feed = ScriptedSentimentFeed(bullish=19, bearish=1)
    report = await decide(
        sentiment_feed=sentiment_feed,
        sentiment_reading=SentimentReading(
            source="supplied", bullish_post_count=10, bearish_post_count=10
        ),
    )
    assert sentiment_feed.requests == []
    assert report.sentiment.mood_score == 50


async def test_bearish_news_tone_makes_the_desk_wait_and_bullish_tone_changes_nothing():
    news_feed = RecordingNewsFeed([make_news_item(AS_OF - timedelta(hours=5))])
    bearish = await decide(
        news_feed=news_feed, news_tone_feed=ScriptedSentimentFeed(bullish=1, bearish=9)
    )
    assert bearish.news.verdict is Classification.BEARISH
    assert bearish.risk.verdict is RiskVerdict.APPROVE
    assert bearish.decision.action is DecisionAction.WAIT
    assert "SNOWDEN reports bearish news" in bearish.decision.summary

    plain = await decide(news_feed=news_feed)
    bullish = await decide(
        news_feed=news_feed, news_tone_feed=ScriptedSentimentFeed(bullish=9, bearish=1)
    )
    assert bullish.news.verdict is Classification.BULLISH
    assert bullish.decision.action is DecisionAction.TRADE
    assert bullish.decision.plan == plain.decision.plan  # good news never enlarges a trade


async def test_a_failed_tone_reading_falls_back_to_listing_the_items():
    news_feed = RecordingNewsFeed([make_news_item(AS_OF - timedelta(hours=5))])
    report = await decide(
        news_feed=news_feed,
        news_tone_feed=ScriptedSentimentFeed(error=RuntimeError("sk-SECRET-VALUE")),
    )
    assert report.news.verdict is Classification.NEUTRAL
    assert not report.news.abstain
    assert trace_entry(report, "snowden").error == "News tone failed: RuntimeError"
    assert "SECRET-VALUE" not in report.model_dump_json()


async def test_tone_is_not_requested_when_there_is_no_news_to_label():
    tone_feed = ScriptedSentimentFeed(bullish=9, bearish=1)
    report = await decide(news_feed=RecordingNewsFeed([]), news_tone_feed=tone_feed)
    assert tone_feed.requests == []
    assert report.news.abstain
