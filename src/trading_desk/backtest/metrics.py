"""Performance measures for one engine run, including the unflattering ones.

A win rate alone says nothing: these metrics always pair it with the size of wins
and losses in R (multiples of planned risk), the drawdown, and a buy-and-hold
benchmark over the same bars.
"""

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from trading_desk.engine import EngineResult
from trading_desk.markets import MarketProfile
from trading_desk.models import Candle, Model

DECISION_KINDS = ("TRADE", "WAIT", "REJECT")


class PerformanceMetrics(Model):
    start: datetime | None
    end: datetime | None
    bars: int
    starting_equity: float
    final_equity: float
    total_return: float
    benchmark_symbol: str
    benchmark_return: float | None
    max_drawdown: float
    sharpe_ratio: float | None
    exposure: float
    trade_count: int
    win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    average_win_r: float | None
    average_loss_r: float | None
    total_costs: float
    exit_reason_counts: dict[str, int]
    decision_counts: dict[str, int]
    rejection_reason_counts: dict[str, int]


def maximum_drawdown(equity_values: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a negative fraction (0.0 when equity never fell)."""
    peak_equity, worst_drawdown = -math.inf, 0.0
    for equity in equity_values:
        peak_equity = max(peak_equity, equity)
        worst_drawdown = min(worst_drawdown, equity / peak_equity - 1)
    return worst_drawdown


def annualised_sharpe_ratio(equity_values: Sequence[float], periods_per_year: int) -> float | None:
    """Mean over standard deviation of per-bar returns, annualised; risk-free rate 0.

    None when there are fewer than three bars or returns never varied.
    """
    period_returns = [
        later_equity / earlier_equity - 1
        for earlier_equity, later_equity in zip(equity_values, equity_values[1:], strict=False)
    ]
    if len(period_returns) < 2:
        return None
    return_deviation = statistics.stdev(period_returns)
    if return_deviation == 0:
        return None
    return statistics.fmean(period_returns) / return_deviation * math.sqrt(periods_per_year)


def buy_and_hold_return(
    benchmark_candles: Sequence[Candle], start: datetime | None, end: datetime | None
) -> float | None:
    """Close-to-close return of holding the benchmark across the same window."""
    if start is None or end is None:
        return None
    window_closes = [
        candle.close for candle in benchmark_candles if start <= candle.timestamp <= end
    ]
    if len(window_closes) < 2:
        return None
    return window_closes[-1] / window_closes[0] - 1


def compute_metrics(
    engine_result: EngineResult,
    market_profile: MarketProfile,
    benchmark_candles: Sequence[Candle] = (),
) -> PerformanceMetrics:
    equity_curve = engine_result.equity_curve
    equity_values = [engine_result.starting_equity] + [point.equity for point in equity_curve]
    start = equity_curve[0].timestamp if equity_curve else None
    end = equity_curve[-1].timestamp if equity_curve else None
    trades = engine_result.closed_trades
    winning_trades = [trade for trade in trades if trade.profit_and_loss > 0]
    losing_trades = [trade for trade in trades if trade.profit_and_loss < 0]
    gross_profit = sum(trade.profit_and_loss for trade in winning_trades)
    gross_loss = -sum(trade.profit_and_loss for trade in losing_trades)
    decision_events = [event for event in engine_result.events if event.agent == "PICARD"]
    return PerformanceMetrics(
        start=start,
        end=end,
        bars=engine_result.bars_processed,
        starting_equity=engine_result.starting_equity,
        final_equity=engine_result.final_equity,
        total_return=engine_result.final_equity / engine_result.starting_equity - 1,
        benchmark_symbol=market_profile.benchmark_symbol,
        benchmark_return=buy_and_hold_return(benchmark_candles, start, end),
        max_drawdown=maximum_drawdown(equity_values),
        sharpe_ratio=annualised_sharpe_ratio(
            equity_values, market_profile.trading_periods_per_year
        ),
        exposure=(
            sum(point.has_open_position for point in equity_curve) / len(equity_curve)
            if equity_curve
            else 0.0
        ),
        trade_count=len(trades),
        win_rate=len(winning_trades) / len(trades) if trades else None,
        # Undefined without a losing trade; reported as None rather than infinity.
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        expectancy=(
            sum(trade.profit_and_loss for trade in trades) / len(trades) if trades else None
        ),
        average_win_r=(
            statistics.fmean(trade.r_multiple for trade in winning_trades)
            if winning_trades
            else None
        ),
        average_loss_r=(
            statistics.fmean(trade.r_multiple for trade in losing_trades) if losing_trades else None
        ),
        total_costs=round(sum(trade.total_cost for trade in trades), 2),
        exit_reason_counts=dict(Counter(trade.exit_reason.value for trade in trades)),
        decision_counts={
            kind: sum(event.kind == kind for event in decision_events) for kind in DECISION_KINDS
        },
        rejection_reason_counts=dict(
            Counter(event.reason for event in decision_events if event.kind == "REJECT")
        ),
    )
