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
    band_venue.submit(make_trade_plan(target_follows_band=True), DAY_ONE)
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
    paper_venue.submit(make_trade_plan(target_follows_band=True), DAY_ONE)
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


def make_short_plan(units=10, entry=100.0, stop=105.0, target=90.0, **overrides):
    """Test helper (not a test): builds a short plan: sell at 100, stop above at 105, target
    below at 90, so the risk is 5 a share and the planned reward 10.
    """
    return make_trade_plan(
        units=units,
        entry=entry,
        stop=stop,
        target=target,
        side=TradeSide.SHORT,
        risk_amount=units * (stop - entry),
        reward_to_risk=(entry - target) / (stop - entry),
        **overrides,
    )


def open_short(paper_venue, **plan_overrides):
    """Test helper (not a test): queues a short and fills it at an open of 100."""
    paper_venue.submit(make_short_plan(**plan_overrides), DAY_ONE)
    return paper_venue.process_bar(
        "AAPL", make_candle(1, open_price=100, high=101, low=99, close=100)
    )


def test_a_short_profits_when_the_price_falls_to_its_target():
    """Verifies: a short sold at 100 and bought back at its 90 target makes 10 a share, two
    times its risk, and cash returns to the start plus that profit.
    """
    paper_venue = make_paper_venue()
    fill_events = open_short(paper_venue)
    assert [event.kind for event in fill_events] == [VenueEventKind.FILL]
    assert paper_venue.positions["AAPL"].side is TradeSide.SHORT
    assert paper_venue.cash == pytest.approx(9_000.0)  # 1,000 set aside as collateral
    exit_events = paper_venue.process_bar(
        "AAPL", make_candle(2, open_price=96, high=97, low=89, close=91)
    )
    assert [event.reason for event in exit_events] == ["TARGET"]
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.side is TradeSide.SHORT
    assert closed_trade.exit_price == pytest.approx(90.0)
    assert closed_trade.profit_and_loss == pytest.approx(100.0)
    assert closed_trade.r_multiple == pytest.approx(2.0)
    assert paper_venue.cash == pytest.approx(10_100.0)
    assert paper_venue.equity == pytest.approx(10_100.0)


def test_a_short_loses_when_the_price_rises_to_its_stop():
    """Verifies: a short is stopped out by a rise: bought back at the 105 stop for the
    planned loss of one R, and the loss counts toward the day's realised loss.
    """
    paper_venue = make_paper_venue()
    open_short(paper_venue)
    exit_events = paper_venue.process_bar(
        "AAPL", make_candle(2, open_price=101, high=106, low=100, close=105)
    )
    assert [event.reason for event in exit_events] == ["STOP"]
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.exit_price == pytest.approx(105.0)
    assert closed_trade.profit_and_loss == pytest.approx(-50.0)
    assert closed_trade.r_multiple == pytest.approx(-1.0)
    assert paper_venue.account_snapshot().realized_loss_today == pytest.approx(50.0)


def test_an_open_short_is_marked_to_market_so_equity_falls_as_price_rises():
    """Verifies: while a short is open, equity moves against the price: up 3 a share when
    the price falls 3, down 2 a share when it rises 2.
    """
    paper_venue = make_paper_venue()
    open_short(paper_venue)
    paper_venue.process_bar("AAPL", make_candle(2, open_price=99, high=99.5, low=96.5, close=97))
    assert paper_venue.equity == pytest.approx(10_030.0)
    paper_venue.process_bar("AAPL", make_candle(3, open_price=98, high=102.5, low=97, close=102))
    assert paper_venue.equity == pytest.approx(9_980.0)
    assert paper_venue.account_snapshot().open_risk_amount == pytest.approx(50.0)


def test_a_short_bar_touching_stop_and_target_counts_as_a_loss():
    """Verifies: the worst-case rule holds for shorts too: one bar spanning both levels is
    booked as the stop.
    """
    paper_venue = make_paper_venue()
    open_short(paper_venue)
    exit_events = paper_venue.process_bar(
        "AAPL", make_candle(2, open_price=100, high=106, low=89, close=95)
    )
    assert [event.reason for event in exit_events] == ["STOP"]


def test_short_gaps_exit_at_the_open():
    """Verifies: a gap up through a short's stop is bought back at the open (worse than the
    stop), and a gap down through its target is bought back at the open (better).
    """
    gapped_against = make_paper_venue()
    open_short(gapped_against)
    gapped_against.process_bar("AAPL", make_candle(2, open_price=108, high=109, low=107, close=108))
    assert gapped_against.closed_trades[0].exit_price == pytest.approx(108.0)
    assert gapped_against.closed_trades[0].exit_reason is ExitReason.STOP

    gapped_in_favour = make_paper_venue()
    open_short(gapped_in_favour)
    gapped_in_favour.process_bar("AAPL", make_candle(2, open_price=88, high=89, low=87, close=88))
    assert gapped_in_favour.closed_trades[0].exit_price == pytest.approx(88.0)
    assert gapped_in_favour.closed_trades[0].exit_reason is ExitReason.TARGET


@pytest.mark.parametrize(
    "open_price,expected_reason",
    [(105.0, "opened_at_or_above_stop"), (90.0, "opened_at_or_below_target")],
)
def test_a_short_order_is_cancelled_when_the_open_voids_the_idea(open_price, expected_reason):
    """Verifies: a short is not opened if the next open is already at or beyond its stop or
    its target.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_short_plan(), DAY_ONE)
    events = paper_venue.process_bar(
        "AAPL",
        make_candle(
            1, open_price=open_price, high=open_price + 1, low=open_price - 1, close=open_price
        ),
    )
    assert [(event.kind, event.reason) for event in events] == [
        (VenueEventKind.CANCEL, expected_reason)
    ]
    assert paper_venue.cash == 10_000.0


def test_costs_work_against_a_short_on_both_sides():
    """Verifies: with slippage and commission a short sells a little lower and buys back a
    little higher, so the result is below the frictionless 100 and costs are recorded.
    """
    paper_venue = make_paper_venue(COSTLY_MARKET)
    open_short(paper_venue)
    assert paper_venue.positions["AAPL"].entry_price == pytest.approx(99.9)
    paper_venue.process_bar("AAPL", make_candle(2, open_price=96, high=97, low=89, close=91))
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.exit_price == pytest.approx(90.09)
    # (99.9 - 90.09) x 10 minus 0.1% commission on each side.
    assert closed_trade.profit_and_loss == pytest.approx(98.1 - 0.999 - 0.9009, abs=0.01)
    assert closed_trade.total_cost == pytest.approx(1.0 + 0.9 + 0.999 + 0.9009, abs=0.01)


def test_a_short_records_its_excursions_the_right_way_round():
    """Verifies: for a short, the best point is the lowest price and the worst the highest."""
    paper_venue = make_paper_venue()
    open_short(paper_venue)
    paper_venue.process_bar("AAPL", make_candle(2, open_price=100, high=102, low=96, close=97))
    paper_venue.process_bar("AAPL", make_candle(3, open_price=96, high=97, low=89, close=91))
    closed_trade = paper_venue.closed_trades[0]
    assert closed_trade.maximum_favorable_excursion_r == pytest.approx(2.0)  # down to 90
    assert closed_trade.maximum_adverse_excursion_r == pytest.approx(-0.4)  # up to 102


def test_a_stalled_short_is_bought_back_early():
    """Verifies: the early-exit rule mirrors for shorts: held long enough, closing above the
    entry, never half its risk in profit, so it is covered at the next open.
    """
    paper_venue = make_paper_venue()
    open_short(paper_venue, stalled_trade_sessions=2)
    paper_venue.process_bar("AAPL", make_candle(2, open_price=100, high=101.5, low=99.6, close=101))
    assert paper_venue.positions["AAPL"].exit_at_next_open is True
    exit_events = paper_venue.process_bar(
        "AAPL", make_candle(3, open_price=101.2, high=102, low=100.5, close=101)
    )
    assert [event.reason for event in exit_events] == ["STALLED"]
    assert paper_venue.closed_trades[0].r_multiple == pytest.approx(-0.24)

    working = make_paper_venue()
    open_short(working, stalled_trade_sessions=2)
    working.process_bar("AAPL", make_candle(2, open_price=100, high=100.5, low=97, close=98))
    assert working.positions["AAPL"].exit_at_next_open is False  # it is ahead: left alone


def make_minute_bars(*ranges):
    """Test helper (not a test): builds consecutive one-minute bars from (low, high) pairs."""
    return [
        Candle(
            timestamp=DAY_ONE + timedelta(days=2, minutes=minute),
            open=(low + high) / 2,
            high=high,
            low=low,
            close=(low + high) / 2,
            volume=100,
        )
        for minute, (low, high) in enumerate(ranges)
    ]


def open_long_then_touch_both(intraday_candles=None):
    """Test helper (not a test): fills the standard long (stop 95, target 110) and processes a
    day whose range touches both levels, with the given minute bars. Returns the venue and
    that day's events.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    wide_day = make_candle(2, 101.0, 111.0, 94.0, 100.0)
    assert paper_venue.needs_intraday_bars("AAPL", wide_day) is True
    return paper_venue, paper_venue.process_bar("AAPL", wide_day, intraday_candles)


def test_minute_bars_showing_the_target_first_turn_the_assumed_loss_into_the_real_win():
    """Verifies: when the session's minute bars reach the target before the stop, the trade
    closes at its target for a profit and is marked as settled intraday.
    """
    target_first = make_minute_bars((100.5, 102.0), (103.0, 110.5), (94.0, 99.0))
    paper_venue, events = open_long_then_touch_both(target_first)
    (closed_trade,) = paper_venue.closed_trades
    assert events[0].reason == "TARGET"
    assert closed_trade.exit_reason is ExitReason.TARGET
    assert closed_trade.exit_price == 110.0
    assert closed_trade.profit_and_loss == pytest.approx(100.0)
    assert closed_trade.same_bar_resolution.value == "INTRADAY"


def test_minute_bars_showing_the_stop_first_confirm_the_loss():
    """Verifies: when the minute bars reach the stop first, the trade is a loss at the stop,
    now marked as settled intraday rather than assumed.
    """
    stop_first = make_minute_bars((99.0, 101.0), (94.5, 98.0), (104.0, 111.0))
    paper_venue, _ = open_long_then_touch_both(stop_first)
    (closed_trade,) = paper_venue.closed_trades
    assert closed_trade.exit_reason is ExitReason.STOP
    assert closed_trade.profit_and_loss == pytest.approx(-50.0)
    assert closed_trade.same_bar_resolution.value == "INTRADAY"


@pytest.mark.parametrize(
    "unhelpful_minute_bars",
    [
        None,
        [],
        make_minute_bars((99.0, 101.0), (98.0, 100.0)),
        make_minute_bars((99.0, 101.0), (94.0, 111.0), (104.0, 111.0)),
    ],
    ids=["none supplied", "empty session", "neither level touched", "both in one minute"],
)
def test_when_minute_bars_cannot_decide_the_stop_is_still_assumed_first(unhelpful_minute_bars):
    """Verifies: with no minute bars, bars that never touch a level, or one minute that
    touches both, the venue keeps the worse outcome and says it was an assumption.
    """
    paper_venue, _ = open_long_then_touch_both(unhelpful_minute_bars)
    (closed_trade,) = paper_venue.closed_trades
    assert closed_trade.exit_reason is ExitReason.STOP
    assert closed_trade.exit_price == 95.0
    assert closed_trade.same_bar_resolution.value == "ASSUMED_STOP_FIRST"


def test_minute_bars_are_read_in_time_order_whatever_order_they_arrive_in():
    """Verifies: minute bars supplied newest first give the same answer as oldest first."""
    target_first = make_minute_bars((103.0, 110.5), (94.0, 99.0))
    paper_venue, _ = open_long_then_touch_both(list(reversed(target_first)))
    assert paper_venue.closed_trades[0].exit_reason is ExitReason.TARGET


def test_a_short_touching_both_levels_is_settled_the_mirrored_way():
    """Verifies: for a short (stop above, target below) the minute bars decide the same way:
    a fall to the target first is a win, a rise to the stop first is a loss.
    """
    outcomes = {}
    for label, minute_bars in {
        "target first": make_minute_bars((89.5, 96.0), (100.0, 106.0)),
        "stop first": make_minute_bars((100.0, 105.5), (89.0, 96.0)),
    }.items():
        paper_venue = make_paper_venue()
        paper_venue.submit(make_short_plan(), DAY_ONE)
        paper_venue.process_bar("AAPL", make_candle(1, 100.0, 101.0, 98.0, 99.0))
        wide_day = make_candle(2, 99.0, 106.0, 89.0, 100.0)
        assert paper_venue.needs_intraday_bars("AAPL", wide_day) is True
        paper_venue.process_bar("AAPL", wide_day, minute_bars)
        outcomes[label] = paper_venue.closed_trades[0]
    assert outcomes["target first"].exit_reason is ExitReason.TARGET
    assert outcomes["target first"].profit_and_loss == pytest.approx(100.0)
    assert outcomes["stop first"].exit_reason is ExitReason.STOP
    assert outcomes["stop first"].profit_and_loss == pytest.approx(-50.0)
    assert {trade.same_bar_resolution.value for trade in outcomes.values()} == {"INTRADAY"}


def test_minute_bars_are_only_needed_for_a_bar_that_is_truly_ambiguous():
    """Verifies: no minute bars are asked for when there is no position, when only one level
    is touched, when a held position gaps through its stop, or when it is already marked to
    be sold at the open; a queued order whose fill day touches both does need them. A trade
    closed on an unambiguous bar carries no same-bar mark.
    """
    paper_venue = make_paper_venue()
    wide_day = make_candle(2, 101.0, 111.0, 94.0, 100.0)
    assert paper_venue.needs_intraday_bars("AAPL", wide_day) is False

    paper_venue.submit(make_trade_plan(), DAY_ONE)
    assert paper_venue.needs_intraday_bars("AAPL", make_candle(1, 100.0, 111.0, 94.0, 100.0))
    paper_venue.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    assert paper_venue.needs_intraday_bars("AAPL", make_candle(2, 101.0, 111.0, 96.0, 105.0)) is (
        False
    )
    assert paper_venue.needs_intraday_bars("AAPL", make_candle(2, 94.0, 111.0, 93.0, 100.0)) is (
        False
    )
    paper_venue.positions["AAPL"] = paper_venue.positions["AAPL"].model_copy(
        update={"exit_at_next_open": True}
    )
    assert paper_venue.needs_intraday_bars("AAPL", wide_day) is False

    target_only = make_paper_venue()
    target_only.submit(make_trade_plan(), DAY_ONE)
    target_only.process_bar("AAPL", make_candle(1, 100.0, 102.0, 99.0, 101.0))
    target_only.process_bar("AAPL", make_candle(2, 101.0, 111.0, 100.0, 110.0))
    assert target_only.closed_trades[0].same_bar_resolution is None


def test_a_trade_filled_and_closed_on_the_same_wide_day_can_be_settled_too():
    """Verifies: when the fill day itself touches both levels, minute bars after the open
    decide the exit just as on any later day.
    """
    paper_venue = make_paper_venue()
    paper_venue.submit(make_trade_plan(), DAY_ONE)
    fill_day = make_candle(2, 100.0, 111.0, 94.0, 100.0)
    events = paper_venue.process_bar(
        "AAPL", fill_day, make_minute_bars((99.5, 101.0), (102.0, 110.2), (94.0, 99.0))
    )
    assert [event.kind for event in events] == [VenueEventKind.FILL, VenueEventKind.EXIT]
    assert paper_venue.closed_trades[0].exit_reason is ExitReason.TARGET
    assert paper_venue.closed_trades[0].same_bar_resolution.value == "INTRADAY"
