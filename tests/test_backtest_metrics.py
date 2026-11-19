from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.backtest.metrics import (
    annualised_sharpe_ratio,
    buy_and_hold_return,
    compute_metrics,
    maximum_drawdown,
)
from trading_desk.engine import EngineResult, EquityPoint
from trading_desk.markets import US_EQUITIES
from trading_desk.models import Candle, ClosedTrade, DeskEvent, ExitReason

START = datetime(2025, 1, 2, 21, tzinfo=UTC)


def make_bar_time(offset):
    """Test helper (not a test): builds bar time for the tests below."""
    return START + timedelta(days=offset)


def make_closed_trade(
    profit_and_loss,
    r_multiple,
    exit_reason=ExitReason.TARGET,
    total_cost=1.0,
    strategy="TREND_CONTINUATION",
    bars_held=1,
):
    """Test helper (not a test): builds closed trade for the tests below."""
    return ClosedTrade(
        symbol="AAPL",
        strategy=strategy,
        units=10,
        entry_price=100.0,
        exit_price=101.0,
        planned_stop=95.0,
        planned_target=115.0,
        planned_risk_amount=50.0,
        opened_at=make_bar_time(0),
        closed_at=make_bar_time(1),
        bars_held=bars_held,
        maximum_favorable_excursion_r=max(r_multiple, 0.0),
        maximum_adverse_excursion_r=min(r_multiple, 0.0),
        exit_reason=exit_reason,
        total_cost=total_cost,
        profit_and_loss=profit_and_loss,
        r_multiple=r_multiple,
    )


def make_decision_event(sequence, kind, reason="approved", agent="PICARD"):
    """Test helper (not a test): builds decision event for the tests below."""
    return DeskEvent(
        run_id="run",
        sequence=sequence,
        timestamp=make_bar_time(0),
        symbol="AAPL",
        agent=agent,
        kind=kind,
        reason=reason,
    )


def make_engine_result(equities, exposures=None, trades=(), events=()):
    """Test helper (not a test): builds engine result for the tests below."""
    exposures = exposures or [False] * len(equities)
    return EngineResult(
        run_id="run",
        starting_equity=100.0,
        final_equity=equities[-1] if equities else 100.0,
        equity_curve=[
            EquityPoint(timestamp=make_bar_time(index), equity=equity, has_open_position=exposure)
            for index, (equity, exposure) in enumerate(zip(equities, exposures, strict=True))
        ],
        closed_trades=list(trades),
        events=list(events),
        bars_processed=len(equities),
    )


def make_candle(offset, close):
    """Test helper (not a test): builds candle for the tests below."""
    return Candle(
        timestamp=make_bar_time(offset),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1,
    )


def test_maximum_drawdown_measures_the_worst_fall_from_a_peak():
    """Verifies: maximum drawdown measures the worst fall from a peak."""
    assert maximum_drawdown([100, 120, 90, 110, 130, 117]) == pytest.approx(-0.25)
    assert maximum_drawdown([100, 101, 102]) == 0.0
    assert maximum_drawdown([]) == 0.0


def test_sharpe_ratio_is_annualised_and_undefined_without_variation():
    # Returns of 10% then 20%: mean 0.15, sample deviation 0.0707107.
    """Verifies: sharpe ratio is annualised and undefined without variation."""
    expected_ratio = 0.15 / 0.0707106781 * 252**0.5
    assert annualised_sharpe_ratio([100, 110, 132], 252) == pytest.approx(expected_ratio)
    assert annualised_sharpe_ratio([100, 90, 99], 252) == pytest.approx(0.0, abs=1e-9)
    assert annualised_sharpe_ratio([100, 100, 100], 252) is None
    assert annualised_sharpe_ratio([100, 101], 252) is None


def test_buy_and_hold_return_uses_only_the_window():
    """Verifies: buy and hold return uses only the window."""
    candles = [
        make_candle(0, 50.0),
        make_candle(1, 100.0),
        make_candle(2, 110.0),
        make_candle(3, 200.0),
    ]
    assert buy_and_hold_return(candles, make_bar_time(1), make_bar_time(2)) == pytest.approx(0.10)
    assert buy_and_hold_return(candles, make_bar_time(3), make_bar_time(3)) is None
    assert buy_and_hold_return(candles, None, None) is None


def test_metrics_report_wins_and_losses_in_money_and_in_r():
    """Verifies: metrics report wins and losses in money and in r."""
    result = make_engine_result(
        [100, 104, 102, 108],
        exposures=[False, True, True, False],
        trades=[
            make_closed_trade(60.0, 2.0),
            make_closed_trade(-25.0, -1.0, ExitReason.STOP),
            make_closed_trade(-25.0, -1.0, ExitReason.STOP),
            make_closed_trade(30.0, 1.0, ExitReason.END_OF_RUN),
        ],
        events=[
            make_decision_event(0, "TRADE"),
            make_decision_event(1, "REJECT", "Open ideas"),
            make_decision_event(2, "REJECT", "Open ideas"),
            make_decision_event(3, "WAIT", "contested"),
            make_decision_event(4, "SKIP", "position_open", agent="ENGINE"),
        ],
    )
    metrics = compute_metrics(result, US_EQUITIES, [make_candle(0, 100.0), make_candle(3, 103.0)])
    assert metrics.total_return == pytest.approx(0.08)
    assert metrics.benchmark_symbol == "SPY"
    assert metrics.benchmark_return == pytest.approx(0.03)
    assert metrics.max_drawdown == pytest.approx(102 / 104 - 1)
    assert metrics.exposure == 0.5
    assert metrics.trade_count == 4
    assert metrics.win_rate == 0.5
    assert metrics.profit_factor == pytest.approx(90 / 50)
    assert metrics.expectancy == pytest.approx(10.0)
    assert metrics.average_win_r == pytest.approx(1.5)
    assert metrics.average_loss_r == pytest.approx(-1.0)
    assert metrics.total_costs == 4.0
    assert metrics.exit_reason_counts == {"TARGET": 1, "STOP": 2, "END_OF_RUN": 1}
    assert metrics.decision_counts == {"TRADE": 1, "WAIT": 1, "REJECT": 2}
    assert metrics.rejection_reason_counts == {"Open ideas": 2}


def test_metrics_for_a_run_without_trades_or_bars_are_honest_blanks():
    """Verifies: metrics for a run without trades or bars are honest blanks."""
    metrics = compute_metrics(make_engine_result([]), US_EQUITIES)
    assert metrics.total_return == 0.0
    assert metrics.trade_count == 0
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.expectancy is None
    assert metrics.benchmark_return is None
    assert metrics.sharpe_ratio is None
    assert metrics.exposure == 0.0


def test_profit_factor_is_undefined_when_nothing_lost():
    """Verifies: profit factor is undefined when nothing lost."""
    metrics = compute_metrics(
        make_engine_result([100, 110], trades=[make_closed_trade(10.0, 1.0)]), US_EQUITIES
    )
    assert metrics.profit_factor is None
    assert metrics.average_loss_r is None
    assert metrics.win_rate == 1.0


def test_average_win_and_loss_are_reported_in_dollars_with_the_extremes():
    """Verifies: the scorecard gives the average winning and losing trade in dollars, the
    single largest win and loss, and the average holding time, not only R multiples.
    """
    trades = [
        make_closed_trade(900.0, 3.0, bars_held=10),
        make_closed_trade(300.0, 1.0, bars_held=4),
        make_closed_trade(-250.0, -1.0, ExitReason.STOP, bars_held=2),
        make_closed_trade(-150.0, -0.6, ExitReason.STOP, bars_held=4),
    ]
    metrics = compute_metrics(make_engine_result([100, 110], trades=trades), US_EQUITIES)
    assert metrics.average_win_amount == 600.0
    assert metrics.average_loss_amount == -200.0
    assert metrics.largest_win_amount == 900.0
    assert metrics.largest_loss_amount == -250.0
    assert metrics.average_bars_held == 5.0


def test_dollar_averages_are_missing_not_zero_when_there_is_nothing_to_average():
    """Verifies: with no trades, or no losing trades, the dollar statistics are None."""
    no_trades = compute_metrics(make_engine_result([100, 100]), US_EQUITIES)
    assert no_trades.average_win_amount is None
    assert no_trades.average_loss_amount is None
    assert no_trades.largest_loss_amount is None
    assert no_trades.average_bars_held is None
    assert no_trades.results_by_strategy == {}
    only_wins = compute_metrics(
        make_engine_result([100, 110], trades=[make_closed_trade(10.0, 1.0)]), US_EQUITIES
    )
    assert only_wins.average_win_amount == 10.0
    assert only_wins.average_loss_amount is None


def test_results_are_broken_down_by_the_strategy_that_opened_each_trade():
    """Verifies: each strategy gets its own trade count, win rate, profit and average R, so
    one strategy's results cannot hide another's.
    """
    trades = [
        make_closed_trade(600.0, 2.0, strategy="BREAKOUT"),
        make_closed_trade(-300.0, -1.0, ExitReason.STOP, strategy="BREAKOUT"),
        make_closed_trade(450.0, 1.5, strategy="BOLLINGER_REVERSION"),
    ]
    metrics = compute_metrics(make_engine_result([100, 110], trades=trades), US_EQUITIES)
    assert list(metrics.results_by_strategy) == ["BOLLINGER_REVERSION", "BREAKOUT"]
    breakout = metrics.results_by_strategy["BREAKOUT"]
    assert (breakout.trade_count, breakout.win_rate) == (2, 0.5)
    assert breakout.profit_and_loss == 300.0
    assert breakout.average_r == 0.5
    assert metrics.results_by_strategy["BOLLINGER_REVERSION"].win_rate == 1.0
