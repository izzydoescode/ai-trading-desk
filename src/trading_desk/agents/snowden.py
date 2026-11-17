"""SNOWDEN, the news analyst: dated, sourced items only.

Without supplied items SNOWDEN abstains and says what is missing; it never
invents a headline, source or timestamp. Supplied items are filtered to what
was public at the evaluation time, then split into facts and commentary.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from trading_desk.models import Classification, NewsItem, NewsReport

NEWS_LOOKBACK = timedelta(days=7)


def report(news_items: Sequence[NewsItem] | None, as_of: datetime) -> NewsReport:
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
    return NewsReport(
        verdict=Classification.NEUTRAL,
        abstain=False,
        summary=(
            f"{len(verified_facts)} dated item(s) and {len(commentary)} commentary piece(s) in "
            "the last 7 days. Rules list them; they do not judge market impact."
        ),
        verified_facts=verified_facts,
        commentary=commentary,
        red_flags=red_flags,
    )
