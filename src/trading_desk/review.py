"""Reviewing past trades: the numbers behind the review page and the weekly report.

Everything here is arithmetic over `ClosedTrade` records and journal events. It
reads what the desk already saved; it never re-runs an agent or a model, and a
figure that cannot be computed (an average of no trades) is `None`, never zero.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta

from trading_desk.models import ClosedTrade, Model, SameBarResolution
from trading_desk.paper.journal import Journal

# Holding-time groups, in sessions held: (label, longest holding time included).
HOLDING_TIME_GROUPS: tuple[tuple[str, int | None], ...] = (
    ("1-2 sessions", 2),
    ("3-5 sessions", 5),
    ("6-10 sessions", 10),
    ("11+ sessions", None),
)
DECISION_KINDS = ("TRADE", "WAIT", "REJECT")


class GroupResult(Model):
    """Results for one slice of the trades (one strategy, one symbol, one exit reason...)."""

    label: str
    trade_count: int
    win_count: int
    win_rate: float | None
    profit_and_loss: float
    average_r: float | None
    average_win_amount: float | None
    average_loss_amount: float | None


class ReviewedTrade(Model):
    """One closed trade with its place in the list and whether its decision report was saved."""

    trade_number: int
    trade: ClosedTrade
    has_saved_decision: bool = False


class TradeReview(Model):
    """Everything the review page and the weekly report show about a set of closed trades."""

    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float | None
    profit_and_loss: float
    total_costs: float
    average_win_amount: float | None
    average_loss_amount: float | None
    average_win_r: float | None
    average_loss_r: float | None
    # Average result per trade in R: what one more trade like these has been worth.
    expectancy_r: float | None
    average_bars_held: float | None
    largest_win: ClosedTrade | None
    largest_loss: ClosedTrade | None
    # Trades whose closing bar touched both stop and target: how many were settled by minute
    # bars, and how many still rest on the assumption that the stop came first.
    settled_intraday_count: int = 0
    assumed_stop_first_count: int = 0
    by_strategy: list[GroupResult]
    by_symbol: list[GroupResult]
    by_side: list[GroupResult]
    by_exit_reason: list[GroupResult]
    by_holding_time: list[GroupResult]
    trades: list[ReviewedTrade]


def _average(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def is_win(trade: ClosedTrade) -> bool:
    """A trade wins when it made money after costs. Break-even counts as a loss."""
    return trade.profit_and_loss > 0


def summarise_group(label: str, trades: Sequence[ClosedTrade]) -> GroupResult:
    """Count, win rate, P&L and averages for one slice of the trades."""
    winning_amounts = [trade.profit_and_loss for trade in trades if is_win(trade)]
    losing_amounts = [trade.profit_and_loss for trade in trades if not is_win(trade)]
    return GroupResult(
        label=label,
        trade_count=len(trades),
        win_count=len(winning_amounts),
        win_rate=len(winning_amounts) / len(trades) if trades else None,
        profit_and_loss=sum(trade.profit_and_loss for trade in trades),
        average_r=_average([trade.r_multiple for trade in trades]),
        average_win_amount=_average(winning_amounts),
        average_loss_amount=_average(losing_amounts),
    )


def group_trades(
    trades: Sequence[ClosedTrade], label_of: Callable[[ClosedTrade], str]
) -> list[GroupResult]:
    """Split the trades by a label and summarise each group, most profitable first."""
    trades_by_label: dict[str, list[ClosedTrade]] = {}
    for trade in trades:
        trades_by_label.setdefault(label_of(trade), []).append(trade)
    return sorted(
        (summarise_group(label, group) for label, group in trades_by_label.items()),
        key=lambda group_result: (-group_result.profit_and_loss, group_result.label),
    )


def holding_time_label(trade: ClosedTrade) -> str:
    """Which holding-time group a trade falls in, by sessions held."""
    for label, longest_included in HOLDING_TIME_GROUPS:
        if longest_included is None or trade.bars_held <= longest_included:
            return label
    raise AssertionError("the last holding-time group has no upper limit")


def review_trades(
    trades: Sequence[ClosedTrade], trades_with_saved_decision: frozenset[int] = frozenset()
) -> TradeReview:
    """Build the full review of a list of closed trades, in the order they closed.

    `trades_with_saved_decision` holds the positions (0-based) of trades whose
    six-agent decision report can be opened from the journal.
    """
    overall = summarise_group("all", trades)
    winners = [trade for trade in trades if is_win(trade)]
    losers = [trade for trade in trades if not is_win(trade)]
    holding_time_order = {
        label: position for position, (label, _) in enumerate(HOLDING_TIME_GROUPS)
    }
    return TradeReview(
        trade_count=overall.trade_count,
        win_count=overall.win_count,
        loss_count=len(losers),
        win_rate=overall.win_rate,
        profit_and_loss=overall.profit_and_loss,
        total_costs=sum(trade.total_cost for trade in trades),
        average_win_amount=overall.average_win_amount,
        average_loss_amount=overall.average_loss_amount,
        average_win_r=_average([trade.r_multiple for trade in winners]),
        average_loss_r=_average([trade.r_multiple for trade in losers]),
        expectancy_r=overall.average_r,
        average_bars_held=_average([trade.bars_held for trade in trades]),
        largest_win=max(winners, key=lambda trade: trade.profit_and_loss, default=None),
        largest_loss=min(losers, key=lambda trade: trade.profit_and_loss, default=None),
        settled_intraday_count=sum(
            trade.same_bar_resolution is SameBarResolution.INTRADAY for trade in trades
        ),
        assumed_stop_first_count=sum(
            trade.same_bar_resolution is SameBarResolution.ASSUMED_STOP_FIRST for trade in trades
        ),
        by_strategy=group_trades(trades, lambda trade: trade.strategy.value),
        by_symbol=group_trades(trades, lambda trade: trade.symbol),
        by_side=group_trades(trades, lambda trade: trade.side.value),
        by_exit_reason=group_trades(trades, lambda trade: trade.exit_reason.value),
        by_holding_time=sorted(
            group_trades(trades, holding_time_label),
            key=lambda group_result: holding_time_order[group_result.label],
        ),
        trades=[
            ReviewedTrade(
                trade_number=trade_number,
                trade=trade,
                has_saved_decision=trade_number in trades_with_saved_decision,
            )
            for trade_number, trade in enumerate(trades)
        ],
    )


def review_run(journal: Journal, run_id: str) -> TradeReview:
    """Review every closed trade of one journalled run, marking those with a saved decision."""
    trades = journal.trades(run_id)
    return review_trades(
        trades,
        frozenset(
            trade_number
            for trade_number, trade in enumerate(trades)
            if journal.decision_for_trade(run_id, trade) is not None
        ),
    )


# -- the weekly report ---------------------------------------------------------


class WeeklyReport(Model):
    """One week of one run: the trades that closed in it and what the desk decided."""

    run_id: str
    week_start: date
    week_end: date
    review: TradeReview
    decision_counts: dict[str, int]
    rejection_reason_counts: dict[str, int]
    wait_reason_counts: dict[str, int]


def week_containing(day: date) -> tuple[date, date]:
    """The Monday-to-Sunday week that contains a day."""
    week_start = day - timedelta(days=day.weekday())
    return week_start, week_start + timedelta(days=6)


def build_weekly_report(
    journal: Journal, run_id: str, day_in_week: date | None = None
) -> WeeklyReport:
    """Collect one week of a run from the journal.

    Weeks follow the bars' own dates, so a replay of 2025 is reported in 2025's
    weeks. Without `day_in_week` the run's most recent week is used.
    """
    events = journal.events(run_id)
    if not events:
        raise ValueError("the journal has no events for that run")
    week_start, week_end = week_containing(day_in_week or events[-1].timestamp.date())
    window_start = datetime.combine(week_start, time.min, UTC)
    window_end = datetime.combine(week_end + timedelta(days=1), time.min, UTC)
    all_trades = journal.trades(run_id)
    trade_numbers_in_week = [
        trade_number
        for trade_number, trade in enumerate(all_trades)
        if window_start <= trade.closed_at < window_end
    ]
    trades_in_week = [all_trades[trade_number] for trade_number in trade_numbers_in_week]
    events_in_week = [event for event in events if window_start <= event.timestamp < window_end]
    decisions_in_week = [event for event in events_in_week if event.agent == "PICARD"]

    def reason_counts(kind: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in decisions_in_week:
            if event.kind == kind:
                counts[event.reason] = counts.get(event.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    return WeeklyReport(
        run_id=run_id,
        week_start=week_start,
        week_end=week_end,
        review=review_trades(
            trades_in_week,
            frozenset(
                position
                for position, trade in enumerate(trades_in_week)
                if journal.decision_for_trade(run_id, trade) is not None
            ),
        ),
        decision_counts={
            kind: sum(1 for event in decisions_in_week if event.kind == kind)
            for kind in DECISION_KINDS
        },
        rejection_reason_counts=reason_counts("REJECT"),
        wait_reason_counts=reason_counts("WAIT"),
    )


def _money(amount: float | None) -> str:
    if amount is None:
        return "-"
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


def _r(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}R"


def _percent(fraction: float | None) -> str:
    return "-" if fraction is None else f"{fraction:.0%}"


def _group_table(heading: str, groups: Sequence[GroupResult]) -> list[str]:
    """Markdown lines for one breakdown table (by strategy, symbol, exit reason...)."""
    lines = [
        f"### {heading}",
        "",
        "| | Trades | Wins | Win rate | P&L | Average R | Average win | Average loss |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {group.label} | {group.trade_count} | {group.win_count} | "
        f"{_percent(group.win_rate)} | {_money(group.profit_and_loss)} | {_r(group.average_r)} | "
        f"{_money(group.average_win_amount)} | {_money(group.average_loss_amount)} |"
        for group in groups
    ]
    return [*lines, ""]


def _trade_line(trade: ClosedTrade) -> str:
    return (
        f"{trade.symbol} {trade.side.value} {trade.strategy.value}, "
        f"{_money(trade.profit_and_loss)} ({_r(trade.r_multiple)}), "
        f"closed {trade.closed_at.date().isoformat()} on {trade.exit_reason.value}"
    )


def _count_table(heading: str, first_column: str, counts: dict[str, int]) -> list[str]:
    if not counts:
        return []
    lines = [f"### {heading}", "", f"| {first_column} | Times |", "|---|---:|"]
    lines += [f"| {reason} | {count} |" for reason, count in counts.items()]
    return [*lines, ""]


def render_weekly_report(report: WeeklyReport) -> str:
    """Write the weekly report as one Markdown page."""
    review = report.review
    lines = [
        f"# Trading desk: week of {report.week_start.isoformat()} to {report.week_end.isoformat()}",
        "",
        f"Run `{report.run_id}`. Paper trades only; no order reached a broker. "
        "Research record, not investment advice.",
        "",
        "## Summary",
        "",
        "| | |",
        "|---|---:|",
        f"| Trades closed | {review.trade_count} |",
        f"| Wins / losses | {review.win_count} / {review.loss_count} |",
        f"| Win rate | {_percent(review.win_rate)} |",
        f"| Profit and loss after costs | {_money(review.profit_and_loss)} |",
        f"| Trading costs | {_money(review.total_costs)} |",
        f"| Average win | {_money(review.average_win_amount)} ({_r(review.average_win_r)}) |",
        f"| Average loss | {_money(review.average_loss_amount)} ({_r(review.average_loss_r)}) |",
        f"| Average result per trade | {_r(review.expectancy_r)} |",
        "| Average sessions held | "
        + ("-" if review.average_bars_held is None else f"{review.average_bars_held:.1f}")
        + " |",
        f"| Stop and target touched on one day: settled by minute bars | "
        f"{review.settled_intraday_count} |",
        f"| Stop and target touched on one day: stop assumed first | "
        f"{review.assumed_stop_first_count} |",
        "",
    ]
    if review.trade_count == 0:
        lines += ["No trade closed this week.", ""]
    else:
        lines += [
            "Biggest win: " + (_trade_line(review.largest_win) if review.largest_win else "none."),
            "",
            "Biggest loss: "
            + (_trade_line(review.largest_loss) if review.largest_loss else "none."),
            "",
            "## Results",
            "",
            *_group_table("By strategy", review.by_strategy),
            *_group_table("By symbol", review.by_symbol),
            *_group_table("By exit reason", review.by_exit_reason),
            *_group_table("By holding time", review.by_holding_time),
            "## Trades",
            "",
            "| # | Symbol | Side | Strategy | Opened | Closed | Sessions | Exit | P&L | R | "
            "Best | Worst |",
            "|---:|---|---|---|---|---|---:|---|---:|---:|---:|---:|",
            *[
                f"| {reviewed.trade_number + 1} | {reviewed.trade.symbol} | "
                f"{reviewed.trade.side.value} | {reviewed.trade.strategy.value} | "
                f"{reviewed.trade.opened_at.date().isoformat()} | "
                f"{reviewed.trade.closed_at.date().isoformat()} | {reviewed.trade.bars_held} | "
                f"{reviewed.trade.exit_reason.value} | {_money(reviewed.trade.profit_and_loss)} | "
                f"{_r(reviewed.trade.r_multiple)} | "
                f"{_r(reviewed.trade.maximum_favorable_excursion_r)} | "
                f"{_r(reviewed.trade.maximum_adverse_excursion_r)} |"
                for reviewed in review.trades
            ],
            "",
        ]
    decision_total = sum(report.decision_counts.values())
    lines += [
        "## What the desk decided",
        "",
        f"{decision_total} decisions: {report.decision_counts['TRADE']} to trade, "
        f"{report.decision_counts['WAIT']} to wait, {report.decision_counts['REJECT']} rejected.",
        "",
        *_count_table("Why ideas were rejected", "Failed rule", report.rejection_reason_counts),
        *_count_table("Why the desk waited", "Reason", report.wait_reason_counts),
        "## Model spend",
        "",
        "Not recorded in the journal. Each metered model call and its cost is in the log "
        "(`logs/trading-desk.log`); a run on deterministic rules costs $0.",
        "",
    ]
    return "\n".join(lines)
