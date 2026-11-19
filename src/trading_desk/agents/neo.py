"""NEO, the chart analyst: turns measurements into a thesis and concrete levels.

NEO knows several long entry patterns and checks them in a fixed order of
priority. The first one whose trigger is met supplies the entry reference, stop
and target; the others are recorded with what they were missing, so the report
shows what NEO looked for and not only what it found.

The overall trend decides the side. In a bullish trend (SMA20 above SMA50 with the
close above SMA50) NEO takes long setups only. In a bearish trend (both below) it
takes short setups only, each the mirror image of its long counterpart. With no
clear trend NEO takes nothing.

1. BREAKOUT: the close clears the highest high of a prior window (a year, 55
   sessions or 20 sessions, longest first) on a volume surge. A breakout without
   volume is not taken. Short: the close breaks the lowest low of a prior window.
2. TREND_CONTINUATION: close above SMA20 above SMA50. Short: below both, in order.
3. BOLLINGER_REVERSION: in a bullish trend a touch or cross of the lower band is
   the entry and the next touch of the upper band is the exit, so the target
   follows the upper band from session to session. (In a bearish trend the roles
   swap: the upper band is the short entry and the lower band the exit.)

These are uncalibrated rules. Whether any of them has an edge is a question for
the backtest, and ODIN still decides whether the reward justifies the risk.
"""

from pydantic import ConfigDict, Field

from trading_desk.agents import contrarian, technical
from trading_desk.models import (
    ChartReport,
    Classification,
    Indicators,
    Model,
    SetupStrategy,
    TradeSetup,
    TradeSide,
)

PRICE_DECIMAL_PLACES = 4
ALL_STRATEGIES = (
    SetupStrategy.BREAKOUT,
    SetupStrategy.TREND_CONTINUATION,
    SetupStrategy.BOLLINGER_REVERSION,
)


class SetupRules(Model):
    """Which patterns NEO may use and how it places stops and targets."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    stop_atr_multiple: float = Field(default=2.0, gt=0)
    target_reward_multiple: float = Field(default=3.0, gt=0)
    # Checked in this order; the first pattern that triggers wins.
    enabled_strategies: tuple[SetupStrategy, ...] = ALL_STRATEGIES
    # A breakout needs the latest volume at least this multiple of its 20-session average.
    breakout_volume_ratio: float = Field(default=1.5, gt=0)
    # A reversion stop sits this many ATR beyond the low that touched the band.
    reversion_stop_atr_buffer: float = Field(default=0.5, gt=0)
    # Sides NEO may trade. Remove SHORT to run the desk long-only.
    allowed_sides: tuple[TradeSide, ...] = (TradeSide.LONG, TradeSide.SHORT)


DEFAULT_SETUP_RULES = SetupRules()

Levels = tuple[float, float, float, str, str]  # entry, stop, target, confirmation, invalidation


def overall_trend(indicators: Indicators) -> Classification:
    """Classify the trend that decides which side NEO may trade.

    BULLISH: SMA20 above SMA50 and the close above SMA50. BEARISH: SMA20 below SMA50
    and the close below SMA50. NEUTRAL when the two disagree. INSUFFICIENT_DATA when
    any of the three is unavailable.
    """
    close, sma20, sma50 = indicators.close, indicators.sma20, indicators.sma50
    if close is None or sma20 is None or sma50 is None:
        return Classification.INSUFFICIENT_DATA
    if sma20 > sma50 and close > sma50:
        return Classification.BULLISH
    if sma20 < sma50 and close < sma50:
        return Classification.BEARISH
    return Classification.NEUTRAL


def _stop_and_target(close: float, side: TradeSide, indicators: Indicators, rules: SetupRules):
    """Stop `stop_atr_multiple` ATR against the trade and target `target_reward_multiple`
    times that risk in its favour: below and above for a long, above and below for a short.
    """
    risk = rules.stop_atr_multiple * indicators.atr14
    direction = 1 if side is TradeSide.LONG else -1
    return close - direction * risk, close + direction * rules.target_reward_multiple * risk


def _breakout(
    indicators: Indicators, rules: SetupRules, side: TradeSide
) -> tuple[Levels | None, str]:
    """Check the BREAKOUT pattern and return (levels or None, a note saying why).

    Long: the close is above the longest prior high it has cleared (52-week, then 55-session,
    then 20-session). Short: the close is below the longest prior low it has broken. Either way
    the latest volume must be at least `breakout_volume_ratio` times its 20-session average; a
    break without volume is not taken.
    """
    close, is_long = indicators.close, side is TradeSide.LONG
    extreme_name = "high" if is_long else "low"
    windows = (
        [
            ("52-week", indicators.prior_high252),
            ("55-session", indicators.prior_high55),
            ("20-session", indicators.prior_high20),
        ]
        if is_long
        else [
            ("52-week", indicators.prior_low252),
            ("55-session", indicators.prior_low55),
            ("20-session", indicators.prior_low20),
        ]
    )
    broken = [
        (name, level)
        for name, level in windows
        if level is not None and (close > level if is_long else close < level)
    ]
    if not broken:
        available = [name for name, level in windows if level is not None]
        if not available:
            return None, f"BREAKOUT: not enough history for a prior {extreme_name}."
        position_word = "above" if is_long else "below"
        return None, (
            f"BREAKOUT: close is not {position_word} its prior {available[-1]} {extreme_name}."
        )
    window_name, breakout_level = broken[0]
    relative_volume = indicators.relative_volume20
    if relative_volume is None or relative_volume < rules.breakout_volume_ratio:
        volume_text = "unavailable" if relative_volume is None else f"{relative_volume:.2f}x"
        return None, (
            f"BREAKOUT: close cleared the {window_name} {extreme_name} {breakout_level:.4f} but "
            f"volume is {volume_text} (needs {rules.breakout_volume_ratio:g}x): not taken."
        )
    stop, target = _stop_and_target(close, side, indicators, rules)
    if is_long:
        confirmation = (
            f"Breakout above the {window_name} high {breakout_level:.4f} on "
            f"{relative_volume:.2f}x volume."
        )
        invalidation = (
            f"A close below {stop:.4f} ({rules.stop_atr_multiple:g} ATR under entry) means the "
            "breakout failed."
        )
    else:
        confirmation = (
            f"Breakdown below the {window_name} low {breakout_level:.4f} on "
            f"{relative_volume:.2f}x volume."
        )
        invalidation = (
            f"A close above {stop:.4f} ({rules.stop_atr_multiple:g} ATR over entry) means the "
            "breakdown failed."
        )
    direction_word = "above" if is_long else "below"
    return (close, stop, target, confirmation, invalidation), (
        f"BREAKOUT: triggered {direction_word} the {window_name} {extreme_name} on "
        f"{relative_volume:.2f}x volume."
    )


def _trend_continuation(
    indicators: Indicators,
    rules: SetupRules,
    thesis_classification: Classification,
    side: TradeSide,
) -> tuple[Levels | None, str]:
    """Check the TREND_CONTINUATION pattern and return (levels or None, a note).

    Long: the technical thesis is BULLISH (close above SMA20 above SMA50). Short: it is BEARISH
    (close below SMA20 below SMA50). Stop and target are placed the same way as for a breakout.
    """
    close, is_long = indicators.close, side is TradeSide.LONG
    wanted = Classification.BULLISH if is_long else Classification.BEARISH
    order_words = "above SMA20 above SMA50" if is_long else "below SMA20 below SMA50"
    if thesis_classification != wanted:
        return None, f"TREND_CONTINUATION: close is not {order_words}."
    stop, target = _stop_and_target(close, side, indicators, rules)
    if is_long:
        confirmation = (
            f"Uptrend confirmed while the close holds above SMA20 {indicators.sma20:.4f}."
        )
        invalidation = (
            f"A close below {stop:.4f} ({rules.stop_atr_multiple:g} ATR under entry) "
            "invalidates the idea."
        )
    else:
        confirmation = (
            f"Downtrend confirmed while the close holds below SMA20 {indicators.sma20:.4f}."
        )
        invalidation = (
            f"A close above {stop:.4f} ({rules.stop_atr_multiple:g} ATR over entry) "
            "invalidates the idea."
        )
    return (close, stop, target, confirmation, invalidation), (
        f"TREND_CONTINUATION: triggered, close {order_words}."
    )


def _bollinger_reversion(
    indicators: Indicators, rules: SetupRules, side: TradeSide
) -> tuple[Levels | None, str]:
    """Check the BOLLINGER_REVERSION pattern and return (levels or None, a note).

    In a bullish trend a touch or cross of the lower band is the long entry and the upper band
    is the exit. In a bearish trend a touch or cross of the upper band is the short entry and
    the lower band is the exit. The stop sits `reversion_stop_atr_buffer` ATR beyond the
    extreme that touched the band. Refused when there is no room between entry and exit band.
    """
    close, is_long = indicators.close, side is TradeSide.LONG
    lower_band, upper_band = indicators.bollinger_lower20, indicators.bollinger_upper20
    touching_extreme = indicators.latest_low if is_long else indicators.latest_high
    if lower_band is None or upper_band is None or touching_extreme is None:
        return None, "BOLLINGER_REVERSION: bands unavailable."
    buffer = rules.reversion_stop_atr_buffer * indicators.atr14
    if is_long:
        if touching_extreme > lower_band:
            return None, f"BOLLINGER_REVERSION: low did not reach the lower band {lower_band:.4f}."
        stop, target = min(touching_extreme, lower_band) - buffer, upper_band
        if not stop < close < target:
            return None, (
                "BOLLINGER_REVERSION: lower band touched, but no room left to the upper band."
            )
        return (
            close,
            stop,
            target,
            f"Bullish trend and the lower Bollinger band {lower_band:.4f} was touched: entry. "
            f"Exit is the next touch of the upper band, now {target:.4f}; it moves with the band.",
            f"A close below {stop:.4f} ({rules.reversion_stop_atr_buffer:g} ATR under the low "
            "that touched the band) means the bounce failed.",
        ), "BOLLINGER_REVERSION: triggered at the lower band in a bullish trend."
    if touching_extreme < upper_band:
        return None, f"BOLLINGER_REVERSION: high did not reach the upper band {upper_band:.4f}."
    stop, target = max(touching_extreme, upper_band) + buffer, lower_band
    if not target < close < stop:
        return None, "BOLLINGER_REVERSION: upper band touched, but no room left to the lower band."
    return (
        close,
        stop,
        target,
        f"Bearish trend and the upper Bollinger band {upper_band:.4f} was touched: short entry. "
        f"Exit is the next touch of the lower band, now {target:.4f}; it moves with the band.",
        f"A close above {stop:.4f} ({rules.reversion_stop_atr_buffer:g} ATR over the high "
        "that touched the band) means the rejection failed.",
    ), "BOLLINGER_REVERSION: triggered at the upper band in a bearish trend."


WATCH_FOR_A_SETUP = (
    "A clear trend with a trigger in its direction. Bullish (SMA20 above SMA50, close above "
    "SMA50): a volume-backed close above a prior high, a close above SMA20, or a touch of the "
    "lower Bollinger band. Bearish: the mirror image, taken short."
)


def analyze_chart(
    indicators: Indicators,
    blockers: list[str],
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
) -> ChartReport:
    """Build NEO's chart report for one symbol.

    Forms the technical thesis and its counter-case and reads the overall trend, which picks the
    side: bullish means long setups only, bearish means short setups only, anything else means
    none. If the data is usable and that side is allowed, each enabled strategy is checked in
    order. The first pattern that triggers supplies the trade setup; every pattern leaves a note
    explaining why it did or did not fire. With no trigger the report has no setup and says what
    NEO is watching for.
    """
    thesis = technical.analyze(indicators)
    counter_case = contrarian.review(indicators, thesis, blockers)
    close, average_true_range = indicators.close, indicators.atr14
    data_is_usable = (
        not blockers
        and not thesis.abstain
        and close is not None
        and average_true_range is not None
        and average_true_range > 0
    )
    chosen: tuple[SetupStrategy, Levels] | None = None
    strategy_notes: list[str] = []
    trend = overall_trend(indicators)
    side = {Classification.BULLISH: TradeSide.LONG, Classification.BEARISH: TradeSide.SHORT}.get(
        trend
    )
    if data_is_usable and side is None:
        strategy_notes.append(
            "No clear trend (SMA20 and the close disagree about SMA50): nothing is taken "
            "until the trend is bullish or bearish."
        )
    elif data_is_usable and side not in setup_rules.allowed_sides:
        strategy_notes.append(
            f"Trend is {trend.value.lower()}, which calls for {side.value.lower()} setups only, "
            f"and the {side.value.lower()} side is switched off. Nothing is taken against the "
            "trend."
        )
        side = None
    if data_is_usable and side is not None:
        for strategy in setup_rules.enabled_strategies:
            if strategy is SetupStrategy.BREAKOUT:
                levels, note = _breakout(indicators, setup_rules, side)
            elif strategy is SetupStrategy.TREND_CONTINUATION:
                levels, note = _trend_continuation(
                    indicators, setup_rules, thesis.classification, side
                )
            else:
                levels, note = _bollinger_reversion(indicators, setup_rules, side)
            if levels is not None and min(levels[1], levels[2]) <= 0:
                levels, note = (
                    None,
                    f"{strategy.value}: the stop or target would be at or below zero.",
                )
            if levels is not None and chosen is not None:
                note += f" Not used: {chosen[0].value} has priority."
            elif levels is not None:
                chosen = (strategy, levels)
            strategy_notes.append(note)

    if chosen is None:
        return ChartReport(
            thesis=thesis,
            counter_case=counter_case,
            trend=trend,
            strategy_notes=strategy_notes,
            support=indicators.low20,
            resistance=indicators.high20,
            watch_for=WATCH_FOR_A_SETUP,
        )
    strategy, (entry_reference, stop, target, confirmation, invalidation) = chosen
    return ChartReport(
        thesis=thesis,
        counter_case=counter_case,
        trend=trend,
        strategy_notes=strategy_notes,
        support=indicators.low20,
        resistance=indicators.high20,
        setup=TradeSetup(
            strategy=strategy,
            side=side,
            target_follows_band=strategy is SetupStrategy.BOLLINGER_REVERSION,
            entry_reference=round(entry_reference, PRICE_DECIMAL_PLACES),
            stop=round(stop, PRICE_DECIMAL_PLACES),
            target=round(target, PRICE_DECIMAL_PLACES),
            confirmation=confirmation,
            invalidation=invalidation,
        ),
    )
