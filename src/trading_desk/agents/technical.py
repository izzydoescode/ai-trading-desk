"""A transparent baseline; the LLM path receives these same measured inputs."""

from trading_desk.models import Analysis, Classification, Evidence, Indicators


def abstention(reason: str) -> Analysis:
    return Analysis(
        classification=Classification.INSUFFICIENT_DATA,
        summary=reason,
        evidence=[],
        risks=[reason],
        abstain=True,
    )


def evidence(i: Indicators, *metrics: str) -> list[Evidence]:
    return [Evidence(metric=m, value=getattr(i, m)) for m in metrics if getattr(i, m) is not None]


def analyze(i: Indicators) -> Analysis:
    if i.sma50 is None or i.close is None or i.sma20 is None:
        return abstention("Need at least 50 valid daily candles for SMA50 and trend assessment.")
    bullish = i.close > i.sma20 > i.sma50
    bearish = i.close < i.sma20 < i.sma50
    direction = (
        Classification.BULLISH
        if bullish
        else Classification.BEARISH
        if bearish
        else Classification.NEUTRAL
    )
    return Analysis(
        classification=direction,
        summary=f"Observed close {i.close:.4f}, SMA20 {i.sma20:.4f}, SMA50 {i.sma50:.4f}. "
        "Moving-average ordering describes historical trend; continuation is uncertain.",
        evidence=evidence(i, "close", "sma20", "sma50", "rsi14", "atr14", "return20_pct"),
        risks=["Lagging indicators cannot establish future returns or identify event risk."],
        abstain=False,
    )
