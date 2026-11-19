"""ODIN, the risk manager: the deterministic gate between an idea and an order.

ODIN's job is to protect the account, not to find reasons to trade. Every check
is a rule with a stated limit and the measured value; any failure blocks the
idea. Position size is computed here and nowhere else, with trading costs counted
inside both the risk and the reward. No model can change an ODIN verdict.
"""

import math

from trading_desk.markets import MarketProfile
from trading_desk.models import (
    AccountSnapshot,
    ChartReport,
    RiskCheck,
    RiskLimits,
    RiskReport,
    RiskVerdict,
    TradePlan,
    TradeSide,
)

REWARD_TO_RISK_CHECK = "Reward to risk after costs"
FRACTIONAL_UNIT_DECIMAL_PLACES = 6
MONEY_DECIMAL_PLACES = 2


def evaluate(
    symbol: str,
    chart: ChartReport,
    account: AccountSnapshot,
    limits: RiskLimits,
    market_profile: MarketProfile,
    blockers: list[str],
) -> RiskReport:
    """Run every risk check on NEO's chart report and return ODIN's verdict.

    Checks, in order: kill switch, data quality, open ideas, daily loss budget (realised losses
    plus open stop risk), a setup with an invalidation level, reward to risk after trading costs,
    position size, max loss per idea, and no leverage. Position size is the smaller of what the
    risk budget allows and what the position cap and cash allow, rounded down.

    APPROVE needs every check to pass and returns the sized plan. REVISE is returned only when
    reward to risk is the sole failure. Anything else is REJECT. The verdict is final: no later
    agent or model can overturn it.
    """
    setup = chart.setup
    checks = [
        RiskCheck(
            name="Kill switch",
            limit="off",
            actual="ENGAGED" if limits.kill_switch_engaged else "off",
            passed=not limits.kill_switch_engaged,
        ),
        RiskCheck(
            name="Data quality",
            limit="no blockers",
            actual=f"{len(blockers)} blocker(s)",
            passed=not blockers,
        ),
        RiskCheck(
            name="Open ideas",
            limit=f"fewer than {limits.maximum_open_ideas} open",
            actual=f"{account.open_idea_count} open",
            passed=account.open_idea_count < limits.maximum_open_ideas,
        ),
    ]
    # Realised losses and the stops of open positions both draw down today's budget.
    daily_loss_budget = (
        account.equity * limits.maximum_daily_loss_fraction
        - account.realized_loss_today
        - account.open_risk_amount
    )
    checks.append(
        RiskCheck(
            name="Daily loss budget",
            limit=f"{limits.maximum_daily_loss_fraction:.2%} of equity incl. open stops",
            actual=f"{max(daily_loss_budget, 0.0):.2f} left",
            passed=daily_loss_budget > 0,
        )
    )
    checks.append(
        RiskCheck(
            name="Invalidation level set",
            limit="required",
            actual=f"{setup.stop:.4f}" if setup is not None else "missing",
            passed=setup is not None,
        )
    )

    plan = None
    if setup is not None:
        entry_reference, stop, target = setup.entry_reference, setup.stop, setup.target
        cost_per_unit = entry_reference * market_profile.round_trip_cost_rate
        risk_per_unit = entry_reference - stop + cost_per_unit
        reward_per_unit = target - entry_reference - cost_per_unit
        reward_to_risk = reward_per_unit / risk_per_unit
        checks.append(
            RiskCheck(
                name=REWARD_TO_RISK_CHECK,
                limit=f"at least {limits.minimum_reward_to_risk:.2f}",
                actual=f"{reward_to_risk:.2f}",
                passed=reward_to_risk >= limits.minimum_reward_to_risk,
            )
        )
        risk_budget = min(
            account.equity * limits.max_loss_per_idea_fraction, max(daily_loss_budget, 0.0)
        )
        # No leverage: the position can use neither more than its share of equity nor
        # more cash than the account holds.
        affordable_notional = min(account.equity * limits.maximum_position_fraction, account.cash)
        units = _round_down_units(
            min(risk_budget / risk_per_unit, affordable_notional / entry_reference),
            market_profile.allows_fractional_units,
        )
        notional = round(units * entry_reference, MONEY_DECIMAL_PLACES)
        risk_amount = round(units * risk_per_unit, MONEY_DECIMAL_PLACES)
        checks.append(
            RiskCheck(
                name="Position size",
                limit="at least one tradable unit within every limit",
                actual=f"{units:g} unit(s)",
                passed=units > 0,
            )
        )
        checks.append(
            RiskCheck(
                name="Max loss per idea",
                limit=f"{limits.max_loss_per_idea_fraction:.2%} of equity",
                actual=f"{risk_amount / account.equity:.2%}",
                passed=risk_amount <= account.equity * limits.max_loss_per_idea_fraction,
            )
        )
        checks.append(
            RiskCheck(
                name="Leverage",
                limit="none",
                actual=f"{notional / account.equity:.2f}x of equity",
                passed=notional <= account.cash,
            )
        )
        if units > 0 and reward_to_risk > 0:
            plan = TradePlan(
                symbol=symbol,
                side=TradeSide.LONG,
                entry_reference=entry_reference,
                stop=stop,
                target=target,
                units=units,
                notional=notional,
                risk_amount=risk_amount,
                reward_to_risk=round(reward_to_risk, 4),
            )

    failed_checks = [check.name for check in checks if not check.passed]
    if not failed_checks:
        return RiskReport(verdict=RiskVerdict.APPROVE, checks=checks, plan=plan)
    if failed_checks == [REWARD_TO_RISK_CHECK]:
        return RiskReport(
            verdict=RiskVerdict.REVISE,
            checks=checks,
            failed_checks=failed_checks,
            revision=(
                "Wait for an entry closer to the stop or a wider target so the reward is at "
                f"least {limits.minimum_reward_to_risk:.2f} times the risk after costs."
            ),
        )
    return RiskReport(verdict=RiskVerdict.REJECT, checks=checks, failed_checks=failed_checks)


def _round_down_units(units: float, allows_fractional_units: bool) -> float:
    """Never round a position up: that would exceed the risk budget."""
    if not allows_fractional_units:
        return float(math.floor(units + 1e-9))
    scale = 10**FRACTIONAL_UNIT_DECIMAL_PLACES
    return math.floor(units * scale + 1e-9) / scale
