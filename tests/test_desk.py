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


def account(**overrides):
    return AccountSnapshot(**{"equity": 100_000.0, "cash": 100_000.0, **overrides})


async def decide(scenario="trend", symbol="AAPL", as_of=AS_OF, account_snapshot=None, **options):
    return await run_desk_decision(
        symbol, FixtureProvider(scenario), account_snapshot or account(), as_of, **options
    )


async def test_clean_trend_produces_an_approved_sized_trade():
    report = await decide()
    assert report.schema_version == "2.0"
    assert report.mode == "deterministic"
    assert report.scout.verdict is ScoutVerdict.WATCH
    assert report.risk.verdict is RiskVerdict.APPROVE
    assert report.decision.action is DecisionAction.TRADE
    assert report.decision.plan == report.risk.plan
    assert report.decision.plan.risk_amount <= 250.0
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
    report = await decide(account_snapshot=account(**account_overrides), limits=limits)
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
    report = await run_desk_decision("AAPL", FailingProvider(), account(), AS_OF)
    assert report.decision.action is DecisionAction.REJECT
    assert "SECRET-TOKEN" not in report.model_dump_json()
    assert any("RuntimeError" in text for text in report.limitations)


async def test_scan_ranks_reports_with_watched_symbols_first():
    reports = await scan_watchlist(["MSFT", "AAPL"], FixtureProvider(), account(), AS_OF)
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
