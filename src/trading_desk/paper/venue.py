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
            position.units * self._last_prices.get(symbol, position.entry_price)
            for symbol, position in self.positions.items()
        )
        return round(self.cash + open_value, MONEY_DECIMAL_PLACES)

    def account_snapshot(self) -> AccountSnapshot:
        open_risk_amount = sum(
            max(position.entry_price - position.stop, 0.0) * position.units
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
        self._last_prices[symbol] = candle.close
        return events

    def _fill(self, plan: TradePlan, candle: Candle) -> VenueEvent:
        if candle.open <= plan.stop:
            return self._event(
                VenueEventKind.CANCEL, candle.timestamp, plan.symbol, "opened_at_or_below_stop"
            )
        if candle.open >= plan.target:
            return self._event(
                VenueEventKind.CANCEL, candle.timestamp, plan.symbol, "opened_at_or_above_target"
            )
        fill_price = candle.open * (1 + self.market_profile.slippage_rate)
        notional = plan.units * fill_price
        commission = notional * self.market_profile.commission_rate
        if notional + commission > self.cash:
            return self._event(
                VenueEventKind.CANCEL, candle.timestamp, plan.symbol, "insufficient_cash_at_open"
            )
        self.cash -= notional + commission
        slippage_cost = plan.units * (fill_price - candle.open)
        self.positions[plan.symbol] = Position(
            symbol=plan.symbol,
            units=plan.units,
            entry_price=fill_price,
            stop=plan.stop,
            target=plan.target,
            opened_at=candle.timestamp,
            entry_commission=commission,
            entry_slippage_cost=slippage_cost,
            planned_risk_amount=plan.risk_amount,
        )
        return self._event(
            VenueEventKind.FILL,
            candle.timestamp,
            plan.symbol,
            "filled_at_next_open",
            price=fill_price,
            units=plan.units,
        )

    def _check_exit(
        self, position: Position, candle: Candle, position_opened_this_bar: bool
    ) -> VenueEvent | None:
        # The open belongs to the entry on the fill bar, so a gap exit cannot apply there.
        if not position_opened_this_bar and candle.open <= position.stop:
            return self._close(position, candle.open, candle.timestamp, ExitReason.STOP)
        if candle.low <= position.stop:
            return self._close(position, position.stop, candle.timestamp, ExitReason.STOP)
        if not position_opened_this_bar and candle.open >= position.target:
            return self._close(position, candle.open, candle.timestamp, ExitReason.TARGET)
        if candle.high >= position.target:
            return self._close(position, position.target, candle.timestamp, ExitReason.TARGET)
        return None

    def _close(
        self, position: Position, reference_price: float, closed_at: datetime, reason: ExitReason
    ) -> VenueEvent:
        exit_price = reference_price * (1 - self.market_profile.slippage_rate)
        proceeds = position.units * exit_price
        commission = proceeds * self.market_profile.commission_rate
        self.cash += proceeds - commission
        exit_slippage_cost = position.units * (reference_price - exit_price)
        # Slippage is already inside the entry and exit prices; commissions are not.
        profit_and_loss = round(
            position.units * (exit_price - position.entry_price)
            - position.entry_commission
            - commission,
            MONEY_DECIMAL_PLACES,
        )
        total_cost = (
            position.entry_commission
            + position.entry_slippage_cost
            + commission
            + exit_slippage_cost
        )
        del self.positions[position.symbol]
        self.closed_trades.append(
            ClosedTrade(
                symbol=position.symbol,
                units=position.units,
                entry_price=position.entry_price,
                exit_price=exit_price,
                opened_at=position.opened_at,
                closed_at=closed_at,
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
