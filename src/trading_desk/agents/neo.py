"""NEO, the chart analyst: turns measurements into a thesis and concrete levels.

NEO knows several long entry patterns and checks them in a fixed order of
priority. The first one whose trigger is met supplies the entry reference, stop
and target; the others are recorded with what they were missing, so the report
shows what NEO looked for and not only what it found.

The overall trend decides the side. In a bullish trend (SMA20 above SMA50 with the
close above SMA50) NEO takes long setups only. In a bearish trend it would take
short setups only; shorts are not built yet, so a bearish trend produces no setup.
With no clear trend NEO takes nothing.

1. BREAKOUT: the close clears the highest high of a prior window (a year, 55
   sessions or 20 sessions, longest first) on a volume surge. A breakout without
   volume is not taken.
2. TREND_CONTINUATION: close above SMA20 above SMA50.
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


def _breakout(indicators: Indicators, rules: SetupRules) -> tuple[Levels | None, str]:
    """Check the BREAKOUT pattern and return (levels or None, a note saying why).

    Triggers when the close is above the longest prior high it has cleared (52-week, then
    55-session, then 20-session) AND the latest volume is at least `breakout_volume_ratio` times
    its 20-session average. Stop is `stop_atr_multiple` ATR under the close; target is
    `target_reward_multiple` times that risk above it.
    """
    close, average_true_range = indicators.close, indicators.atr14
    windows = [
        ("52-week", indicators.prior_high252),
        ("55-session", indicators.prior_high55),
        ("20-session", indicators.prior_high20),
    ]
    broken = [(name, level) for name, level in windows if level is not None and close > level]
    if not broken:
        available = [name for name, level in windows if level is not None]
        if not available:
            return None, "BREAKOUT: not enough history for a prior high."
        return None, f"BREAKOUT: close is not above its prior {available[-1]} high."
    window_name, breakout_level = broken[0]
    relative_volume = indicators.relative_volume20
    if relative_volume is None or relative_volume < rules.breakout_volume_ratio:
        volume_text = "unavailable" if relative_volume is None else f"{relative_volume:.2f}x"
        return None, (
            f"BREAKOUT: close cleared the {window_name} high {breakout_level:.4f} but volume "
            f"is {volume_text} (needs {rules.breakout_volume_ratio:g}x): not taken."
        )
    stop = close - rules.stop_atr_multiple * average_true_range
    target = close + rules.target_reward_multiple * (close - stop)
    return (
        close,
        stop,
        target,
        f"Breakout above the {window_name} high {breakout_level:.4f} on "
        f"{relative_volume:.2f}x volume.",
        f"A close below {stop:.4f} ({rules.stop_atr_multiple:g} ATR under entry) means the "
        "breakout failed.",
    ), f"BREAKOUT: triggered above the {window_name} high on {relative_volume:.2f}x volume."


def _trend_continuation(
    indicators: Indicators, rules: SetupRules, thesis_classification: Classification
) -> tuple[Levels | None, str]:
    """Check the TREND_CONTINUATION pattern and return (levels or None, a note).

    Triggers when the technical thesis is BULLISH (close above SMA20 above SMA50). Stop and target
    are placed the same way as for a breakout.
    """
    if thesis_classification != Classification.BULLISH:
        return None, "TREND_CONTINUATION: close is not above SMA20 above SMA50."
    close = indicators.close
    stop = close - rules.stop_atr_multiple * indicators.atr14
    target = close + rules.target_reward_multiple * (close - stop)
    return (
        close,
        stop,
        target,
        f"Uptrend confirmed while the close holds above SMA20 {indicators.sma20:.4f}.",
        f"A close below {stop:.4f} ({rules.stop_atr_multiple:g} ATR under entry) "
        "invalidates the idea.",
    ), "TREND_CONTINUATION: triggered, close above SMA20 above SMA50."


def _bollinger_reversion(indicators: Indicators, rules: SetupRules) -> tuple[Levels | None, str]:
    """Check the BOLLINGER_REVERSION pattern and return (levels or None, a note).

    Triggers when the session's low touches or crosses the lower band while the close is still
    above SMA50. Stop sits `reversion_stop_atr_buffer` ATR under the low that touched; target is
    the upper band or the 20-session high, whichever is nearer. Refused when there is no room
    between entry and target.
    """
    close, latest_low = indicators.close, indicators.latest_low
    lower_band, upper_band = indicators.bollinger_lower20, indicators.bollinger_upper20
    if lower_band is None or upper_band is None or latest_low is None:
        return None, "BOLLINGER_REVERSION: bands unavailable."
    if latest_low > lower_band:
        return None, f"BOLLINGER_REVERSION: low did not reach the lower band {lower_band:.4f}."
    stop = min(latest_low, lower_band) - rules.reversion_stop_atr_buffer * indicators.atr14
    target = upper_band
    if not stop < close < target:
        return None, "BOLLINGER_REVERSION: lower band touched, but no room left to the upper band."
    return (
        close,
        stop,
        target,
        f"Bullish trend and the lower Bollinger band {lower_band:.4f} was touched: entry. "
        f"Exit is the next touch of the upper band, now {target:.4f}; it moves with the band.",
        f"A close below {stop:.4f} ({rules.reversion_stop_atr_buffer:g} ATR under the low that "
        "touched the band) means the bounce failed.",
    ), "BOLLINGER_REVERSION: triggered at the lower band in a bullish trend."


def analyze_chart(
    indicators: Indicators,
    blockers: list[str],
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
) -> ChartReport:
    """Build NEO's chart report for one symbol.

    Forms the technical thesis and its counter-case, then, if the data is usable, checks each
    enabled strategy in order. The first pattern that triggers supplies the trade setup; every
    pattern leaves a note explaining why it did or did not fire. With no trigger the report has no
    setup and says what NEO is watching for.
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
    if data_is_usable and trend is Classification.BEARISH:
        strategy_notes.append(
            "Trend is bearish (SMA20 below SMA50, close below SMA50): only short setups "
            "apply, and shorts are not built yet. No long is taken against the trend."
        )
    elif data_is_usable and trend is not Classification.BULLISH:
        strategy_notes.append(
            "No clear trend (SMA20 and the close disagree about SMA50): nothing is taken "
            "until the trend is bullish or bearish."
        )
    if data_is_usable and trend is Classification.BULLISH:
        for strategy in setup_rules.enabled_strategies:
            if strategy is SetupStrategy.BREAKOUT:
                levels, note = _breakout(indicators, setup_rules)
            elif strategy is SetupStrategy.TREND_CONTINUATION:
                levels, note = _trend_continuation(indicators, setup_rules, thesis.classification)
            else:
                levels, note = _bollinger_reversion(indicators, setup_rules)
            if levels is not None and levels[1] <= 0:
                levels, note = None, f"{strategy.value}: the stop would be at or below zero."
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
            watch_for=(
                "A bullish trend (SMA20 above SMA50, close above SMA50) with a volume-backed "
                "close above a prior high, a close above SMA20, or a touch of the lower "
                "Bollinger band."
            ),
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
            target_follows_upper_band=strategy is SetupStrategy.BOLLINGER_REVERSION,
            strategy=strategy,
            entry_reference=round(entry_reference, PRICE_DECIMAL_PLACES),
            stop=round(stop, PRICE_DECIMAL_PLACES),
            target=round(target, PRICE_DECIMAL_PLACES),
            confirmation=confirmation,
            invalidation=invalidation,
        ),
    )
