"""SNOWDEN, the news analyst: dated, sourced items only.

Without supplied items SNOWDEN abstains and says what is missing; it never
invents a headline, source or timestamp. Supplied items are filtered to what
was public at the evaluation time, then split into facts and commentary.

Rules alone only list the items, so the verdict is NEUTRAL. When a headline-tone
reading is supplied (a model labels each headline, code counts the labels), the
verdict becomes BULLISH or BEARISH if enough headlines lean one way. A BEARISH
verdict makes PICARD wait; a BULLISH one adds nothing to a trade's size or odds.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from trading_desk.agents.fama import SentimentReading
from trading_desk.models import Classification, NewsItem, NewsReport

NEWS_LOOKBACK = timedelta(days=7)
# Tone needs at least this many headlines that lean one way or the other.
MINIMUM_DIRECTIONAL_HEADLINES = 5
BULLISH_TONE_SHARE = 0.6
BEARISH_TONE_SHARE = 0.4


def report(
    news_items: Sequence[NewsItem] | None,
    as_of: datetime,
    headline_tone: SentimentReading | None = None,
) -> NewsReport:
    """Build SNOWDEN's news report from supplied items, as of the evaluation time.

    Abstains when no items were supplied, or when no factual item from the last 7 days remains
    after dropping anything published after `as_of`. Splits what is left into dated facts and
    commentary and flags stale or duplicated items. Without a headline-tone reading the verdict is
    NEUTRAL; with one it is BULLISH at 60% or more bullish among directional headlines, BEARISH at
    40% or less, and needs at least five directional headlines to say either.
    """
    if news_items is None:
        return NewsReport(
            verdict=Classification.INSUFFICIENT_DATA,
            abstain=True,
            summary="No news source was supplied to the desk.",
            missing_information=["Dated news items for the symbol"],
        )
    # Point-in-time: an item published after the evaluation time did not exist yet.
    visible_items = [item for item in news_items if item.published_at <= as_of]
    recent_items = [item for item in visible_items if as_of - item.published_at <= NEWS_LOOKBACK]
    stale_item_count = len(visible_items) - len(recent_items)
    verified_facts = [item for item in recent_items if not item.is_commentary]
    commentary = [item for item in recent_items if item.is_commentary]
    red_flags = []
    if stale_item_count:
        red_flags.append(f"{stale_item_count} supplied item(s) are older than 7 days and ignored.")
    duplicate_count = len(recent_items) - len({item.headline.casefold() for item in recent_items})
    if duplicate_count:
        red_flags.append(f"{duplicate_count} duplicated headline(s) supplied.")
    if not verified_facts:
        return NewsReport(
            verdict=Classification.INSUFFICIENT_DATA,
            abstain=True,
            summary="No dated factual item from the last 7 days was supplied.",
            commentary=commentary,
            red_flags=red_flags,
            missing_information=["A recent item from a primary or reputable source"],
        )
    item_summary = (
        f"{len(verified_facts)} dated item(s) and {len(commentary)} commentary piece(s) in "
        "the last 7 days."
    )
    if headline_tone is None:
        return NewsReport(
            verdict=Classification.NEUTRAL,
            abstain=False,
            summary=f"{item_summary} Rules list them; they do not judge market impact.",
            verified_facts=verified_facts,
            commentary=commentary,
            red_flags=red_flags,
        )
    bullish_count, bearish_count = (
        headline_tone.bullish_post_count,
        headline_tone.bearish_post_count,
    )
    directional_count = bullish_count + bearish_count
    verdict = Classification.NEUTRAL
    if directional_count < MINIMUM_DIRECTIONAL_HEADLINES:
        tone_summary = (
            f"Only {directional_count} headline(s) lean either way; at least "
            f"{MINIMUM_DIRECTIONAL_HEADLINES} are needed to call the tone."
        )
    else:
        bullish_share = bullish_count / directional_count
        if bullish_share >= BULLISH_TONE_SHARE:
            verdict = Classification.BULLISH
        elif bullish_share <= BEARISH_TONE_SHARE:
            verdict = Classification.BEARISH
        tone_summary = (
            f"Headline tone is {verdict.value.lower()}: {bullish_count} bullish, "
            f"{bearish_count} bearish, {headline_tone.neutral_post_count} neutral "
            f"({headline_tone.source})."
        )
    return NewsReport(
        verdict=verdict,
        abstain=False,
        summary=f"{item_summary} {tone_summary} Tone is a model's reading, not a forecast.",
        verified_facts=verified_facts,
        commentary=commentary,
        red_flags=red_flags,
        bullish_headline_count=bullish_count,
        bearish_headline_count=bearish_count,
        neutral_headline_count=headline_tone.neutral_post_count,
    )
