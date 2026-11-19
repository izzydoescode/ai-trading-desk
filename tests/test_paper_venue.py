from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.markets import US_EQUITIES
from trading_desk.models import Candle, ExitReason, TradePlan, TradeSide, VenueEventKind
from trading_desk.paper import PaperVenue, create_venue

FREE_MARKET = US_EQUITIES.model_copy(update={"commission_rate": 0.0, "slippage_rate": 0.0})
COSTLY_MARKET = US_EQUITIES.model_copy(update={"commission_rate": 0.001, "slippage_rate": 0.001})
DAY_ONE = datetime(2025, 3, 3, 14, 30, tzinfo=UTC)


def make_candle(day, open_price, high, low, close):
    """Test helper (not a test): builds candle for the tests below."""
    return Candle(
        timestamp=DAY_ONE + timedelta(days=day),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def make_trade_plan(symbol="AAPL", units=10, entry=100.0, stop=95.0, target=110.0, **overrides):
    """Test helper (not a test): builds trade plan for the tests below."""
    fields = {
        "strategy": "TREND_CONTINUATION",
        "symbol": symbol,
        "side": TradeSide.LONG,
        "entry_reference": entry,
        "stop": stop,
        "target": target,
        "units": units,
        "notional": units * entry,
        "risk_amount": units * (entry - stop),
        "reward_to_risk": (target - entry) / (entry - stop),
    }
    return TradePlan(**{**fields, **overrides})


def make_paper_venue(market=FREE_MARKET, starting_cash=10_000.0):
    """Test helper (not a test): builds paper venue for the tests below."""
    return PaperVenue(starting_cash, market)


def test_order_fills_at_the_next_bar_open_not_at_the_signal_price():
    """Verifies: order fills at the next bar open not at the signal price."""
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
    """Verifies: target exit books profit and r multiple."""
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
    """Verifies: stop exit books the planned loss."""
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
    """Verifies: a bar touching stop and target counts as a loss."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 100.0, 112.0, 94.0, 105.0))
    assert paper_venue.closed_trades[0].exit_reason is ExitReason.STOP


def test_gap_through_the_stop_exits_at_the_open_not_at_the_stop():
    """Verifies: gap through the stop exits at the open not at the stop."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 90.0, 91.0, 89.0, 90.5))
    trade = paper_venue.closed_trades[0]
    assert trade.exit_price == 90.0
    assert trade.r_multiple == pytest.approx(-2.0)


def test_gap_above_the_target_exits_at_the_open():
    """Verifies: gap above the target exits at the open."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 115.0, 116.0, 114.0, 115.5))
    assert paper_venue.closed_trades[0].exit_price == 115.0


def test_stop_can_trigger_on_the_fill_bar_but_never_at_its_open():
    """Verifies: stop can trigger on the fill bar but never at its open."""
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
    """Verifies: order is cancelled when the open voids the idea."""
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
    """Verifies: order is cancelled when the open costs more than the cash held."""
    paper_venue = make_paper_venue(starting_cash=1_000.0)
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    events = paper_venue.process_bar("AAPL", make_candle(1, 105.0, 106.0, 104.0, 105.5))
    assert events[0].reason == "insufficient_cash_at_open"


def test_commission_and_slippage_reduce_the_result_on_both_sides():
    """Verifies: commission and slippage reduce the result on both sides."""
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
    """Verifies: only one idea per symbol."""
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
    """Verifies: account snapshot counts open and queued risk."""
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
    """Verifies: realised loss is tracked per trading day."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    paper_venue.process_bar("AAPL", make_candle(2, 99.0, 99.5, 94.0, 94.5))
    assert paper_venue.account_snapshot().realized_loss_today == pytest.approx(50.0)
    paper_venue.process_bar("AAPL", make_candle(3, 95.0, 96.0, 94.0, 95.5))
    assert paper_venue.account_snapshot().realized_loss_today == 0.0


def test_kill_switch_cancels_queued_orders_and_refuses_new_ones():
    """Verifies: kill switch cancels queued orders and refuses new ones."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan("AAPL"), DAY_ONE)
    events = paper_venue.halt(DAY_ONE)
    assert [event.kind for event in events] == [VenueEventKind.HALT, VenueEventKind.CANCEL]
    assert paper_venue.pending_orders == {}
    assert paper_venue.submit(make_trade_plan("MSFT"), DAY_ONE).reason == "venue_halted"
    paper_venue.resume()
    assert paper_venue.submit(make_trade_plan("MSFT"), DAY_ONE).kind is VenueEventKind.SUBMIT


def test_liquidate_closes_positions_at_the_latest_price():
    """Verifies: liquidate closes positions at the latest price."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    events = paper_venue.liquidate(DAY_ONE + timedelta(days=1), ExitReason.END_OF_RUN)
    assert events[0].reason == "END_OF_RUN"
    assert paper_venue.closed_trades[0].exit_price == 101.0
    assert paper_venue.positions == {}
    assert paper_venue.equity == pytest.approx(10_010.0)


def test_only_paper_execution_exists():
    """Verifies: only paper execution exists."""
    assert isinstance(create_venue("paper", 1_000.0, US_EQUITIES), PaperVenue)
    with pytest.raises(NotImplementedError, match="paper only"):
        create_venue("live", 1_000.0, US_EQUITIES)
    with pytest.raises(ValueError):
        PaperVenue(0, US_EQUITIES)


def test_only_a_band_following_position_lets_its_target_be_moved():
    """Verifies: a target is moved only for a position whose plan asked for a band exit, so a
    fixed-target trade can never have its target changed after entry.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=101, low=99.5, close=100))
    assert paper_venue.move_target("AAPL", 104.0) is False
    assert paper_venue.positions["AAPL"].target == 110.0
    assert paper_venue.move_target("MSFT", 104.0) is False  # no such position

    band_venue = make_paper_venue()
    band_venue.submit(make_trade_plan(target_follows_upper_band=True), DAY_ONE)
    band_venue.process_bar("AAPL", make_candle(1, open_price=100, high=101, low=99.5, close=100))
    assert band_venue.move_target("AAPL", 0.0) is False  # a non-positive level is ignored
    assert band_venue.move_target("AAPL", 104.0) is True
    assert band_venue.positions["AAPL"].target == 104.0
    # The next bar reaches the moved target and exits there, not at the original 110.
    exit_events = band_venue.process_bar(
        "AAPL", make_candle(2, open_price=101, high=104.5, low=100.5, close=104)
    )
    assert [event.reason for event in exit_events] == ["TARGET"]
    assert band_venue.closed_trades[0].exit_price == pytest.approx(104.0)
    assert band_venue.closed_trades[0].profit_and_loss > 0


def test_a_closed_trade_records_its_plan_holding_time_and_how_far_it_travelled():
    """Verifies: the trade record keeps what review needs: the strategy, the planned stop,
    target and risk, how many bars it was held, and the best and worst points reached in R.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(strategy="BREAKOUT"), DAY_ONE)  # entry 100, stop 95
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=101, low=99, close=100))
    paper_venue.process_bar("AAPL", make_candle(2, open_price=100, high=106, low=98, close=105))
    assert paper_venue.positions["AAPL"].bars_held == 2
    paper_venue.process_bar("AAPL", make_candle(3, open_price=105, high=111, low=104, close=110))
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.strategy.value == "BREAKOUT"
    assert (closed_trade.planned_stop, closed_trade.planned_target) == (95.0, 110.0)
    assert closed_trade.planned_risk_amount == 50.0
    assert closed_trade.bars_held == 2  # two full bars held before the exit bar
    assert closed_trade.exit_reason is ExitReason.TARGET
    # Best point: the target at 110, two risk units above entry. Worst: 98, 0.4 below.
    assert closed_trade.maximum_favorable_excursion_r == pytest.approx(2.0)
    assert closed_trade.maximum_adverse_excursion_r == pytest.approx(-0.4)


def test_a_stopped_trade_shows_it_never_went_anywhere():
    """Verifies: a trade stopped out at once records a full unit of adverse excursion and
    almost none in its favour, which is what marks a bad entry rather than a tight stop.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=100.5, low=94, close=95))
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.exit_reason is ExitReason.STOP
    assert closed_trade.bars_held == 0
    assert closed_trade.maximum_adverse_excursion_r == pytest.approx(-1.0)
    assert closed_trade.maximum_favorable_excursion_r == 0.0


def test_a_band_following_trade_keeps_the_target_it_was_planned_with():
    """Verifies: when the target moves with the band, the record still shows the target the
    trade was approved with, so plan and outcome can be compared.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(target_follows_upper_band=True), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=101, low=99.5, close=100))
    paper_venue.move_target("AAPL", 104.0)
    paper_venue.process_bar(
        "AAPL", make_candle(2, open_price=101, high=104.5, low=100.5, close=104)
    )
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.planned_target == 110.0
    assert closed_trade.exit_price == pytest.approx(104.0)


def hold_flat_and_losing(paper_venue, sessions, close=99.0, first_day=1):
    """Test helper (not a test): feeds sessions that drift just under the 100 entry without
    reaching the 95 stop or making any progress, and returns every venue event.
    """
    events = []
    for day in range(first_day, first_day + sessions):
        events += paper_venue.process_bar(
            "AAPL", make_candle(day, open_price=close, high=100.4, low=98.5, close=close)
        )
    return events


def test_a_trade_that_never_got_going_is_sold_early_instead_of_waiting_for_its_stop():
    """Verifies: after the agreed number of sessions, a trade closing below its entry that
    never reached half its risk in profit is sold at the next open, well short of the stop.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(stalled_trade_sessions=3), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=100.4, low=99, close=99))
    assert hold_flat_and_losing(paper_venue, sessions=2, first_day=2) == []
    assert paper_venue.positions["AAPL"].exit_at_next_open is True  # decided at the close
    exit_events = paper_venue.process_bar(
        "AAPL", make_candle(4, open_price=98.8, high=99.5, low=98, close=99)
    )
    assert [event.reason for event in exit_events] == ["STALLED"]
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.exit_reason is ExitReason.STALLED
    assert closed_trade.exit_price == pytest.approx(98.8)  # the next open, not the 95 stop
    assert closed_trade.r_multiple == pytest.approx(-0.24)  # a quarter of the planned loss
    assert closed_trade.bars_held == 3


def test_early_exit_never_fires_before_the_agreed_number_of_sessions():
    """Verifies: the trade is given its time: nothing happens before the session count."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(stalled_trade_sessions=5), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=100.4, low=99, close=99))
    assert hold_flat_and_losing(paper_venue, sessions=3, first_day=2) == []
    assert paper_venue.positions["AAPL"].exit_at_next_open is False
    assert paper_venue.positions["AAPL"].bars_held == 4


def test_a_trade_that_is_ahead_or_has_shown_progress_is_left_alone():
    """Verifies: early exit is only for losers that never moved: a trade closing at or above
    its entry, or one that already reached half its risk in profit, keeps its stop and target.
    """
    ahead = make_paper_venue()
    ahead.submit(make_trade_plan(stalled_trade_sessions=2), DAY_ONE)
    ahead.process_bar("AAPL", make_candle(1, open_price=100, high=100.4, low=99.5, close=100))
    hold_flat_and_losing(ahead, sessions=4, close=100.0, first_day=2)
    assert ahead.positions["AAPL"].exit_at_next_open is False

    showed_progress = make_paper_venue()
    showed_progress.submit(make_trade_plan(stalled_trade_sessions=2), DAY_ONE)
    # Entry 100, risk 5 a share: a high of 102.5 is exactly half the risk in profit.
    showed_progress.process_bar(
        "AAPL", make_candle(1, open_price=100, high=102.5, low=98.9, close=99)
    )
    hold_flat_and_losing(showed_progress, sessions=4, first_day=2)
    assert showed_progress.positions["AAPL"].exit_at_next_open is False


def test_early_exit_is_off_when_the_plan_does_not_ask_for_it():
    """Verifies: with no session count on the plan, a stalled loser is held to its stop."""
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=100.4, low=99, close=99))
    assert hold_flat_and_losing(paper_venue, sessions=20, first_day=2) == []
    assert "AAPL" in paper_venue.positions


def test_an_early_exit_at_a_gap_down_open_takes_that_open():
    """Verifies: the early exit is filled at the next open even when that open is below the
    stop, because the decision to sell was already made; it is recorded as STALLED.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(stalled_trade_sessions=1), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, open_price=100, high=100.4, low=99, close=99))
    exit_events = paper_venue.process_bar(
        "AAPL", make_candle(2, open_price=93, high=94, low=92, close=93)
    )
    assert [event.reason for event in exit_events] == ["STALLED"]
    assert paper_venue.closed_trades[0].exit_price == pytest.approx(93.0)
