from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.markets import US_EQUITIES
from trading_desk.models import Candle, ExitReason, TradePlan, TradeSide, VenueEventKind
from trading_desk.paper import PaperVenue, create_venue

FREE_MARKET = US_EQUITIES.model_copy(update={"commission_rate": 0.0, "slippage_rate": 0.0})
COSTLY_MARKET = US_EQUITIES.model_copy(update={"commission_rate": 0.001, "slippage_rate": 0.001})
DAY_ONE = datetime(2025, 3, 3, 14, 30, tzinfo=UTC)


def make_candle(day, open_price, high, low, close):
    return Candle(
        timestamp=DAY_ONE + timedelta(days=day),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def make_trade_plan(symbol="AAPL", units=10, entry=100.0, stop=95.0, target=110.0):
    return TradePlan(
        symbol=symbol,
        side=TradeSide.LONG,
        entry_reference=entry,
        stop=stop,
        target=target,
        units=units,
        notional=units * entry,
        risk_amount=units * (entry - stop),
        reward_to_risk=(target - entry) / (entry - stop),
    )


def make_paper_venue(market=FREE_MARKET, starting_cash=10_000.0):
    return PaperVenue(starting_cash, market)


def test_order_fills_at_the_next_bar_open_not_at_the_signal_price():
    paper_venue = make_paper_venue()
    submit_event = paper_venue.submit(make_trade_plan(), DAY_ONE)
    assert submit_event.kind is VenueEventKind.SUBMIT
    assert paper_venue.cash == 10_000.0
    events = paper_venue.process_bar("AAPL", make_candle(1, 101.0, 103.0, 100.5, 102.0))
    assert [event.kind for event in events] == [VenueEventKind.FILL]
    assert events[0].price == 101.0
    assert paper_venue.cash == pytest.approx(10_000.0 - 1010.0)
    assert paper_venue.equity == pytest.approx(10_010.0)


def test_target_exit_books_profit_and_r_multiple():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    events = paper_venue.process_bar("AAPL", make_candle(2, 104.0, 111.0, 103.0, 110.5))
    assert events[0].kind is VenueEventKind.EXIT
    assert events[0].reason == "TARGET"
    trade = paper_venue.closed_trades[0]
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.exit_price == 110.0
    assert trade.profit_and_loss == pytest.approx(100.0)
    assert trade.r_multiple == pytest.approx(2.0)
    assert paper_venue.positions == {}
    assert paper_venue.equity == pytest.approx(10_100.0)


def test_stop_exit_books_the_planned_loss():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 99.0, 99.5, 94.0, 94.5))
    trade = paper_venue.closed_trades[0]
    assert trade.exit_reason is ExitReason.STOP
    assert trade.exit_price == 95.0
    assert trade.profit_and_loss == pytest.approx(-50.0)
    assert trade.r_multiple == pytest.approx(-1.0)


def test_a_bar_touching_stop_and_target_counts_as_a_loss():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 100.0, 112.0, 94.0, 105.0))
    assert paper_venue.closed_trades[0].exit_reason is ExitReason.STOP


def test_gap_through_the_stop_exits_at_the_open_not_at_the_stop():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 90.0, 91.0, 89.0, 90.5))
    trade = paper_venue.closed_trades[0]
    assert trade.exit_price == 90.0
    assert trade.r_multiple == pytest.approx(-2.0)


def test_gap_above_the_target_exits_at_the_open():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 115.0, 116.0, 114.0, 115.5))
    assert paper_venue.closed_trades[0].exit_price == 115.0


def test_stop_can_trigger_on_the_fill_bar_but_never_at_its_open():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    events = paper_venue.process_bar("AAPL", make_candle(1, 100.0, 101.0, 94.0, 96.0))
    assert [event.kind for event in events] == [VenueEventKind.FILL, VenueEventKind.EXIT]
    assert paper_venue.closed_trades[0].exit_price == 95.0


@pytest.mark.parametrize(
    "open_price,expected_reason",
    [(95.0, "opened_at_or_below_stop"), (110.0, "opened_at_or_above_target")],
)
def test_order_is_cancelled_when_the_open_voids_the_idea(open_price, expected_reason):
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    events = paper_venue.process_bar(
        "AAPL", make_candle(1, open_price, open_price + 1, open_price - 1, open_price)
    )
    assert [(event.kind, event.reason) for event in events] == [
        (VenueEventKind.CANCEL, expected_reason)
    ]
    assert paper_venue.positions == {}
    assert paper_venue.cash == 10_000.0


def test_order_is_cancelled_when_the_open_costs_more_than_the_cash_held():
    paper_venue = make_paper_venue(starting_cash=1_000.0)
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    events = paper_venue.process_bar("AAPL", make_candle(1, 105.0, 106.0, 104.0, 105.5))
    assert events[0].reason == "insufficient_cash_at_open"


def test_commission_and_slippage_reduce_the_result_on_both_sides():
    paper_venue = make_paper_venue(COSTLY_MARKET)
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 104.0, 111.0, 103.0, 110.5))
    trade = paper_venue.closed_trades[0]
    assert trade.entry_price == pytest.approx(100.1)
    assert trade.exit_price == pytest.approx(109.89)
    # 10 * (109.89 - 100.1) = 97.90, less 1.001 and 1.0989 commission.
    assert trade.profit_and_loss == pytest.approx(95.80)
    assert trade.total_cost == pytest.approx(4.20)
    assert paper_venue.equity == pytest.approx(10_000.0 + 95.80, abs=0.01)


def test_only_one_idea_per_symbol():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    duplicate_event = paper_venue.submit(make_trade_plan(), DAY_ONE)
    assert duplicate_event.kind is VenueEventKind.CANCEL
    assert duplicate_event.reason == "already_exposed_to_symbol"
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    assert paper_venue.submit(make_trade_plan(), DAY_ONE).reason == "already_exposed_to_symbol"
    assert paper_venue.has_exposure("AAPL")
    assert not paper_venue.has_exposure("MSFT")


def test_account_snapshot_counts_open_and_queued_risk():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan("AAPL"), DAY_ONE)
    paper_venue.submit(make_trade_plan("MSFT", units=4), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    snapshot = paper_venue.account_snapshot()
    assert snapshot.open_idea_count == 2
    assert snapshot.open_risk_amount == pytest.approx(50.0 + 20.0)
    assert snapshot.cash == pytest.approx(9_000.0)
    assert snapshot.equity == pytest.approx(10_010.0)


def test_realised_loss_is_tracked_per_trading_day():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 99.0, 99.5, 94.0, 94.5))
    assert paper_venue.account_snapshot().realized_loss_today == pytest.approx(50.0)
    paper_venue.process_bar("AAPL", make_candle(3, 95.0, 96.0, 94.0, 95.5))
    assert paper_venue.account_snapshot().realized_loss_today == 0.0


def test_kill_switch_cancels_queued_orders_and_refuses_new_ones():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan("AAPL"), DAY_ONE)
    events = paper_venue.halt(DAY_ONE)
    assert [event.kind for event in events] == [VenueEventKind.HALT, VenueEventKind.CANCEL]
    assert paper_venue.pending_orders == {}
    assert paper_venue.submit(make_trade_plan("MSFT"), DAY_ONE).reason == "venue_halted"
    paper_venue.resume()
    assert paper_venue.submit(make_trade_plan("MSFT"), DAY_ONE).kind is VenueEventKind.SUBMIT


def test_liquidate_closes_positions_at_the_latest_price():
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    events = paper_venue.liquidate(DAY_ONE + timedelta(days=1), ExitReason.END_OF_RUN)
    assert events[0].reason == "END_OF_RUN"
    assert paper_venue.closed_trades[0].exit_price == 101.0
    assert paper_venue.positions == {}
    assert paper_venue.equity == pytest.approx(10_010.0)


def test_only_paper_execution_exists():
    assert isinstance(create_venue("paper", 1_000.0, US_EQUITIES), PaperVenue)
    with pytest.raises(NotImplementedError, match="paper only"):
        create_venue("live", 1_000.0, US_EQUITIES)
    with pytest.raises(ValueError):
        PaperVenue(0, US_EQUITIES)
