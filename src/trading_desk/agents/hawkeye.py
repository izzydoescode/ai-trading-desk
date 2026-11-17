"""HAWKEYE, the market scout: ranks symbols by how much attention they deserve.

A transparent, uncalibrated scoring rule over measured indicators. It only decides
what the desk looks at next; it never proposes a trade. It scores whichever way the
symbol is trending: an uptrend is a long candidate, a downtrend a short candidate.
"""

from collections.abc import Iterable

from trading_desk.agents.technical import evidence
from trading_desk.models import (
    Indicators,
    ScoutReport,
    ScoutSignal,
    ScoutSignalKind,
    ScoutVerdict,
    TradeSide,
)

WATCH_SCORE_THRESHOLD = 0.6
TREND_ALIGNED_POINTS = 0.4
ABOVE_LONG_AVERAGE_POINTS = 0.2
MAXIMUM_MOMENTUM_POINTS = 0.3
FULL_MOMENTUM_RETURN_PERCENT = 10.0
HEALTHY_STRENGTH_POINTS = 0.2
RECOVERING_STRENGTH_POINTS = 0.1
PARTICIPATION_POINTS = 0.05
# A session trading at least 50% above its 20-session average volume is a surge.
VOLUME_SURGE_RATIO = 1.5
VOLUME_SURGE_POINTS = 0.1
VERDICT_ORDER = {ScoutVerdict.WATCH: 0, ScoutVerdict.SKIP: 1, ScoutVerdict.NEED_MORE_DATA: 2}


SIGNAL_DESCRIPTIONS = {
    ScoutSignalKind.NEW_52_WEEK_HIGH: "New 52-week high above",
    ScoutSignalKind.NEW_52_WEEK_LOW: "New 52-week low below",
    ScoutSignalKind.BREAKOUT_ABOVE_PRIOR_DAY_HIGH: "Close broke out above the prior day's high",
    ScoutSignalKind.BREAKDOWN_BELOW_PRIOR_DAY_LOW: "Close broke down below the prior day's low",
}


def find_signals(indicators: Indicators, has_volume_surge: bool) -> list[ScoutSignal]:
    """New yearly extremes and breaks of the prior day's range, each marked as
    confirmed only when a volume surge came with it."""
    candidates = [
        (
            ScoutSignalKind.NEW_52_WEEK_HIGH,
            indicators.prior_high252,
            indicators.latest_high,
            True,
        ),
        (
            ScoutSignalKind.NEW_52_WEEK_LOW,
            indicators.prior_low252,
            indicators.latest_low,
            False,
        ),
        (
            ScoutSignalKind.BREAKOUT_ABOVE_PRIOR_DAY_HIGH,
            indicators.previous_high,
            indicators.close,
            True,
        ),
        (
            ScoutSignalKind.BREAKDOWN_BELOW_PRIOR_DAY_LOW,
            indicators.previous_low,
            indicators.close,
            False,
        ),
    ]
    signals = []
    for kind, level, price, is_bullish in candidates:
        if level is None or price is None:
            continue
        if (price > level) if is_bullish else (price < level):
            signals.append(
                ScoutSignal(
                    kind=kind,
                    level=level,
                    is_bullish=is_bullish,
                    volume_confirmed=has_volume_surge,
                )
            )
    return signals


def scout(symbol: str, indicators: Indicators, blockers: list[str]) -> ScoutReport:
    """Score one symbol from its indicators, or say exactly which data is missing."""
    if blockers:
        return ScoutReport(
            symbol=symbol, verdict=ScoutVerdict.NEED_MORE_DATA, reasons=list(blockers), evidence=[]
        )
    required_metrics = ("close", "sma20", "sma50", "rsi14", "return20_pct")
    missing_metrics = [name for name in required_metrics if getattr(indicators, name) is None]
    if missing_metrics:
        return ScoutReport(
            symbol=symbol,
            verdict=ScoutVerdict.NEED_MORE_DATA,
            reasons=[f"Indicators unavailable: {', '.join(missing_metrics)}."],
            evidence=[],
        )

    close, sma20, sma50 = indicators.close, indicators.sma20, indicators.sma50
    rsi14, return20_pct = indicators.rsi14, indicators.return20_pct
    uptrend_is_aligned = close > sma20 > sma50
    downtrend_is_aligned = close < sma20 < sma50
    # The side being scored: short when price and the 20-day average are both under the
    # 50-day, long otherwise (a symbol with no trend is scored as the long candidate it
    # would have to become).
    side = TradeSide.SHORT if close < sma50 and sma20 < sma50 else TradeSide.LONG
    is_long = side is TradeSide.LONG
    trend_is_aligned = uptrend_is_aligned if is_long else downtrend_is_aligned
    reasons: list[str] = []

    if trend_is_aligned:
        trend_points = TREND_ALIGNED_POINTS
        reasons.append(
            "Trend aligned: close above SMA20 above SMA50."
            if is_long
            else "Downtrend aligned: close below SMA20 below SMA50."
        )
    elif not is_long:
        trend_points = ABOVE_LONG_AVERAGE_POINTS
        reasons.append("Close below SMA50 but the averages are not aligned.")
    elif close > sma50:
        trend_points = ABOVE_LONG_AVERAGE_POINTS
        reasons.append("Close above SMA50 but the averages are not aligned.")
    else:
        trend_points = 0.0
        reasons.append("Close below SMA50: no uptrend.")

    # Momentum counts in the direction of the side: gains for a long, losses for a short.
    directional_return = return20_pct if is_long else -return20_pct
    momentum_share = min(max(directional_return / FULL_MOMENTUM_RETURN_PERCENT, 0.0), 1.0)
    momentum_points = MAXIMUM_MOMENTUM_POINTS * momentum_share
    reasons.append(f"20-session return {return20_pct:+.2f}%.")

    # Strength is RSI on the right side of 50 without being stretched: 45 to 70 for a
    # long, 30 to 55 for a short.
    is_healthy = (45 <= rsi14 < 70) if is_long else (30 < rsi14 <= 55)
    is_fading = (30 < rsi14 < 45) if is_long else (55 < rsi14 < 70)
    if is_healthy:
        strength_points = HEALTHY_STRENGTH_POINTS
        reasons.append(
            f"RSI14 {rsi14:.1f} is firm without being overbought."
            if is_long
            else f"RSI14 {rsi14:.1f} is weak without being oversold."
        )
    elif is_fading:
        strength_points = RECOVERING_STRENGTH_POINTS
        reasons.append(f"RSI14 {rsi14:.1f} is soft." if is_long else f"RSI14 {rsi14:.1f} is firm.")
    else:
        strength_points = 0.0
        reasons.append(f"RSI14 {rsi14:.1f} is at an extreme.")

    relative_volume20 = indicators.relative_volume20
    has_volume_surge = relative_volume20 is not None and relative_volume20 >= VOLUME_SURGE_RATIO
    if has_volume_surge:
        participation_points = VOLUME_SURGE_POINTS
        reasons.append(
            f"Volume surge: {relative_volume20:.2f}x its 20-session average "
            f"({relative_volume20 - 1:+.0%})."
        )
    elif relative_volume20 is not None and relative_volume20 >= 1:
        participation_points = PARTICIPATION_POINTS
        reasons.append(f"Volume {relative_volume20:.2f}x its 20-session average.")
    else:
        participation_points = 0.0

    signals = find_signals(indicators, has_volume_surge)
    for signal in signals:
        confirmation = (
            "confirmed by the volume surge"
            if signal.volume_confirmed
            else "without a volume surge: unconfirmed, low probability"
        )
        reasons.append(f"{SIGNAL_DESCRIPTIONS[signal.kind]} {signal.level:.4f}, {confirmation}.")
    if indicators.prior_high252 is None:
        reasons.append(f"52-week range unavailable: {indicators.candle_count} sessions of history.")
    # A heavy-volume move against the side being scored takes the symbol off the list:
    # a breakdown for a long candidate, a breakout for a short candidate.
    has_confirmed_move_against = any(
        signal.volume_confirmed and signal.is_bullish != is_long for signal in signals
    )
    if has_confirmed_move_against:
        reasons.append(
            "Heavy-volume breakdown: not a long candidate today."
            if is_long
            else "Heavy-volume breakout: not a short candidate today."
        )

    score = round(trend_points + momentum_points + strength_points + participation_points, 4)
    is_watch = (
        trend_is_aligned and score >= WATCH_SCORE_THRESHOLD and not has_confirmed_move_against
    )
    return ScoutReport(
        symbol=symbol,
        verdict=ScoutVerdict.WATCH if is_watch else ScoutVerdict.SKIP,
        score=score,
        side=side if trend_is_aligned else None,
        volume_surge=has_volume_surge,
        signals=signals,
        reasons=reasons,
        evidence=evidence(
            indicators, "close", "sma20", "sma50", "rsi14", "return20_pct", "relative_volume20"
        ),
    )


def rank_watchlist(scout_reports: Iterable[ScoutReport]) -> list[ScoutReport]:
    """Order reports for the desk: WATCH first, best score first, missing data last.

    Within a verdict, a symbol with a volume-confirmed breakout or new high goes
    ahead of the rest, then score decides, then a plain volume surge breaks ties.
    Unconfirmed moves earn no priority.
    """
    return sorted(
        scout_reports,
        key=lambda report: (
            VERDICT_ORDER[report.verdict],
            -confirmed_bullish_signal_count(report),
            -(report.score or 0.0),
            not report.volume_surge,
            report.symbol,
        ),
    )


def confirmed_bullish_signal_count(report: ScoutReport) -> int:
    """How many volume-confirmed signals point the way this report's side does."""
    wants_bullish = report.side is not TradeSide.SHORT
    return sum(
        1
        for signal in report.signals
        if signal.volume_confirmed and signal.is_bullish == wants_bullish
    )
