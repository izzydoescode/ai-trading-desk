"""FAMA, the sentiment analyst: crowd mood, kept separate from evidence.

FAMA never claims to have searched social platforms. Without supplied readings
it abstains. Popularity is not proof: a very high bullish share raises hype risk
rather than conviction.
"""

from pydantic import Field

from trading_desk.models import Classification, HypeRisk, Model, SentimentReport

MINIMUM_POST_COUNT = 20
HIGH_HYPE_BULLISH_SHARE = 0.85
MEDIUM_HYPE_BULLISH_SHARE = 0.7
BULLISH_MOOD_SCORE = 60
BEARISH_MOOD_SCORE = 40


class SentimentReading(Model):
    """Counts supplied by a sentiment source for one symbol and period."""

    source: str = Field(min_length=1)
    bullish_post_count: int = Field(ge=0)
    bearish_post_count: int = Field(ge=0)
    neutral_post_count: int = Field(default=0, ge=0)
    # What is being counted: "post" for a social feed, "headline" for news tone.
    item_label: str = Field(default="post", min_length=1)


def report(sentiment_reading: SentimentReading | None) -> SentimentReport:
    """Turn a sentiment reading into FAMA's report: mood score, hype risk and verdict.

    Abstains with no reading, or when fewer than 20 items were counted or none lean either way.
    Mood is the bullish share of directional items (0-100). A bullish share of 85% or more is HIGH
    hype risk and 70% or more is MEDIUM: one-sided optimism raises caution, not conviction.
    """
    if sentiment_reading is None:
        return SentimentReport(
            verdict=Classification.INSUFFICIENT_DATA,
            abstain=True,
            summary="No sentiment source was supplied to the desk.",
            contrarian_view="Unknown crowd positioning is itself a risk.",
        )
    item_label = sentiment_reading.item_label
    directional_post_count = (
        sentiment_reading.bullish_post_count + sentiment_reading.bearish_post_count
    )
    total_post_count = directional_post_count + sentiment_reading.neutral_post_count
    if total_post_count < MINIMUM_POST_COUNT or directional_post_count == 0:
        return SentimentReport(
            verdict=Classification.INSUFFICIENT_DATA,
            abstain=True,
            summary=(
                f"Only {total_post_count} {item_label}(s) from {sentiment_reading.source}; "
                f"at least {MINIMUM_POST_COUNT} with a stated direction are needed."
            ),
            contrarian_view="A thin sample says little about the crowd.",
        )
    bullish_share = sentiment_reading.bullish_post_count / directional_post_count
    mood_score = round(bullish_share * 100)
    if bullish_share >= HIGH_HYPE_BULLISH_SHARE:
        hype_risk = HypeRisk.HIGH
    elif bullish_share >= MEDIUM_HYPE_BULLISH_SHARE:
        hype_risk = HypeRisk.MEDIUM
    else:
        hype_risk = HypeRisk.LOW
    if mood_score >= BULLISH_MOOD_SCORE:
        verdict = Classification.BULLISH
    elif mood_score <= BEARISH_MOOD_SCORE:
        verdict = Classification.BEARISH
    else:
        verdict = Classification.NEUTRAL
    return SentimentReport(
        verdict=verdict,
        abstain=False,
        summary=(
            f"{mood_score} of 100 bullish across {directional_post_count} directional "
            f"{item_label}s from {sentiment_reading.source}. Engagement is not evidence."
        ),
        mood_score=mood_score,
        hype_risk=hype_risk,
        reasons=[
            f"{sentiment_reading.bullish_post_count} bullish {item_label}s",
            f"{sentiment_reading.bearish_post_count} bearish {item_label}s",
            f"{sentiment_reading.neutral_post_count} neutral {item_label}s",
        ],
        contrarian_view=(
            "One-sided optimism often marks crowded positioning."
            if hype_risk is not HypeRisk.LOW
            else "The crowd is not one-sided; sentiment adds little either way."
        ),
    )
