"""End-to-end tests of the desk loop over history: agents, risk and paper venue together."""

from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.engine import run_engine
from trading_desk.markets import US_EQUITIES
from trading_desk.models import Candle, ExitReason, MarketData, RiskLimits
from trading_desk.paper import PaperVenue
from trading_desk.paper.journal import Journal
from trading_desk.tools.market_data import FixtureProvider

FIRST_BAR = datetime(2025, 1, 2, 21, tzinfo=UTC)
LAST_BAR = datetime(2025, 4, 23, 21, tzinfo=UTC)
STARTING_CASH = 100_000.0


def make_trending_candles(session_count=90, daily_gain=0.4, start_price=100.0, volume=1_000_000):
    """Test helper (not a test): A steady uptrend with small pullbacks every fifth session, on
    weekdays only.
    """
    candles, price, session_date = [], start_price, FIRST_BAR
    while len(candles) < session_count:
        if session_date.weekday() < 5:
            change = -0.6 if len(candles) % 5 == 4 else daily_gain
            open_price, price = price, round(price + change, 4)
            candles.append(
                Candle(
                    timestamp=session_date,
                    open=open_price,
                    high=max(open_price, price) + 0.5,
                    low=min(open_price, price) - 0.5,
                    close=price,
                    volume=volume,
                )
            )
        session_date += timedelta(days=1)
    return candles


class SyntheticProvider:
    """Test double (not a test): stands in for a real provider so tests control what it does."""

    name = "synthetic-test"

    def __init__(self, candles_by_symbol):
        self.candles_by_symbol = candles_by_symbol

    async def fetch(self, symbol):
        if symbol not in self.candles_by_symbol:
            return MarketData(symbol=symbol, source=self.name, errors=["unknown symbol"])
        return MarketData(
            symbol=symbol, source=self.name, synthetic=True, candles=self.candles_by_symbol[symbol]
        )


async def run(provider=None, symbols=("AAPL",), venue=None, **options):
    """Test helper (not a test): run."""
    venue = venue or PaperVenue(STARTING_CASH, US_EQUITIES)
    result = await run_engine(
        list(symbols),
        provider or FixtureProvider(),
        venue,
        start=options.pop("start", FIRST_BAR),
        end=options.pop("end", LAST_BAR),
        **options,
    )
    return result, venue


def events_of(result, kind):
    """Test helper (not a test): events of."""
    return [event for event in result.events if event.kind == kind]


async def test_run_is_deterministic_apart_from_its_run_id():
    """Verifies: run is deterministic apart from its run id."""
    first_result, _ = await run()
    second_result, _ = await run()
    assert first_result.run_id != second_result.run_id
    assert first_result.final_equity == second_result.final_equity
    assert first_result.equity_curve == second_result.equity_curve
    assert first_result.closed_trades == second_result.closed_trades
    assert [(e.timestamp, e.symbol, e.kind, e.reason) for e in first_result.events] == [
        (e.timestamp, e.symbol, e.kind, e.reason) for e in second_result.events
    ]


async def test_no_decision_is_possible_before_enough_history_exists():
    """Verifies: no decision is possible before enough history exists."""
    result, _ = await run()
    assert result.bars_processed == 80
    first_fifty_bars = [point.timestamp for point in result.equity_curve[:49]]
    early_trades = [e for e in events_of(result, "TRADE") if e.timestamp in first_fifty_bars]
    assert early_trades == []
    assert events_of(result, "REJECT")[0].reason.startswith("Data quality")


async def test_orders_fill_on_the_bar_after_the_decision_never_the_same_bar():
    """Verifies: orders fill on the bar after the decision never the same bar."""
    result, _ = await run()
    trade_decisions = events_of(result, "TRADE")
    fills = events_of(result, "FILL")
    assert trade_decisions and fills
    bar_times = [point.timestamp for point in result.equity_curve]
    for fill in fills:
        fill_index = bar_times.index(fill.timestamp)
        assert any(decision.timestamp == bar_times[fill_index - 1] for decision in trade_decisions)


async def test_equity_is_flat_until_the_first_fill_and_positions_close_at_the_end():
    """Verifies: equity is flat until the first fill and positions close at the end."""
    result, venue = await run()
    first_fill_time = events_of(result, "FILL")[0].timestamp
    for point in result.equity_curve:
        if point.timestamp < first_fill_time:
            assert point.equity == STARTING_CASH
            assert not point.has_open_position
    assert venue.positions == {}
    assert result.final_equity == venue.equity == pytest.approx(venue.cash)
    assert result.closed_trades == venue.closed_trades
    assert result.closed_trades[-1].exit_reason in set(ExitReason)


async def test_symbol_with_an_open_position_is_skipped_not_re_entered():
    """Verifies: symbol with an open position is skipped not re entered."""
    result, _ = await run()
    skips = [event for event in events_of(result, "SKIP") if event.reason == "position_open"]
    assert skips
    assert len(events_of(result, "FILL")) == len(result.closed_trades)


async def test_one_open_idea_limit_holds_across_several_trending_symbols():
    """Verifies: one open idea limit holds across several trending symbols."""
    candles = make_trending_candles()
    provider = SyntheticProvider({"AAA": candles, "BBB": candles, "CCC": candles})
    result, _ = await run(provider, ("AAA", "BBB", "CCC"), end=candles[-1].timestamp)
    open_position_count = 0
    for event in result.events:
        if event.kind == "FILL":
            open_position_count += 1
        elif event.kind == "EXIT":
            open_position_count -= 1
        assert open_position_count <= 1
    assert any("Open ideas" in event.reason for event in events_of(result, "REJECT"))


async def test_more_open_ideas_are_taken_when_the_limit_allows_and_risk_is_rechecked():
    """Verifies: more open ideas are taken when the limit allows and risk is rechecked."""
    candles = make_trending_candles()
    provider = SyntheticProvider({"AAA": candles, "BBB": candles})
    result, venue = await run(
        provider,
        ("AAA", "BBB"),
        end=candles[-1].timestamp,
        limits=RiskLimits(maximum_open_ideas=2, maximum_daily_loss_fraction=0.01),
    )
    submit_times = [event.timestamp for event in events_of(result, "SUBMIT")]
    assert len(submit_times) != len(set(submit_times))  # two ideas queued on one bar
    assert {trade.symbol for trade in venue.closed_trades} == {"AAA", "BBB"}


async def test_symbol_without_data_is_reported_and_the_rest_still_run():
    """Verifies: symbol without data is reported and the rest still run."""
    provider = SyntheticProvider({"AAA": make_trending_candles()})
    result, _ = await run(provider, ("AAA", "MISSING"), end=LAST_BAR + timedelta(days=60))
    data_errors = events_of(result, "DATA_ERROR")
    assert [event.symbol for event in data_errors] == ["MISSING"]
    assert events_of(result, "FILL")


async def test_halted_venue_keeps_settling_bars_but_takes_no_new_ideas():
    """Verifies: halted venue keeps settling bars but takes no new ideas."""
    venue = PaperVenue(STARTING_CASH, US_EQUITIES)
    venue.halt(FIRST_BAR)
    result, _ = await run(venue=venue)
    assert result.bars_processed == 80
    assert events_of(result, "TRADE") == []
    assert result.final_equity == STARTING_CASH


async def test_kill_switch_limit_rejects_every_idea():
    """Verifies: kill switch limit rejects every idea."""
    result, _ = await run(limits=RiskLimits(kill_switch_engaged=True))
    assert events_of(result, "FILL") == []
    assert all("Kill switch" in event.reason for event in events_of(result, "REJECT"))


async def test_events_reach_the_journal_and_the_listener_in_sequence():
    """Verifies: events reach the journal and the listener in sequence."""
    journal, heard_events = Journal(), []
    result, _ = await run(journal=journal, on_event=heard_events.append)
    assert heard_events == result.events
    assert [event.sequence for event in result.events] == list(range(len(result.events)))
    assert journal.events(result.run_id) == result.events
    assert journal.trades(result.run_id) == result.closed_trades
    assert journal.reason_counts(result.run_id, "SKIP")["position_open"] >= 1


async def test_window_limits_which_bars_are_traded_but_not_the_history_used():
    """Verifies: window limits which bars are traded but not the history used."""
    midpoint = FIRST_BAR + timedelta(days=90)
    result, _ = await run(start=midpoint)
    assert all(point.timestamp >= midpoint for point in result.equity_curve)
    assert events_of(result, "TRADE")


async def test_empty_window_and_reversed_window():
    """Verifies: empty window and reversed window."""
    result, _ = await run(start=LAST_BAR + timedelta(days=10), end=LAST_BAR + timedelta(days=20))
    assert result.bars_processed == 0
    assert result.final_equity == STARTING_CASH
    with pytest.raises(ValueError):
        await run(start=LAST_BAR, end=FIRST_BAR)


async def test_bar_hook_sees_every_bar_in_order_with_that_bars_reports():
    """Verifies: bar hook sees every bar in order with that bars reports."""
    bar_updates = []

    async def remember(bar_update):
        bar_updates.append(bar_update)

    result, _ = await run(on_bar=remember)
    assert len(bar_updates) == result.bars_processed
    assert [bar_update.bar_index for bar_update in bar_updates] == list(range(len(bar_updates)))
    assert {bar_update.bar_count for bar_update in bar_updates} == {result.bars_processed}
    assert [bar_update.equity_point for bar_update in bar_updates[:-1]] == result.equity_curve[:-1]
    assert all("AAPL" in bar_update.candles_by_symbol for bar_update in bar_updates)
    decided_symbols = [
        report.symbol for bar_update in bar_updates for report in bar_update.desk_reports
    ]
    assert len(decided_symbols) == len(
        [event for event in result.events if event.agent == "PICARD"]
    )
    for bar_update in bar_updates:
        assert all(
            report.evaluated_as_of == bar_update.bar_time for report in bar_update.desk_reports
        )


async def test_bar_hook_still_fires_while_the_venue_is_halted():
    """Verifies: bar hook still fires while the venue is halted."""
    venue = PaperVenue(STARTING_CASH, US_EQUITIES)
    venue.halt(FIRST_BAR)
    bar_updates = []

    async def remember(bar_update):
        bar_updates.append(bar_update)

    result, _ = await run(venue=venue, on_bar=remember)
    assert len(bar_updates) == result.bars_processed > 0
    assert all(bar_update.desk_reports == [] for bar_update in bar_updates)


async def test_bar_hook_does_not_change_the_outcome():
    """Verifies: bar hook does not change the outcome."""

    async def ignore(bar_update):
        return None

    (with_hook, _), (without_hook, _) = await run(on_bar=ignore), await run()
    assert with_hook.final_equity == without_hook.final_equity
    assert len(with_hook.events) == len(without_hook.events)


async def test_news_feed_is_queried_point_in_time_on_every_decision():
    """Verifies: news feed is queried point in time on every decision."""

    class RecordingNewsFeed:
        name = "recording-news-feed"

        def __init__(self):
            self.window_ends = []

        async def fetch(self, symbol, start, end):
            self.window_ends.append(end)
            return []

    news_feed = RecordingNewsFeed()
    result, _ = await run(news_feed=news_feed, liquidate_at_end=False)
    decision_times = [event.timestamp for event in result.events if event.agent == "PICARD"]
    # Each decision asked for news ending at its own bar, never later.
    assert news_feed.window_ends == decision_times
    assert news_feed.window_ends == sorted(news_feed.window_ends)


def make_pullback_candles(pullback=(-1.8, -2.0, -1.6), recovery_sessions=25):
    """Test helper (not a test): builds an uptrend, a sharp three-day pullback that touches the
    lower Bollinger band while the trend is still bullish, then a steady recovery.
    """
    closes, price = [], 100.0
    for session in range(70):
        price += 0.5 if session % 4 else -0.3
        closes.append(round(price, 2))
    for change in pullback:
        price += change
        closes.append(round(price, 2))
    for _ in range(recovery_sessions):
        price += 0.9
        closes.append(round(price, 2))
    candles, previous_close, session_date = [], closes[0], FIRST_BAR
    for close in closes:
        while session_date.weekday() >= 5:
            session_date += timedelta(days=1)
        candles.append(
            Candle(
                timestamp=session_date,
                open=previous_close,
                high=max(previous_close, close) + 0.4,
                low=min(previous_close, close) - 0.4,
                close=close,
                volume=1_000_000,
            )
        )
        previous_close, session_date = close, session_date + timedelta(days=1)
    return candles


async def test_bollinger_trade_enters_at_the_lower_band_and_exits_at_the_moving_upper_band():
    """Verifies: in a bullish trend the desk buys the touch of the lower band and sells at the
    next touch of the upper band, where "the upper band" is the level known before each
    session, not the price fixed on the day of entry.
    """
    from trading_desk.agents.neo import SetupRules
    from trading_desk.tools.indicators import bollinger_bands

    candles = make_pullback_candles()
    result, _ = await run(
        SyntheticProvider({"AAPL": candles}),
        end=candles[-1].timestamp,
        setup_rules=SetupRules(enabled_strategies=("BOLLINGER_REVERSION",)),
        limits=RiskLimits(minimum_reward_to_risk=1.0),
    )
    assert len(result.closed_trades) == 1
    closed_trade = result.closed_trades[0]
    assert closed_trade.exit_reason is ExitReason.TARGET
    assert closed_trade.profit_and_loss > 0

    submit_event = events_of(result, "SUBMIT")[0]
    entry_day_closes = [c.close for c in candles if c.timestamp <= submit_event.timestamp]
    upper_band_at_entry, lower_band_at_entry = bollinger_bands(entry_day_closes)
    entry_candle = next(c for c in candles if c.timestamp == submit_event.timestamp)
    assert entry_candle.low <= lower_band_at_entry  # the entry bar touched the lower band

    exit_candle = next(c for c in candles if c.timestamp == closed_trade.closed_at)
    closes_before_exit = [c.close for c in candles if c.timestamp < exit_candle.timestamp]
    upper_band_before_exit, _ = bollinger_bands(closes_before_exit)
    slippage = US_EQUITIES.slippage_rate
    # It exited at the band as it stood before the exit session (or the open, on a gap)...
    expected_exit = max(upper_band_before_exit, exit_candle.open) * (1 - slippage)
    assert closed_trade.exit_price == pytest.approx(expected_exit)
    # ...which is a different price from the band on the day of entry.
    assert upper_band_before_exit != pytest.approx(upper_band_at_entry, abs=0.01)
    # And it never used a band that included the exit day's own close.
    assert exit_candle.high >= upper_band_before_exit


async def test_every_trade_taken_has_its_decision_report_and_its_record_in_the_journal():
    """Verifies: a run with a journal keeps, for each trade, the full report that justified
    it and the closed-trade record with strategy and excursions.
    """
    journal = Journal()
    result, _ = await run(journal=journal)
    saved_decisions = journal.decisions(result.run_id)
    trade_events = events_of(result, "TRADE")
    assert len(saved_decisions) == len(trade_events) >= 1
    assert [decision["evaluated_as_of"] for decision in saved_decisions] == [
        event.timestamp.isoformat().replace("+00:00", "Z") for event in trade_events
    ]
    assert all(decision["decision"]["action"] == "TRADE" for decision in saved_decisions)
    saved_trades = journal.trades(result.run_id)
    assert saved_trades == result.closed_trades
    assert saved_trades[0].strategy.value == "TREND_CONTINUATION"
    assert saved_trades[0].bars_held > 0
    assert saved_trades[0].maximum_favorable_excursion_r >= saved_trades[0].r_multiple > 0
    assert journal.run_ids() == [result.run_id]
