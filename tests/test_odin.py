import pytest

from trading_desk.agents import odin
from trading_desk.markets import US_EQUITIES
from trading_desk.models import (
    AccountSnapshot,
    Analysis,
    ChartReport,
    Classification,
    Evidence,
    RiskLimits,
    RiskVerdict,
    TradeSetup,
)

NO_COST_MARKET = US_EQUITIES.model_copy(update={"commission_rate": 0.0, "slippage_rate": 0.0})


def analysis():
    return Analysis(
        classification=Classification.BULLISH,
        summary="Trend up.",
        evidence=[Evidence(metric="close", value=100.0)],
        risks=[],
        abstain=False,
    )


def setup(entry_reference=100.0, stop=95.0, target=115.0):
    """A chart report carrying a complete trade setup."""
    return ChartReport(
        thesis=analysis(),
        counter_case=analysis(),
        setup=TradeSetup(
            strategy="TREND_CONTINUATION",
            entry_reference=entry_reference,
            stop=stop,
            target=target,
            confirmation="c",
            invalidation="i",
        ),
    )


def chart_without_a_setup():
    return ChartReport(thesis=analysis(), counter_case=analysis(), watch_for="w")


def account(**overrides):
    return AccountSnapshot(**{"equity": 100_000.0, "cash": 100_000.0, **overrides})


def evaluate(
    chart_setup=None, account_snapshot=None, limits=None, market=NO_COST_MARKET, blockers=None
):
    return odin.evaluate(
        "AAPL",
        chart_setup or setup(),
        account_snapshot or account(),
        limits or RiskLimits(),
        market,
        blockers or [],
    )


def check(report, name):
    return next(risk_check for risk_check in report.checks if risk_check.name == name)


def test_clean_setup_is_approved_and_sized_from_the_loss_limit():
    report = evaluate()
    assert report.verdict is RiskVerdict.APPROVE
    assert all(risk_check.passed for risk_check in report.checks)
    # 1% of 100,000 = 1,000 of risk; 5 per share => 200 shares.
    assert report.plan.units == 200
    assert report.plan.risk_amount == pytest.approx(1000.0)
    assert report.plan.notional == pytest.approx(20_000.0)
    assert report.plan.reward_to_risk == pytest.approx(3.0)


def test_trading_costs_reduce_reward_and_increase_risk():
    costly_market = US_EQUITIES.model_copy(update={"commission_rate": 0.0, "slippage_rate": 0.005})
    # Round trip cost = 1% of 100 = 1.00 per share: risk 6, reward 14.
    report = evaluate(market=costly_market)
    assert report.verdict is RiskVerdict.REVISE
    assert check(report, odin.REWARD_TO_RISK_CHECK).actual == "2.33"


def test_units_are_rounded_down_so_risk_never_exceeds_the_limit():
    report = evaluate(chart_setup=setup(stop=93.0, target=120.0))
    assert report.plan.units == 142  # 1,000 / 7 = 142.9
    assert report.plan.risk_amount <= 1000.0


def test_fractional_markets_keep_fractional_units():
    fractional_market = NO_COST_MARKET.model_copy(update={"allows_fractional_units": True})
    report = evaluate(chart_setup=setup(stop=93.0, target=120.0), market=fractional_market)
    assert report.plan.units == pytest.approx(142.857142, abs=1e-6)


def test_reward_to_risk_below_the_floor_asks_for_a_revision_not_a_trade():
    report = evaluate(chart_setup=setup(target=108.0))
    assert report.verdict is RiskVerdict.REVISE
    assert report.plan is None
    assert report.failed_checks == [odin.REWARD_TO_RISK_CHECK]
    assert "at least 2.75" in report.revision


def test_kill_switch_rejects_everything():
    report = evaluate(limits=RiskLimits(kill_switch_engaged=True))
    assert report.verdict is RiskVerdict.REJECT
    assert "Kill switch" in report.failed_checks
    assert report.plan is None


def test_data_blockers_reject_even_a_well_formed_setup():
    report = evaluate(blockers=["Stale daily data"])
    assert report.verdict is RiskVerdict.REJECT
    assert report.failed_checks == ["Data quality"]


def test_an_open_idea_blocks_a_second_one():
    report = evaluate(account_snapshot=account(open_idea_count=1))
    assert report.verdict is RiskVerdict.REJECT
    assert "Open ideas" in report.failed_checks


def test_missing_levels_are_rejected_for_lack_of_an_invalidation():
    report = evaluate(chart_setup=chart_without_a_setup())
    assert report.verdict is RiskVerdict.REJECT
    assert report.failed_checks == ["Invalidation level set"]
    assert check(report, "Invalidation level set").actual == "missing"


def test_daily_loss_budget_counts_realised_losses_and_open_stops():
    # Budget 1,000; 600 lost and 300 at risk leaves 100 => 20 shares instead of 200.
    report = evaluate(
        account_snapshot=account(realized_loss_today=600.0, open_risk_amount=300.0),
        limits=RiskLimits(maximum_open_ideas=3),
    )
    assert report.verdict is RiskVerdict.APPROVE
    assert report.plan.units == 20
    assert check(report, "Daily loss budget").actual == "100.00 left"


def test_exhausted_daily_loss_budget_rejects():
    report = evaluate(account_snapshot=account(realized_loss_today=1_000.0))
    assert report.verdict is RiskVerdict.REJECT
    assert {"Daily loss budget", "Position size"} <= set(report.failed_checks)


def test_position_is_capped_by_its_share_of_equity():
    # A 0.5 stop would size 2,000 shares (200,000); the 20% cap allows 200.
    report = evaluate(chart_setup=setup(stop=99.5, target=102.0))
    assert report.plan.units == 200
    assert report.plan.notional == pytest.approx(20_000.0)


def test_position_never_uses_more_cash_than_the_account_holds():
    report = evaluate(account_snapshot=account(cash=1_000.0))
    assert report.plan.units == 10
    assert check(report, "Leverage").passed


def test_account_too_small_for_one_unit_is_rejected():
    report = evaluate(account_snapshot=account(equity=400.0, cash=400.0))
    assert report.verdict is RiskVerdict.REJECT
    assert "Position size" in report.failed_checks
