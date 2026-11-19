"""Trade review: the breakdowns, the weekly report, the `desk report` command and the
review API. Everything is computed from saved trade records.
"""

import sys
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from trading_desk import cli
from trading_desk.engine import run_engine
from trading_desk.markets import US_EQUITIES
from trading_desk.models import ClosedTrade, DeskEvent, ExitReason, SetupStrategy, TradeSide
from trading_desk.paper import PaperVenue
from trading_desk.paper.journal import Journal
from trading_desk.review import (
    build_weekly_report,
    holding_time_label,
    render_weekly_report,
    review_run,
    review_trades,
    week_containing,
)
from trading_desk.server import DeskSession, ReplaySettings, create_app
from trading_desk.tools.market_data import FixtureProvider

MONDAY = datetime(2025, 3, 10, 21, tzinfo=UTC)
FIRST_BAR = datetime(2025, 1, 2, 21, tzinfo=UTC)
LAST_BAR = datetime(2025, 4, 23, 21, tzinfo=UTC)


def make_trade(profit_and_loss, **overrides):
    """Test helper (not a test): builds a closed trade that risked $500, so its R multiple is
    the profit divided by 500.
    """
    return ClosedTrade(
        **{
            "symbol": "AAPL",
            "side": TradeSide.LONG,
            "strategy": SetupStrategy.BREAKOUT,
            "units": 10,
            "entry_price": 100.0,
            "exit_price": 100.0 + profit_and_loss / 10,
            "planned_stop": 50.0,
            "planned_target": 250.0,
            "planned_risk_amount": 500.0,
            "opened_at": MONDAY,
            "closed_at": MONDAY + timedelta(days=2),
            "bars_held": 2,
            "exit_reason": ExitReason.TARGET if profit_and_loss > 0 else ExitReason.STOP,
            "total_cost": 2.0,
            "profit_and_loss": profit_and_loss,
            "r_multiple": profit_and_loss / 500.0,
            "maximum_favorable_excursion_r": max(profit_and_loss / 500.0, 0.0),
            "maximum_adverse_excursion_r": min(profit_and_loss / 500.0, 0.0),
            **overrides,
        }
    )


def make_mixed_trades():
    """Test helper (not a test): two winners and two losers across strategies and symbols."""
    return [
        make_trade(1500.0),
        make_trade(-500.0, symbol="MSFT", bars_held=4),
        make_trade(500.0, strategy=SetupStrategy.TREND_CONTINUATION, bars_held=12),
        make_trade(
            -300.0,
            strategy=SetupStrategy.TREND_CONTINUATION,
            side=TradeSide.SHORT,
            exit_reason=ExitReason.STALLED,
            bars_held=7,
        ),
    ]


def test_the_review_reports_wins_losses_and_averages_in_dollars_and_r():
    """Verifies: totals, win rate, average win and loss in dollars and R, expectancy, costs,
    holding time and the biggest win and loss are computed from the trades.
    """
    trade_review = review_trades(make_mixed_trades())
    assert (trade_review.trade_count, trade_review.win_count, trade_review.loss_count) == (4, 2, 2)
    assert trade_review.win_rate == 0.5
    assert trade_review.profit_and_loss == 1200.0
    assert trade_review.total_costs == 8.0
    assert (trade_review.average_win_amount, trade_review.average_loss_amount) == (1000.0, -400.0)
    assert (trade_review.average_win_r, trade_review.average_loss_r) == (2.0, -0.8)
    assert trade_review.expectancy_r == pytest.approx(0.6)
    assert trade_review.average_bars_held == 6.25
    assert trade_review.largest_win.profit_and_loss == 1500.0
    assert trade_review.largest_loss.profit_and_loss == -500.0
    assert [reviewed.trade_number for reviewed in trade_review.trades] == [0, 1, 2, 3]


def test_the_review_breaks_results_down_by_strategy_symbol_side_exit_and_holding_time():
    """Verifies: each breakdown groups the right trades, the most profitable group comes
    first, and holding-time groups stay in order of length.
    """
    trade_review = review_trades(make_mixed_trades())
    by_strategy = {group.label: group for group in trade_review.by_strategy}
    assert [group.label for group in trade_review.by_strategy] == [
        "BREAKOUT",
        "TREND_CONTINUATION",
    ]
    assert (by_strategy["BREAKOUT"].trade_count, by_strategy["BREAKOUT"].profit_and_loss) == (
        2,
        1000.0,
    )
    assert by_strategy["TREND_CONTINUATION"].win_rate == 0.5
    assert by_strategy["TREND_CONTINUATION"].average_r == pytest.approx(0.2)
    assert {group.label: group.trade_count for group in trade_review.by_symbol} == {
        "AAPL": 3,
        "MSFT": 1,
    }
    assert {group.label: group.profit_and_loss for group in trade_review.by_side} == {
        "LONG": 1500.0,
        "SHORT": -300.0,
    }
    assert {group.label: group.trade_count for group in trade_review.by_exit_reason} == {
        "TARGET": 2,
        "STOP": 1,
        "STALLED": 1,
    }
    assert [group.label for group in trade_review.by_holding_time] == [
        "1-2 sessions",
        "3-5 sessions",
        "6-10 sessions",
        "11+ sessions",
    ]
    assert holding_time_label(make_trade(1.0, bars_held=0)) == "1-2 sessions"
    assert holding_time_label(make_trade(1.0, bars_held=400)) == "11+ sessions"


def test_the_review_counts_how_same_day_stop_and_target_exits_were_settled():
    """Verifies: the review says how many trades were settled by minute bars and how many
    still rest on the stop-first assumption, and the weekly page prints both.
    """
    trades = [
        make_trade(1500.0, same_bar_resolution="INTRADAY"),
        make_trade(-500.0, same_bar_resolution="INTRADAY"),
        make_trade(-500.0, same_bar_resolution="ASSUMED_STOP_FIRST"),
        make_trade(250.0),
    ]
    trade_review = review_trades(trades)
    assert trade_review.settled_intraday_count == 2
    assert trade_review.assumed_stop_first_count == 1
    journal = Journal()
    journal.record_event(
        DeskEvent(
            run_id="run-1",
            sequence=0,
            timestamp=MONDAY,
            symbol="AAPL",
            agent="PICARD",
            kind="TRADE",
            reason="approved",
            detail={},
        )
    )
    for trade in trades:
        journal.record_trade("run-1", trade)
    page = render_weekly_report(build_weekly_report(journal, "run-1"))
    assert "| Stop and target touched on one day: settled by minute bars | 2 |" in page
    assert "| Stop and target touched on one day: stop assumed first | 1 |" in page


def test_a_review_of_no_trades_shows_nothing_rather_than_zeroes():
    """Verifies: with no trades, every average is missing (None), not zero, and a break-even
    trade counts as a loss.
    """
    empty_review = review_trades([])
    assert empty_review.trade_count == 0
    assert empty_review.win_rate is None and empty_review.expectancy_r is None
    assert empty_review.average_win_amount is None and empty_review.largest_loss is None
    assert empty_review.by_strategy == [] and empty_review.trades == []
    break_even = review_trades([make_trade(0.0)])
    assert (break_even.win_count, break_even.loss_count) == (0, 1)
    assert break_even.average_win_amount is None


async def make_journalled_run():
    """Test helper (not a test): runs the fixture through the engine with a journal and
    returns the journal and the run's result.
    """
    journal = Journal()
    engine_result = await run_engine(
        ["AAPL"],
        FixtureProvider(),
        PaperVenue(100_000.0, US_EQUITIES),
        start=FIRST_BAR,
        end=LAST_BAR,
        journal=journal,
    )
    return journal, engine_result


async def test_a_closed_trade_is_linked_to_the_decision_that_opened_it():
    """Verifies: the journal finds the saved six-agent report behind a trade (the latest TRADE
    decision for that symbol before it opened), finds none for an unknown symbol, and the
    run review marks the trade as having one.
    """
    journal, engine_result = await make_journalled_run()
    (closed_trade,) = engine_result.closed_trades
    saved_decision = journal.decision_for_trade(engine_result.run_id, closed_trade)
    assert saved_decision["symbol"] == "AAPL"
    assert saved_decision["decision"]["action"] == "TRADE"
    assert datetime.fromisoformat(saved_decision["evaluated_as_of"]) < closed_trade.opened_at
    assert "data" not in saved_decision
    other_symbol = closed_trade.model_copy(update={"symbol": "MSFT"})
    assert journal.decision_for_trade(engine_result.run_id, other_symbol) is None
    opened_before_any_decision = closed_trade.model_copy(update={"opened_at": FIRST_BAR})
    assert journal.decision_for_trade(engine_result.run_id, opened_before_any_decision) is None
    assert journal.latest_run_id() == engine_result.run_id
    assert Journal().latest_run_id() is None

    run_review = review_run(journal, engine_result.run_id)
    assert [reviewed.has_saved_decision for reviewed in run_review.trades] == [True]


def test_weeks_run_monday_to_sunday():
    """Verifies: any day maps to the Monday and Sunday of its own week."""
    assert week_containing(date(2025, 3, 12)) == (date(2025, 3, 10), date(2025, 3, 16))
    assert week_containing(date(2025, 3, 10)) == (date(2025, 3, 10), date(2025, 3, 16))
    assert week_containing(date(2025, 3, 16)) == (date(2025, 3, 10), date(2025, 3, 16))


def make_weekly_journal():
    """Test helper (not a test): a journal with one run whose trades and decisions fall in two
    different weeks.
    """
    journal = Journal()
    decisions = [
        (MONDAY, "TRADE", "approved"),
        (MONDAY, "REJECT", "Invalidation level set"),
        (MONDAY + timedelta(days=1), "REJECT", "Invalidation level set"),
        (MONDAY + timedelta(days=1), "REJECT", "Reward to risk"),
        (MONDAY + timedelta(days=2), "WAIT", "Bearish news"),
        (MONDAY + timedelta(days=7), "REJECT", "Next week's rejection"),
    ]
    for sequence, (timestamp, kind, reason) in enumerate(decisions):
        journal.record_event(
            DeskEvent(
                run_id="run-1",
                sequence=sequence,
                timestamp=timestamp,
                symbol="AAPL",
                agent="PICARD",
                kind=kind,
                reason=reason,
                detail={},
            )
        )
    journal.record_trade("run-1", make_trade(1500.0))
    journal.record_trade("run-1", make_trade(-500.0, symbol="MSFT"))
    journal.record_trade("run-1", make_trade(250.0, closed_at=MONDAY + timedelta(days=8)))
    return journal


def test_the_weekly_report_covers_only_that_weeks_trades_and_decisions():
    """Verifies: a week's report counts the trades that closed in it and the decisions made in
    it, tallies why ideas were rejected, and defaults to the run's latest week.
    """
    journal = make_weekly_journal()
    first_week = build_weekly_report(journal, "run-1", date(2025, 3, 12))
    assert (first_week.week_start, first_week.week_end) == (date(2025, 3, 10), date(2025, 3, 16))
    assert first_week.review.trade_count == 2
    assert first_week.review.profit_and_loss == 1000.0
    assert first_week.decision_counts == {"TRADE": 1, "WAIT": 1, "REJECT": 3}
    assert first_week.rejection_reason_counts == {
        "Invalidation level set": 2,
        "Reward to risk": 1,
    }
    assert first_week.wait_reason_counts == {"Bearish news": 1}

    latest_week = build_weekly_report(journal, "run-1")
    assert latest_week.week_start == date(2025, 3, 17)
    assert latest_week.review.trade_count == 1
    assert latest_week.decision_counts == {"TRADE": 0, "WAIT": 0, "REJECT": 1}
    with pytest.raises(ValueError, match="no events"):
        build_weekly_report(journal, "unknown-run")


def test_the_weekly_report_reads_as_a_page_with_dollar_amounts():
    """Verifies: the Markdown names the week, states results in dollars and R, lists each
    trade and each rejection reason, says the trades are paper, and a week with no trades
    says so plainly.
    """
    journal = make_weekly_journal()
    page = render_weekly_report(build_weekly_report(journal, "run-1", date(2025, 3, 12)))
    assert page.startswith("# Trading desk: week of 2025-03-10 to 2025-03-16")
    assert "Paper trades only; no order reached a broker." in page
    assert "| Trades closed | 2 |" in page
    assert "| Profit and loss after costs | $1,000.00 |" in page
    assert "| Average win | $1,500.00 (+3.00R) |" in page
    assert "| Average loss | -$500.00 (-1.00R) |" in page
    assert (
        "Biggest win: AAPL LONG BREAKOUT, $1,500.00 (+3.00R), closed 2025-03-12 on TARGET" in page
    )
    assert "| MSFT | 1 | 0 | 0% | -$500.00 | -1.00R | - | -$500.00 |" in page
    assert "5 decisions: 1 to trade, 1 to wait, 3 rejected." in page
    assert "| Invalidation level set | 2 |" in page
    assert "| Bearish news | 1 |" in page
    assert "## Model spend" in page

    empty_week = render_weekly_report(build_weekly_report(journal, "run-1", date(2025, 6, 2)))
    assert "No trade closed this week." in empty_week
    assert "| Win rate | - |" in empty_week
    assert "0 decisions" in empty_week
    assert "## Trades" not in empty_week


def run_report_cli(monkeypatch, *arguments):
    """Test helper (not a test): runs `trading-desk desk report` with the given arguments."""
    monkeypatch.setattr(sys, "argv", ["trading-desk", "desk", "report", *arguments])
    return cli.main()


async def test_desk_report_writes_the_latest_weeks_page_from_a_journal_file(
    monkeypatch, tmp_path, capsys
):
    """Verifies: `desk report` reads a journal file, writes the Markdown page to --output or
    stdout, and accepts a run id and a week.
    """
    journal_path = tmp_path / "journal.sqlite"
    journal = Journal(journal_path)
    engine_result = await run_engine(
        ["AAPL"],
        FixtureProvider(),
        PaperVenue(100_000.0, US_EQUITIES),
        start=FIRST_BAR,
        end=LAST_BAR,
        journal=journal,
    )
    journal.close()
    output_path = tmp_path / "week.md"
    exit_code = run_report_cli(
        monkeypatch, "--journal", str(journal_path), "--output", str(output_path)
    )
    assert exit_code == 0
    page = output_path.read_text()
    assert "week of 2025-04-21 to 2025-04-27" in page
    assert f"Run `{engine_result.run_id}`" in page
    assert "| Trades closed | 1 |" in page
    assert "END_OF_RUN" in page
    assert "trades closed=1" in capsys.readouterr().err

    exit_code = run_report_cli(
        monkeypatch,
        "--journal",
        str(journal_path),
        "--run-id",
        engine_result.run_id,
        "--week",
        "2025-03-12",
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "week of 2025-03-10 to 2025-03-16" in captured.out
    assert "No trade closed this week." in captured.out


def test_desk_report_explains_a_missing_journal_empty_journal_or_bad_week(
    monkeypatch, tmp_path, capsys
):
    """Verifies: a missing journal, an empty one, an unknown run and an unreadable date each
    exit 2 with a plain message instead of a stack trace.
    """
    missing_path = tmp_path / "nothing.sqlite"
    assert run_report_cli(monkeypatch, "--journal", str(missing_path)) == 2
    assert "no journal at" in capsys.readouterr().err

    empty_path = tmp_path / "empty.sqlite"
    Journal(empty_path).close()
    assert run_report_cli(monkeypatch, "--journal", str(empty_path)) == 2
    assert "the journal is empty" in capsys.readouterr().err
    assert run_report_cli(monkeypatch, "--journal", str(empty_path), "--run-id", "nope") == 2
    assert "no events for that run" in capsys.readouterr().err
    assert run_report_cli(monkeypatch, "--journal", str(empty_path), "--week", "last week") == 2
    assert "Configuration/output error" in capsys.readouterr().err


async def yield_once(seconds):
    """Test helper (not a test): a pacing wait that returns at once."""


def make_review_client(journal):
    """Test helper (not a test): an API client over a fixture session with the given journal."""
    desk_session = DeskSession(FixtureProvider(), sleep=yield_once, journal=journal)
    replay_defaults = ReplaySettings(
        symbols=["AAPL"], start=FIRST_BAR, end=LAST_BAR, bars_per_second=1000
    )
    return TestClient(create_app(desk_session, replay_defaults))


def wait_until_finished(client):
    """Test helper (not a test): polls the state until the replay has finished."""
    for _ in range(2000):
        if client.get("/api/state").json()["status"] == "FINISHED":
            return
    raise AssertionError("the replay never finished")


def test_the_review_api_reports_the_current_runs_trades_and_their_saved_decisions():
    """Verifies: before a run the review is empty; after one it holds the closed trade with
    its breakdowns, and the trade's saved decision report can be fetched by its number.
    """
    with make_review_client(Journal()) as client:
        idle_review = client.get("/api/review").json()
        assert (idle_review["run_id"], idle_review["review"]["trade_count"]) == (None, 0)
        assert client.get("/api/review/trades/0/decision").status_code == 404

        assert client.post("/api/replay/start", json={}).status_code == 202
        wait_until_finished(client)
        finished_review = client.get("/api/review").json()
        review = finished_review["review"]
        assert finished_review["run_id"] == client.get("/api/state").json()["run_id"]
        assert review["trade_count"] == 1
        assert review["trades"][0]["has_saved_decision"] is True
        assert review["trades"][0]["trade"]["symbol"] == "AAPL"
        assert [group["label"] for group in review["by_exit_reason"]] == ["END_OF_RUN"]

        decision = client.get("/api/review/trades/0/decision")
        assert decision.status_code == 200
        assert decision.json()["decision"]["action"] == "TRADE"
        assert decision.json()["risk"]["verdict"] == "APPROVE"
        assert client.get("/api/review/trades/1/decision").status_code == 404


def test_without_a_journal_the_review_still_works_but_has_no_saved_decisions():
    """Verifies: a server started without a journal reviews its trades from memory and
    answers 404 for a decision report it never kept.
    """
    with make_review_client(None) as client:
        client.post("/api/replay/start", json={})
        wait_until_finished(client)
        review = client.get("/api/review").json()["review"]
        assert review["trade_count"] == 1
        assert review["trades"][0]["has_saved_decision"] is False
        assert client.get("/api/review/trades/0/decision").status_code == 404
