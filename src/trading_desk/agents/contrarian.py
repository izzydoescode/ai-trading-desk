"""Independent counter-evidence rules, rather than an inversion of the analyst's label."""

from trading_desk.agents.technical import abstention, evidence
from trading_desk.models import Analysis, Classification, Indicators


def review(i: Indicators, thesis: Analysis, blockers: list[str]) -> Analysis:
    """Challenge the technical thesis independently and return the counter-case.

    Abstains when the data is blocked or the thesis abstained. Otherwise it reads momentum (RSI14
    at or above 70 is a bearish counter-scenario, at or below 30 a bullish one) and lists the risks
    the thesis ignores. It may disagree with the thesis; that disagreement is kept, not resolved
    here.
    """
    if blockers or thesis.abstain:
        return abstention("; ".join(blockers) or "Initial thesis lacks sufficient evidence.")
    risks = [
        "No current catalysts, fundamentals, liquidity, or corporate-action review supplied.",
        "A trend may reverse; gaps can exceed observed daily ranges.",
    ]
    direction = Classification.NEUTRAL
    if i.rsi14 is not None and i.rsi14 >= 70:
        direction = Classification.BEARISH
        risks.append(
            f"RSI14 {i.rsi14:.4f} is elevated: a pullback is a counter-scenario, not a forecast."
        )
    elif i.rsi14 is not None and i.rsi14 <= 30:
        direction = Classification.BULLISH
        risks.append(
            f"RSI14 {i.rsi14:.4f} is depressed: a rebound could invalidate a bearish thesis."
        )
    if (
        i.return20_pct is not None
        and i.return20_pct < 0
        and thesis.classification == Classification.BULLISH
    ):
        risks.append("Negative 20-session return conflicts with bullish moving-average ordering.")
    return Analysis(
        classification=direction,
        summary="Independent momentum and data-risk review. " + " ".join(risks),
        evidence=evidence(i, "rsi14", "atr14", "return20_pct"),
        risks=risks,
        abstain=False,
    )
