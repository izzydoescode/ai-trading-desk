"""HAWKEYE, the market scout: ranks symbols by how much attention they deserve.

A transparent, uncalibrated scoring rule over measured indicators. It only decides
what the desk looks at next; it never proposes a trade. Long setups only for now.
"""

from collections.abc import Iterable

from trading_desk.agents.technical import evidence
from trading_desk.models import Indicators, ScoutReport, ScoutVerdict

WATCH_SCORE_THRESHOLD = 0.6
TREND_ALIGNED_POINTS = 0.4
ABOVE_LONG_AVERAGE_POINTS = 0.2
MAXIMUM_MOMENTUM_POINTS = 0.3
FULL_MOMENTUM_RETURN_PERCENT = 10.0
HEALTHY_STRENGTH_POINTS = 0.2
RECOVERING_STRENGTH_POINTS = 0.1
PARTICIPATION_POINTS = 0.1
VERDICT_ORDER = {ScoutVerdict.WATCH: 0, ScoutVerdict.SKIP: 1, ScoutVerdict.NEED_MORE_DATA: 2}


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
    trend_is_aligned = close > sma20 > sma50
    reasons: list[str] = []

    if trend_is_aligned:
        trend_points = TREND_ALIGNED_POINTS
        reasons.append("Trend aligned: close above SMA20 above SMA50.")
    elif close > sma50:
        trend_points = ABOVE_LONG_AVERAGE_POINTS
        reasons.append("Close above SMA50 but the averages are not aligned.")
    else:
        trend_points = 0.0
        reasons.append("Close below SMA50: no uptrend.")

    momentum_share = min(max(return20_pct / FULL_MOMENTUM_RETURN_PERCENT, 0.0), 1.0)
    momentum_points = MAXIMUM_MOMENTUM_POINTS * momentum_share
    reasons.append(f"20-session return {return20_pct:+.2f}%.")

    if 45 <= rsi14 < 70:
        strength_points = HEALTHY_STRENGTH_POINTS
        reasons.append(f"RSI14 {rsi14:.1f} is firm without being overbought.")
    elif 30 < rsi14 < 45:
        strength_points = RECOVERING_STRENGTH_POINTS
        reasons.append(f"RSI14 {rsi14:.1f} is soft.")
    else:
        strength_points = 0.0
        reasons.append(f"RSI14 {rsi14:.1f} is at an extreme.")

    relative_volume20 = indicators.relative_volume20
    if relative_volume20 is not None and relative_volume20 >= 1:
        participation_points = PARTICIPATION_POINTS
        reasons.append(f"Volume {relative_volume20:.2f}x its 20-session average.")
    else:
        participation_points = 0.0

    score = round(trend_points + momentum_points + strength_points + participation_points, 4)
    is_watch = trend_is_aligned and score >= WATCH_SCORE_THRESHOLD
    return ScoutReport(
        symbol=symbol,
        verdict=ScoutVerdict.WATCH if is_watch else ScoutVerdict.SKIP,
        score=score,
        reasons=reasons,
        evidence=evidence(
            indicators, "close", "sma20", "sma50", "rsi14", "return20_pct", "relative_volume20"
        ),
    )


def rank_watchlist(scout_reports: Iterable[ScoutReport]) -> list[ScoutReport]:
    """Order reports for the desk: WATCH first, best score first, missing data last."""
    return sorted(
        scout_reports,
        key=lambda report: (VERDICT_ORDER[report.verdict], -(report.score or 0.0), report.symbol),
    )
