"""Simulated execution for approved trade plans. No order ever reaches a broker.

Fill model, chosen to be conservative rather than flattering:

* An order submitted after a bar closes fills at the NEXT bar's open, plus slippage.
* If that open is already at or below the stop, or at or above the target, the idea
  is void and the order is cancelled instead of filled.
* A stop is checked before a target, so a bar that touches both counts as a loss.
* A gap through a level exits at the open, not at the level.
* Commission and slippage from the market profile are charged on entry and exit.

Paper results still overstate reality: there is no market impact, queue position
or halted session here.
"""

from datetime import date, datetime
from typing import Protocol

from trading_desk.markets import MarketProfile
from trading_desk.models import (
    AccountSnapshot,
    Candle,
    ClosedTrade,
    ExitReason,
    Position,
    TradePlan,
    TradeSide,
    VenueEvent,
    VenueEventKind,
)

MONEY_DECIMAL_PLACES = 2


class ExecutionVenue(Protocol):
    """Where approved plans are sent. Swapping this is the only change between modes."""

    name: str

    def submit(self, plan: TradePlan, submitted_at: datetime) -> VenueEvent: ...

    def process_bar(self, symbol: str, candle: Candle) -> list[VenueEvent]: ...

    def account_snapshot(self) -> AccountSnapshot: ...


class PaperVenue:
    """The simulated broker. The only venue this project has.

    Orders queue and fill at the next bar's open with slippage and commission. Each bar it checks
    open positions: the stop is checked before the target, and a gap through either level exits at
    the open. It tracks cash, positions, closed trades and the day's realised loss, and can be
    halted by the kill switch.
    """

    name = "paper"

    def __init__(self, starting_cash: float, market_profile: MarketProfile):
        if starting_cash <= 0:
            raise ValueError("starting cash must be positive")
        self.market_profile = market_profile
        self.cash = float(starting_cash)
        self.is_halted = False
        self.pending_orders: dict[str, TradePlan] = {}
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._last_prices: dict[str, float] = {}
        self._current_trading_day: date | None = None
        self._realized_loss_today = 0.0

    # -- account view ------------------------------------------------------

    @property
    def equity(self) -> float:
        """Cash plus open positions marked at the latest price seen for each symbol."""
        open_value = sum(
            self._position_value(position, self._last_prices.get(symbol, position.entry_price))
            for symbol, position in self.positions.items()
        )
        return round(self.cash + open_value, MONEY_DECIMAL_PLACES)

    @staticmethod
    def _position_value(position: Position, price: float) -> float:
        """What an open position is worth at a price.

        A long is worth its shares. A short is fully cash-collateralised: the cash set
        aside at entry plus or minus what the price has moved, so it gains as price falls.
        """
        if position.side is TradeSide.LONG:
            return position.units * price
        return position.units * (2 * position.entry_price - price)

    def account_snapshot(self) -> AccountSnapshot:
        open_risk_amount = sum(
            abs(position.entry_price - position.stop) * position.units
            for position in self.positions.values()
        ) + sum(plan.risk_amount for plan in self.pending_orders.values())
        return AccountSnapshot(
            equity=max(self.equity, 0.01),
            cash=max(self.cash, 0.0),
            open_idea_count=len(self.positions) + len(self.pending_orders),
            open_risk_amount=round(open_risk_amount, MONEY_DECIMAL_PLACES),
            realized_loss_today=round(self._realized_loss_today, MONEY_DECIMAL_PLACES),
        )

    def has_exposure(self, symbol: str) -> bool:
        return symbol in self.positions or symbol in self.pending_orders

    # -- order intake ------------------------------------------------------

    def submit(self, plan: TradePlan, submitted_at: datetime) -> VenueEvent:
        """Queue an approved plan for the next bar's open. One idea per symbol."""
        if self.is_halted:
            return self._event(VenueEventKind.CANCEL, submitted_at, plan.symbol, "venue_halted")
        if self.has_exposure(plan.symbol):
            return self._event(
                VenueEventKind.CANCEL, submitted_at, plan.symbol, "already_exposed_to_symbol"
            )
        self.pending_orders[plan.symbol] = plan
        return self._event(
            VenueEventKind.SUBMIT,
            submitted_at,
            plan.symbol,
            "queued_for_next_open",
            price=plan.entry_reference,
            units=plan.units,
        )

    def move_target(self, symbol: str, new_target: float) -> bool:
        """Move an open position's target to follow its exit band: the upper Bollinger
        band for a long, the lower band for a short.

        Only positions whose plan asked for a band exit are moved. The new level must
        come from bars BEFORE the one about to be processed, so it is a price the desk
        could really have had resting in the market. Returns True when a target moved.
        """
        position = self.positions.get(symbol)
        if position is None or not position.target_follows_band or new_target <= 0:
            return False
        self.positions[symbol] = position.model_copy(update={"target": new_target})
        return True

    # -- bar processing ----------------------------------------------------

    def process_bar(self, symbol: str, candle: Candle) -> list[VenueEvent]:
        """Advance one symbol by one bar: fill a queued order, then check exits."""
        self._roll_trading_day(candle.timestamp.date())
        events: list[VenueEvent] = []
        position_opened_this_bar = False
        pending_plan = self.pending_orders.pop(symbol, None)
        if pending_plan is not None:
            fill_event = self._fill(pending_plan, candle)
            events.append(fill_event)
            position_opened_this_bar = fill_event.kind is VenueEventKind.FILL
        position = self.positions.get(symbol)
        if position is not None:
            exit_event = self._check_exit(position, candle, position_opened_this_bar)
            if exit_event is not None:
                events.append(exit_event)
            else:
                # Still open after this bar: count it and remember how far price travelled.
                position = position.model_copy(
                    update={
                        "bars_held": position.bars_held + 1,
                        "highest_price": max(position.highest_price, candle.high),
                        "lowest_price": min(position.lowest_price, candle.low),
                    }
                )
                self.positions[symbol] = position.model_copy(
                    update={"exit_at_next_open": self._has_stalled(position, candle.close)}
                )
        self._last_prices[symbol] = candle.close
        return events

    def _fill(self, plan: TradePlan, candle: Candle) -> VenueEvent:
        """Open a position for a queued plan at this bar's open, charging slippage and commission.
        The
        order is rejected instead if cash no longer covers it.
        """
        is_long = plan.side is TradeSide.LONG
        # An open already beyond the stop or the target means the idea is void before entry.
        if (candle.open <= plan.stop) if is_long else (candle.open >= plan.stop):
            reason = "opened_at_or_below_stop" if is_long else "opened_at_or_above_stop"
            return self._event(VenueEventKind.CANCEL, candle.timestamp, plan.symbol, reason)
        if (candle.open >= plan.target) if is_long else (candle.open <= plan.target):
            reason = "opened_at_or_above_target" if is_long else "opened_at_or_below_target"
            return self._event(VenueEventKind.CANCEL, candle.timestamp, plan.symbol, reason)
        # Slippage always works against the desk: a buy fills higher, a short sale lower.
        slippage_direction = 1 if is_long else -1
        fill_price = candle.open * (1 + slippage_direction * self.market_profile.slippage_rate)
        notional = plan.units * fill_price
        commission = notional * self.market_profile.commission_rate
        if notional + commission > self.cash:
            return self._event(
                VenueEventKind.CANCEL, candle.timestamp, plan.symbol, "insufficient_cash_at_open"
            )
        # Long: pay for the shares. Short: set the same amount aside as collateral.
        self.cash -= notional + commission
        slippage_cost = plan.units * abs(fill_price - candle.open)
        self.positions[plan.symbol] = Position(
            symbol=plan.symbol,
            side=plan.side,
            units=plan.units,
            entry_price=fill_price,
            stop=plan.stop,
            target=plan.target,
            opened_at=candle.timestamp,
            entry_commission=commission,
            entry_slippage_cost=slippage_cost,
            planned_risk_amount=plan.risk_amount,
            strategy=plan.strategy,
            target_follows_band=plan.target_follows_band,
            planned_target=plan.target,
            highest_price=fill_price,
            lowest_price=fill_price,
            stalled_trade_sessions=plan.stalled_trade_sessions,
            stalled_trade_progress_r=plan.stalled_trade_progress_r,
        )
        return self._event(
            VenueEventKind.FILL,
            candle.timestamp,
            plan.symbol,
            "filled_at_next_open",
            price=fill_price,
            units=plan.units,
        )

    @staticmethod
    def _has_stalled(position: Position, session_close: float) -> bool:
        """Decide at a session's close whether to give up on the trade at the next open.

        True only when all three hold: it has been held for the agreed number of full
        sessions, it is closing below its entry price, and its best point so far never
        reached the agreed fraction of its risk. A trade that is ahead, or that has
        already shown it can move, is left to its stop and target.
        """
        if position.stalled_trade_sessions is None:
            return False
        if position.bars_held < position.stalled_trade_sessions:
            return False
        is_long = position.side is TradeSide.LONG
        is_losing = (
            session_close < position.entry_price
            if is_long
            else session_close > position.entry_price
        )
        if not is_losing:
            return False
        risk_per_unit = position.planned_risk_amount / position.units
        best_move = (
            position.highest_price - position.entry_price
            if is_long
            else position.entry_price - position.lowest_price
        )
        return best_move / risk_per_unit < position.stalled_trade_progress_r

    def _check_exit(
        self, position: Position, candle: Candle, position_opened_this_bar: bool
    ) -> VenueEvent | None:
        # The open belongs to the entry on the fill bar, so a gap exit cannot apply there.
        """Decide whether this bar closes the position and at what price: a gap through the stop or
        target exits at the open, otherwise the stop is checked before the target.
        """
        if position.exit_at_next_open:
            # Decided at the previous close: sold at this open, wherever it is.
            return self._close(position, candle.open, candle.timestamp, ExitReason.STALLED)
        if position.side is TradeSide.LONG:
            opens_through_stop = candle.open <= position.stop
            touches_stop = candle.low <= position.stop
            opens_through_target = candle.open >= position.target
            touches_target = candle.high >= position.target
        else:
            # Mirror image: a short is stopped by a rise and reaches its target on a fall.
            opens_through_stop = candle.open >= position.stop
            touches_stop = candle.high >= position.stop
            opens_through_target = candle.open <= position.target
            touches_target = candle.low <= position.target
        if not position_opened_this_bar and opens_through_stop:
            return self._close(position, candle.open, candle.timestamp, ExitReason.STOP)
        if touches_stop:
            return self._close(position, position.stop, candle.timestamp, ExitReason.STOP)
        if not position_opened_this_bar and opens_through_target:
            return self._close(position, candle.open, candle.timestamp, ExitReason.TARGET)
        if touches_target:
            return self._close(position, position.target, candle.timestamp, ExitReason.TARGET)
        return None

    def _close(
        self, position: Position, reference_price: float, closed_at: datetime, reason: ExitReason
    ) -> VenueEvent:
        """Close a position at the given price: apply exit slippage and commission, return the cash,
        record the trade with its profit or loss in money and in R, and add any loss to today's
        realised loss.
        """
        is_long = position.side is TradeSide.LONG
        # Slippage works against the desk on the way out too: a long sells lower, a short
        # buys back higher.
        slippage_direction = -1 if is_long else 1
        exit_price = reference_price * (1 + slippage_direction * self.market_profile.slippage_rate)
        commission = position.units * exit_price * self.market_profile.commission_rate
        # Long: the sale proceeds come back. Short: the collateral comes back, plus or minus
        # the price move.
        self.cash += self._position_value(position, exit_price) - commission
        exit_slippage_cost = position.units * abs(reference_price - exit_price)
        price_gain_per_unit = (
            exit_price - position.entry_price if is_long else position.entry_price - exit_price
        )
        # Slippage is already inside the entry and exit prices; commissions are not.
        profit_and_loss = round(
            position.units * price_gain_per_unit - position.entry_commission - commission,
            MONEY_DECIMAL_PLACES,
        )
        total_cost = (
            position.entry_commission
            + position.entry_slippage_cost
            + commission
            + exit_slippage_cost
        )
        del self.positions[position.symbol]
        # The exit itself is part of the journey: a stop extends the worst point, a target
        # the best. Excursions are measured from the entry, in units of the planned risk.
        highest_price = max(position.highest_price, reference_price)
        lowest_price = min(position.lowest_price, reference_price)
        risk_per_unit = position.planned_risk_amount / position.units
        best_price, worst_price = (
            (highest_price, lowest_price) if is_long else (lowest_price, highest_price)
        )
        favorable_move = abs(best_price - position.entry_price)
        adverse_move = abs(worst_price - position.entry_price)
        # A price that never went the right (or wrong) way leaves that excursion at zero.
        if (best_price < position.entry_price) if is_long else (best_price > position.entry_price):
            favorable_move = 0.0
        if (
            (worst_price > position.entry_price)
            if is_long
            else (worst_price < position.entry_price)
        ):
            adverse_move = 0.0
        self.closed_trades.append(
            ClosedTrade(
                symbol=position.symbol,
                side=position.side,
                strategy=position.strategy,
                units=position.units,
                entry_price=position.entry_price,
                exit_price=exit_price,
                planned_stop=position.stop,
                planned_target=position.planned_target,
                planned_risk_amount=position.planned_risk_amount,
                opened_at=position.opened_at,
                closed_at=closed_at,
                bars_held=position.bars_held,
                maximum_favorable_excursion_r=round(favorable_move / risk_per_unit, 4),
                maximum_adverse_excursion_r=round(-adverse_move / risk_per_unit, 4),
                exit_reason=reason,
                total_cost=round(total_cost, MONEY_DECIMAL_PLACES),
                profit_and_loss=profit_and_loss,
                r_multiple=round(profit_and_loss / position.planned_risk_amount, 4),
            )
        )
        if profit_and_loss < 0:
            self._realized_loss_today += -profit_and_loss
        return self._event(
            VenueEventKind.EXIT,
            closed_at,
            position.symbol,
            reason.value,
            price=exit_price,
            units=position.units,
            profit_and_loss=profit_and_loss,
        )

    # -- controls ----------------------------------------------------------

    def halt(self, halted_at: datetime, reason: str = "kill_switch") -> list[VenueEvent]:
        """Kill switch: cancel every queued order and refuse new ones. Positions stay
        protected by their stops; use `liquidate` to flatten them."""
        self.is_halted = True
        events = [self._event(VenueEventKind.HALT, halted_at, "*", reason)]
        for symbol in sorted(self.pending_orders):
            events.append(self._event(VenueEventKind.CANCEL, halted_at, symbol, reason))
        self.pending_orders.clear()
        return events

    def resume(self) -> None:
        self.is_halted = False

    def liquidate(self, closed_at: datetime, reason: ExitReason) -> list[VenueEvent]:
        """Close every open position at its latest seen price."""
        return [
            self._close(
                position,
                self._last_prices.get(position.symbol, position.entry_price),
                closed_at,
                reason,
            )
            for position in list(self.positions.values())
        ]

    # -- helpers -----------------------------------------------------------

    def _roll_trading_day(self, trading_day: date) -> None:
        if self._current_trading_day is None or trading_day > self._current_trading_day:
            self._current_trading_day = trading_day
            self._realized_loss_today = 0.0

    @staticmethod
    def _event(
        kind: VenueEventKind,
        timestamp: datetime,
        symbol: str,
        reason: str,
        price: float | None = None,
        units: float | None = None,
        profit_and_loss: float | None = None,
    ) -> VenueEvent:
        """Build one venue event (submit, fill, exit, reject, halt, cancel) with its reason."""
        return VenueEvent(
            kind=kind,
            timestamp=timestamp,
            symbol=symbol,
            reason=reason,
            price=price,
            units=units,
            profit_and_loss=profit_and_loss,
        )


def create_venue(mode: str, starting_cash: float, market_profile: MarketProfile) -> PaperVenue:
    """The single place a venue is chosen. Anything but paper is refused."""
    if mode != "paper":
        raise NotImplementedError(
            f"Execution mode {mode!r} is not implemented: this project trades on paper only."
        )
    return PaperVenue(starting_cash, market_profile)
