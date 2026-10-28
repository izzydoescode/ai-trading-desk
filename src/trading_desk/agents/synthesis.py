"""Conservative reconciliation preserves dissent instead of voting it away."""

from trading_desk.agents.technical import abstention
from trading_desk.models import Analysis, Classification


def synthesize(technical: Analysis, contrarian: Analysis, blockers: list[str]) -> Analysis:
    if blockers or technical.abstain or contrarian.abstain:
        return abstention("; ".join(blockers) or "A required analysis abstained.")
    conflict = {technical.classification, contrarian.classification} == {
        Classification.BULLISH,
        Classification.BEARISH,
    }
    return Analysis(
        classification=Classification.NEUTRAL if conflict else technical.classification,
        summary=(
            (
                "Directional disagreement remains unresolved; retain a neutral research stance. "
                if conflict
                else "Historical trend is the initial research stance. "
            )
            + "Technical: "
            + technical.summary[:1300]
            + " Counter-review: "
            + contrarian.summary[:1300]
        )[:3000],
        evidence=technical.evidence,
        risks=list(dict.fromkeys(technical.risks + contrarian.risks)),
        abstain=False,
    )
