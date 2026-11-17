import pytest

from trading_desk.agents import fama
from trading_desk.agents.fama import SentimentReading
from trading_desk.models import Classification, HypeRisk


def test_fama_abstains_without_a_source():
    """Verifies: fama abstains without a source."""
    sentiment_report = fama.report(None)
    assert sentiment_report.abstain
    assert sentiment_report.mood_score is None
    assert sentiment_report.hype_risk is HypeRisk.UNKNOWN


@pytest.mark.parametrize(
    "bullish,bearish,neutral",
    [(5, 4, 0), (0, 0, 50)],
)
def test_fama_abstains_on_a_thin_or_directionless_sample(bullish, bearish, neutral):
    """Verifies: fama abstains on a thin or directionless sample."""
    reading = SentimentReading(
        source="example-feed",
        bullish_post_count=bullish,
        bearish_post_count=bearish,
        neutral_post_count=neutral,
    )
    assert fama.report(reading).abstain


@pytest.mark.parametrize(
    "bullish,bearish,expected_verdict,expected_hype,expected_mood",
    [
        (90, 10, Classification.BULLISH, HypeRisk.HIGH, 90),
        (75, 25, Classification.BULLISH, HypeRisk.MEDIUM, 75),
        (50, 50, Classification.NEUTRAL, HypeRisk.LOW, 50),
        (20, 80, Classification.BEARISH, HypeRisk.LOW, 20),
    ],
)
def test_fama_scores_mood_and_treats_one_sided_optimism_as_hype(
    bullish, bearish, expected_verdict, expected_hype, expected_mood
):
    """Verifies: fama scores mood and treats one sided optimism as hype."""
    reading = SentimentReading(
        source="example-feed", bullish_post_count=bullish, bearish_post_count=bearish
    )
    sentiment_report = fama.report(reading)
    assert sentiment_report.verdict is expected_verdict
    assert sentiment_report.hype_risk is expected_hype
    assert sentiment_report.mood_score == expected_mood
    assert sentiment_report.contrarian_view
