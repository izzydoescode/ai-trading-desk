import pytest

from trading_desk.agents.hawkeye import (
    BIG_GAINER_MINIMUM_PERCENT,
    WATCH_SCORE_THRESHOLD,
    rank_watchlist,
    scout,
)
from trading_desk.models import Indicators, ScoutReport, ScoutVerdict


def indicators(**overrides):
    values = {
        "candle_count": 80,
        "close": 110.0,
        "sma20": 105.0,
        "sma50": 100.0,
        "rsi14": 60.0,
        "atr14": 2.0,
        "return20_pct": 10.0,
        "relative_volume20": 1.5,
        "high20": 111.0,
        "low20": 99.0,
    }
    return Indicators(**{**values, **overrides})


def test_strong_aligned_trend_scores_full_marks_and_is_watched():
    report = scout("AAPL", indicators(), blockers=[])
    assert report.verdict is ScoutVerdict.WATCH
    assert report.score == pytest.approx(1.0)
    assert {item.metric for item in report.evidence} >= {"close", "sma20", "sma50", "rsi14"}


def test_downtrend_is_skipped_with_the_reason_stated():
    report = scout("NVDA", indicators(close=90.0, return20_pct=-12.0, rsi14=25.0), blockers=[])
    assert report.verdict is ScoutVerdict.SKIP
    assert report.score == pytest.approx(0.1)
    assert any("below SMA50" in reason for reason in report.reasons)


def test_high_score_without_aligned_averages_is_not_watched():
    report = scout("MSFT", indicators(close=104.0, sma20=106.0), blockers=[])
    assert report.score >= WATCH_SCORE_THRESHOLD
    assert report.verdict is ScoutVerdict.SKIP


def test_overbought_and_quiet_volume_earn_no_points():
    report = scout("TSLA", indicators(rsi14=82.0, relative_volume20=0.4), blockers=[])
    assert report.score == pytest.approx(0.7)
    assert any("extreme" in reason for reason in report.reasons)


def test_soft_strength_earns_partial_points_and_negative_return_earns_none():
    report = scout("AMD", indicators(rsi14=40.0, return20_pct=-3.0), blockers=[])
    assert report.score == pytest.approx(0.6)


def test_blockers_mean_more_data_is_needed_and_no_score_is_invented():
    report = scout("AAPL", indicators(), blockers=["Stale daily data"])
    assert report.verdict is ScoutVerdict.NEED_MORE_DATA
    assert report.score is None
    assert report.reasons == ["Stale daily data"]


def test_missing_indicators_are_named():
    report = scout("AAPL", indicators(sma50=None, rsi14=None), blockers=[])
    assert report.verdict is ScoutVerdict.NEED_MORE_DATA
    assert report.reasons == ["Indicators unavailable: sma50, rsi14."]


def test_ranking_puts_watch_first_by_score_and_missing_data_last():
    def scout_report(symbol, verdict, score=None):
        return ScoutReport(symbol=symbol, verdict=verdict, score=score, reasons=["r"], evidence=[])

    ranked = rank_watchlist(
        [
            scout_report("SKIPHIGH", ScoutVerdict.SKIP, 0.9),
            scout_report("NODATA", ScoutVerdict.NEED_MORE_DATA),
            scout_report("WATCHLOW", ScoutVerdict.WATCH, 0.65),
            scout_report("WATCHTOP", ScoutVerdict.WATCH, 0.95),
            scout_report("SKIPLOW", ScoutVerdict.SKIP, 0.1),
        ]
    )
    assert [report.symbol for report in ranked] == [
        "WATCHTOP",
        "WATCHLOW",
        "SKIPHIGH",
        "SKIPLOW",
        "NODATA",
    ]


@pytest.mark.parametrize(
    "relative_volume,is_surge,expected_score",
    [
        (None, False, 0.9),
        (0.99, False, 0.9),
        (1.0, False, 0.95),
        (1.49, False, 0.95),
        (1.5, True, 1.0),
        (3.2, True, 1.0),
    ],
)
def test_volume_surge_starts_at_fifty_percent_above_the_average(
    relative_volume, is_surge, expected_score
):
    report = scout("AAPL", indicators(relative_volume20=relative_volume), blockers=[])
    assert report.volume_surge is is_surge
    assert report.score == pytest.approx(expected_score)
    assert any("Volume surge" in reason for reason in report.reasons) is is_surge


def test_volume_surge_reason_states_the_size_of_the_surge():
    report = scout("AAPL", indicators(relative_volume20=1.8), blockers=[])
    assert "Volume surge: 1.80x its 20-session average (+80%)." in report.reasons


def test_a_surge_alone_does_not_make_a_downtrend_worth_watching():
    report = scout("NVDA", indicators(close=95.0, relative_volume20=4.0), blockers=[])
    assert report.volume_surge
    assert report.verdict is ScoutVerdict.SKIP


def test_missing_data_never_reports_a_surge():
    report = scout("AAPL", indicators(), blockers=["Stale daily data"])
    assert report.volume_surge is False


def test_between_equal_scores_the_surging_symbol_ranks_first():
    def report(symbol, volume_surge):
        return ScoutReport(
            symbol=symbol,
            verdict=ScoutVerdict.WATCH,
            score=0.8,
            volume_surge=volume_surge,
            reasons=["r"],
            evidence=[],
        )

    ranked = rank_watchlist([report("AAA", False), report("ZZZ", True), report("MMM", False)])
    assert [scout_report.symbol for scout_report in ranked] == ["ZZZ", "AAA", "MMM"]


def signal_kinds(report):
    return {signal.kind.value: signal.volume_confirmed for signal in report.signals}


def test_new_fifty_two_week_high_with_volume_is_a_confirmed_signal():
    report = scout(
        "NVDA",
        indicators(prior_high252=108.0, latest_high=111.0, relative_volume20=1.8),
        blockers=[],
    )
    assert signal_kinds(report) == {"NEW_52_WEEK_HIGH": True}
    assert report.signals[0].level == 108.0 and report.signals[0].is_bullish
    assert any(
        reason == "New 52-week high above 108.0000, confirmed by the volume surge."
        for reason in report.reasons
    )
    assert report.verdict is ScoutVerdict.WATCH


def test_the_same_high_without_volume_is_recorded_as_unconfirmed_and_low_probability():
    report = scout(
        "NVDA",
        indicators(prior_high252=108.0, latest_high=111.0, relative_volume20=0.9),
        blockers=[],
    )
    assert signal_kinds(report) == {"NEW_52_WEEK_HIGH": False}
    assert any("unconfirmed, low probability" in reason for reason in report.reasons)


def test_a_close_thirty_five_percent_above_the_previous_close_is_a_big_gainer():
    """Verifies: a one-day gain at or above the 35% minimum is a bullish signal that
    carries the gain and the previous close, and anything smaller is no signal."""
    gainer = scout("AAPL", indicators(previous_close=80.0, day_change_pct=37.5), blockers=[])
    assert signal_kinds(gainer) == {"BIG_PERCENT_GAINER": True}
    signal = gainer.signals[0]
    assert (signal.level, signal.change_pct, signal.is_bullish) == (80.0, 37.5, True)
    assert (
        "Big % gainer: +37.50% from the previous close 80.0000, confirmed by the volume surge."
        in gainer.reasons
    )
    exactly_at_the_minimum = scout(
        "AAPL", indicators(previous_close=81.4815, day_change_pct=BIG_GAINER_MINIMUM_PERCENT), []
    )
    assert signal_kinds(exactly_at_the_minimum) == {"BIG_PERCENT_GAINER": True}
    just_short = scout("AAPL", indicators(previous_close=81.5, day_change_pct=34.99), blockers=[])
    assert signal_kinds(just_short) == {}


def test_a_big_gain_without_volume_is_unconfirmed_and_a_close_above_yesterdays_high_is_nothing():
    """Verifies: a big gainer on quiet volume is recorded as unconfirmed, and merely
    closing above the prior day's high (the retired breakout rule) raises no signal."""
    quiet_gainer = scout(
        "AAPL",
        indicators(previous_close=80.0, day_change_pct=37.5, relative_volume20=0.9),
        blockers=[],
    )
    assert signal_kinds(quiet_gainer) == {"BIG_PERCENT_GAINER": False}
    assert any("unconfirmed, low probability" in reason for reason in quiet_gainer.reasons)
    above_yesterdays_high = scout(
        "AAPL", indicators(previous_high=109.0, previous_close=108.0, day_change_pct=1.85), []
    )
    assert signal_kinds(above_yesterdays_high) == {}


def test_only_a_big_gainer_signal_may_carry_a_gain():
    """Verifies: the signal contract rejects a big gainer without its gain and any
    other signal that claims one."""
    from pydantic import ValidationError

    from trading_desk.models import ScoutSignal

    with pytest.raises(ValidationError):
        ScoutSignal(kind="BIG_PERCENT_GAINER", level=80.0, is_bullish=True, volume_confirmed=True)
    with pytest.raises(ValidationError):
        ScoutSignal(
            kind="NEW_52_WEEK_HIGH",
            level=80.0,
            is_bullish=True,
            volume_confirmed=True,
            change_pct=40.0,
        )


def test_heavy_volume_breakdown_takes_an_uptrend_off_the_watchlist():
    confirmed = scout("AAPL", indicators(previous_low=111.0), blockers=[])
    assert signal_kinds(confirmed) == {"BREAKDOWN_BELOW_PRIOR_DAY_LOW": True}
    assert confirmed.score >= WATCH_SCORE_THRESHOLD
    assert confirmed.verdict is ScoutVerdict.SKIP
    assert "Heavy-volume breakdown: not a long candidate today." in confirmed.reasons
    quiet = scout("AAPL", indicators(previous_low=111.0, relative_volume20=0.8), blockers=[])
    assert signal_kinds(quiet) == {"BREAKDOWN_BELOW_PRIOR_DAY_LOW": False}
    assert quiet.verdict is ScoutVerdict.WATCH


def test_new_fifty_two_week_low_is_reported_for_a_falling_symbol():
    report = scout(
        "INTC",
        indicators(close=80.0, prior_low252=85.0, latest_low=79.0, previous_low=82.0),
        blockers=[],
    )
    assert signal_kinds(report) == {
        "NEW_52_WEEK_LOW": True,
        "BREAKDOWN_BELOW_PRIOR_DAY_LOW": True,
    }
    assert all(not signal.is_bullish for signal in report.signals)
    assert report.verdict is ScoutVerdict.SKIP


def test_short_history_says_the_yearly_range_is_unavailable_instead_of_guessing():
    report = scout("AAPL", indicators(), blockers=[])
    assert report.signals == []
    assert "52-week range unavailable: 80 sessions of history." in report.reasons
    with_a_year = scout("AAPL", indicators(prior_high252=500.0, latest_high=111.0), blockers=[])
    assert not any("unavailable" in reason for reason in with_a_year.reasons)


def test_a_confirmed_big_gainer_outranks_a_higher_score_and_an_unconfirmed_one_does_not():
    from trading_desk.models import ScoutSignal

    def report(symbol, score, volume_confirmed=None):
        signals = (
            []
            if volume_confirmed is None
            else [
                ScoutSignal(
                    kind="NEW_52_WEEK_HIGH",
                    level=100.0,
                    is_bullish=True,
                    volume_confirmed=volume_confirmed,
                )
            ]
        )
        return ScoutReport(
            symbol=symbol,
            verdict=ScoutVerdict.WATCH,
            score=score,
            signals=signals,
            reasons=["r"],
            evidence=[],
        )

    ranked = rank_watchlist(
        [report("PLAIN", 0.9), report("QUIET", 0.8, False), report("LOUD", 0.7, True)]
    )
    assert [scout_report.symbol for scout_report in ranked] == ["LOUD", "PLAIN", "QUIET"]
