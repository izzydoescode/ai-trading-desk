from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.models import ClosedTrade, DeskEvent, ExitReason
from trading_desk.paper.journal import Journal

START = datetime(2025, 3, 3, 21, tzinfo=UTC)


def make_desk_event(
    sequence, kind="SKIP", reason="close below SMA50", run_id="run-1", symbol="NVDA"
):
    """Test helper (not a test): builds desk event for the tests below."""
    return DeskEvent(
        run_id=run_id,
        sequence=sequence,
        timestamp=START + timedelta(minutes=sequence),
        symbol=symbol,
        agent="HAWKEYE",
        kind=kind,
        reason=reason,
        detail={"score": 0.1, "watch": False},
    )


def make_closed_trade(profit_and_loss=100.0):
    """Test helper (not a test): builds closed trade for the tests below."""
    return ClosedTrade(
        symbol="AAPL",
        strategy="BREAKOUT",
        units=10,
        entry_price=100.0,
        exit_price=110.0,
        planned_stop=95.0,
        planned_target=110.0,
        planned_risk_amount=50.0,
        opened_at=START,
        closed_at=START + timedelta(days=2),
        bars_held=2,
        maximum_favorable_excursion_r=2.0,
        maximum_adverse_excursion_r=-0.4,
        exit_reason=ExitReason.TARGET,
        total_cost=0.0,
        profit_and_loss=profit_and_loss,
        r_multiple=profit_and_loss / 50.0,
    )


def test_events_round_trip_in_order_and_are_isolated_per_run():
    """Verifies: events round trip in order and are isolated per run."""
    journal = Journal()
    for sequence in range(3):
        journal.record_event(make_desk_event(sequence))
    journal.record_event(make_desk_event(0, run_id="run-2"))
    events = journal.events("run-1")
    assert [event.sequence for event in events] == [0, 1, 2]
    assert events[0] == make_desk_event(0)
    assert len(journal.events("run-2")) == 1
    assert journal.events("missing-run") == []


def test_limit_returns_the_most_recent_events_still_in_order():
    """Verifies: limit returns the most recent events still in order."""
    journal = Journal()
    for sequence in range(5):
        journal.record_event(make_desk_event(sequence))
    assert [event.sequence for event in journal.events("run-1", limit=2)] == [3, 4]


def test_trades_round_trip_in_the_order_they_closed():
    """Verifies: trades round trip in the order they closed."""
    journal = Journal()
    journal.record_trade("run-1", make_closed_trade(100.0))
    journal.record_trade("run-1", make_closed_trade(-50.0))
    assert [trade.profit_and_loss for trade in journal.trades("run-1")] == [100.0, -50.0]
    assert journal.trades("run-2") == []


def test_reason_counts_show_where_candidates_are_lost():
    """Verifies: reason counts show where candidates are lost."""
    journal = Journal()
    journal.record_event(make_desk_event(0, reason="close below SMA50"))
    journal.record_event(make_desk_event(1, reason="close below SMA50"))
    journal.record_event(make_desk_event(2, reason="position_open"))
    journal.record_event(make_desk_event(3, kind="TRADE", reason="approved"))
    assert journal.reason_counts("run-1", "SKIP") == {"close below SMA50": 2, "position_open": 1}


def test_journal_persists_to_a_file_and_creates_its_folder(tmp_path):
    """Verifies: journal persists to a file and creates its folder."""
    database_path = tmp_path / "nested" / "journal.sqlite"
    journal = Journal(database_path)
    journal.record_event(make_desk_event(0))
    journal.close()
    reopened_journal = Journal(database_path)
    assert len(reopened_journal.events("run-1")) == 1


def test_values_are_stored_as_data_never_as_sql():
    """Verifies: values are stored as data never as sql."""
    journal = Journal()
    hostile_text = "x'); DROP TABLE desk_events; --"
    journal.record_event(make_desk_event(0, reason=hostile_text, symbol="AAPL"))
    assert journal.events("run-1")[0].reason == hostile_text
    assert journal.reason_counts("run-1", "SKIP") == {hostile_text: 1}


def test_duplicate_sequence_in_one_run_is_rejected():
    """Verifies: duplicate sequence in one run is rejected."""
    journal = Journal()
    journal.record_event(make_desk_event(0))
    with pytest.raises(Exception, match="UNIQUE"):
        journal.record_event(make_desk_event(0))


async def test_the_full_report_behind_a_trade_decision_is_kept_and_read_back():
    """Verifies: the journal stores the whole six-agent report for a trade (indicators, each
    agent's view, risk checks, plan) without the raw bars, and returns it for its run only.
    """
    from trading_desk.desk import run_desk_decision
    from trading_desk.models import AccountSnapshot
    from trading_desk.tools.market_data import FixtureProvider

    desk_report = await run_desk_decision(
        "AAPL",
        FixtureProvider(),
        AccountSnapshot(equity=100_000.0, cash=100_000.0),
        datetime(2025, 4, 23, 22, tzinfo=UTC),
    )
    journal = Journal()
    journal.record_decision("run-1", desk_report)
    journal.record_decision("run-1", desk_report)  # recording twice keeps one row
    (saved_report,) = journal.decisions("run-1")
    assert saved_report["symbol"] == "AAPL"
    assert saved_report["decision"]["plan"]["strategy"] == "TREND_CONTINUATION"
    assert saved_report["indicators"]["close"] == desk_report.indicators.close
    assert len(saved_report["risk"]["checks"]) == 9
    assert saved_report["chart"]["trend"] == "BULLISH"
    assert "data" not in saved_report
    assert journal.decisions("run-2") == []


def test_run_ids_list_every_run_oldest_first():
    """Verifies: the journal can list its runs, which is how a review picks one to open."""
    journal = Journal()
    assert journal.run_ids() == []
    journal.record_event(
        make_desk_event(0, run_id="later-run").model_copy(
            update={"timestamp": START + timedelta(days=5)}
        )
    )
    journal.record_event(make_desk_event(0, run_id="earlier-run"))
    journal.record_event(make_desk_event(1, run_id="earlier-run"))
    assert journal.run_ids() == ["earlier-run", "later-run"]
