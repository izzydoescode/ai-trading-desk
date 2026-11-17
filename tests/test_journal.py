from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.models import ClosedTrade, DeskEvent, ExitReason
from trading_desk.paper.journal import Journal

START = datetime(2025, 3, 3, 21, tzinfo=UTC)


def desk_event(sequence, kind="SKIP", reason="close below SMA50", run_id="run-1", symbol="NVDA"):
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


def closed_trade(profit_and_loss=100.0):
    return ClosedTrade(
        symbol="AAPL",
        units=10,
        entry_price=100.0,
        exit_price=110.0,
        opened_at=START,
        closed_at=START + timedelta(days=2),
        exit_reason=ExitReason.TARGET,
        total_cost=0.0,
        profit_and_loss=profit_and_loss,
        r_multiple=profit_and_loss / 50.0,
    )


def test_events_round_trip_in_order_and_are_isolated_per_run():
    journal = Journal()
    for sequence in range(3):
        journal.record_event(desk_event(sequence))
    journal.record_event(desk_event(0, run_id="run-2"))
    events = journal.events("run-1")
    assert [event.sequence for event in events] == [0, 1, 2]
    assert events[0] == desk_event(0)
    assert len(journal.events("run-2")) == 1
    assert journal.events("missing-run") == []


def test_limit_returns_the_most_recent_events_still_in_order():
    journal = Journal()
    for sequence in range(5):
        journal.record_event(desk_event(sequence))
    assert [event.sequence for event in journal.events("run-1", limit=2)] == [3, 4]


def test_trades_round_trip_in_the_order_they_closed():
    journal = Journal()
    journal.record_trade("run-1", closed_trade(100.0))
    journal.record_trade("run-1", closed_trade(-50.0))
    assert [trade.profit_and_loss for trade in journal.trades("run-1")] == [100.0, -50.0]
    assert journal.trades("run-2") == []


def test_reason_counts_show_where_candidates_are_lost():
    journal = Journal()
    journal.record_event(desk_event(0, reason="close below SMA50"))
    journal.record_event(desk_event(1, reason="close below SMA50"))
    journal.record_event(desk_event(2, reason="position_open"))
    journal.record_event(desk_event(3, kind="TRADE", reason="approved"))
    assert journal.reason_counts("run-1", "SKIP") == {"close below SMA50": 2, "position_open": 1}


def test_journal_persists_to_a_file_and_creates_its_folder(tmp_path):
    database_path = tmp_path / "nested" / "journal.sqlite"
    journal = Journal(database_path)
    journal.record_event(desk_event(0))
    journal.close()
    reopened_journal = Journal(database_path)
    assert len(reopened_journal.events("run-1")) == 1


def test_values_are_stored_as_data_never_as_sql():
    journal = Journal()
    hostile_text = "x'); DROP TABLE desk_events; --"
    journal.record_event(desk_event(0, reason=hostile_text, symbol="AAPL"))
    assert journal.events("run-1")[0].reason == hostile_text
    assert journal.reason_counts("run-1", "SKIP") == {hostile_text: 1}


def test_duplicate_sequence_in_one_run_is_rejected():
    journal = Journal()
    journal.record_event(desk_event(0))
    with pytest.raises(Exception, match="UNIQUE"):
        journal.record_event(desk_event(0))
