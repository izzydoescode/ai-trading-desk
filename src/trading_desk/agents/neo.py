"""NEO, the chart analyst: turns measurements into a thesis and concrete levels.

Reuses the existing technical thesis and independent counter-case, then adds the
prices that define a long idea: entry reference, ATR-based stop, and target.
Levels exist only when the thesis is bullish and the data is usable.
"""

from pydantic import ConfigDict, Field

from trading_desk.agents import contrarian, technical
from trading_desk.models import ChartSetup, Classification, Indicators, Model

PRICE_DECIMAL_PLACES = 4


class SetupRules(Model):
    """How NEO places the stop and target around the entry reference."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    stop_atr_multiple: float = Field(default=2.0, gt=0)
    target_reward_multiple: float = Field(default=2.5, gt=0)


DEFAULT_SETUP_RULES = SetupRules()


def build_setup(
    indicators: Indicators,
    blockers: list[str],
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
) -> ChartSetup:
    thesis = technical.analyze(indicators)
    counter_case = contrarian.review(indicators, thesis, blockers)
    close, average_true_range = indicators.close, indicators.atr14
    can_place_levels = (
        not blockers
        and thesis.classification == Classification.BULLISH
        and close is not None
        and average_true_range is not None
        and average_true_range > 0
    )
    stop = (
        round(close - setup_rules.stop_atr_multiple * average_true_range, PRICE_DECIMAL_PLACES)
        if can_place_levels
        else None
    )
    if stop is None or stop <= 0:
        return ChartSetup(
            thesis=thesis,
            counter_case=counter_case,
            support=indicators.low20,
            resistance=indicators.high20,
            confirmation=(
                "A daily close above SMA20 with SMA20 above SMA50 would confirm an uptrend."
            ),
            invalidation="No long setup is active, so there is nothing to invalidate.",
        )
    entry_reference = round(close, PRICE_DECIMAL_PLACES)
    target = round(
        entry_reference + setup_rules.target_reward_multiple * (entry_reference - stop),
        PRICE_DECIMAL_PLACES,
    )
    return ChartSetup(
        thesis=thesis,
        counter_case=counter_case,
        support=indicators.low20,
        resistance=indicators.high20,
        entry_reference=entry_reference,
        stop=stop,
        target=target,
        confirmation=(
            f"Uptrend confirmed while the close holds above SMA20 {indicators.sma20:.4f}."
        ),
        invalidation=(
            f"A close below {stop:.4f} ({setup_rules.stop_atr_multiple:g} ATR under entry) "
            "invalidates the idea."
        ),
    )
