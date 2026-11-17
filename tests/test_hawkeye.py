import pytest

from trading_desk.agents.hawkeye import WATCH_SCORE_THRESHOLD, rank_watchlist, scout
from trading_desk.models import Indicators, ScoutReport, ScoutVerdict


def make_indicators(**overrides):
    """Test helper (not a test): builds indicators for the tests below."""
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
    """Verifies: strong aligned trend scores full marks and is watched."""
    report = scout("AAPL", make_indicators(), blockers=[])
    assert report.verdict is ScoutVerdict.WATCH
    assert report.score == pytest.approx(1.0)
    assert {item.metric for item in report.evidence} >= {"close", "sma20", "sma50", "rsi14"}


def test_downtrend_is_skipped_with_the_reason_stated():
    """Verifies: downtrend is skipped with the reason stated."""
    report = scout("NVDA", make_indicators(close=90.0, return20_pct=-12.0, rsi14=25.0), blockers=[])
    assert report.verdict is ScoutVerdict.SKIP
    assert report.score == pytest.approx(0.1)
    assert any("below SMA50" in reason for reason in report.reasons)


def test_high_score_without_aligned_averages_is_not_watched():
    """Verifies: high score without aligned averages is not watched."""
    report = scout("MSFT", make_indicators(close=104.0, sma20=106.0), blockers=[])
    assert report.score >= WATCH_SCORE_THRESHOLD
    assert report.verdict is ScoutVerdict.SKIP


def test_overbought_and_quiet_volume_earn_no_points():
    """Verifies: overbought and quiet volume earn no points."""
    report = scout("TSLA", make_indicators(rsi14=82.0, relative_volume20=0.4), blockers=[])
    assert report.score == pytest.approx(0.7)
    assert any("extreme" in reason for reason in report.reasons)


def test_soft_strength_earns_partial_points_and_negative_return_earns_none():
    """Verifies: soft strength earns partial points and negative return earns none."""
    report = scout("AMD", make_indicators(rsi14=40.0, return20_pct=-3.0), blockers=[])
    assert report.score == pytest.approx(0.6)


def test_blockers_mean_more_data_is_needed_and_no_score_is_invented():
    """Verifies: blockers mean more data is needed and no score is invented."""
    report = scout("AAPL", make_indicators(), blockers=["Stale daily data"])
    assert report.verdict is ScoutVerdict.NEED_MORE_DATA
    assert report.score is None
    assert report.reasons == ["Stale daily data"]


def test_missing_indicators_are_named():
    """Verifies: missing indicators are named."""
    report = scout("AAPL", make_indicators(sma50=None, rsi14=None), blockers=[])
    assert report.verdict is ScoutVerdict.NEED_MORE_DATA
    assert report.reasons == ["Indicators unavailable: sma50, rsi14."]


def test_ranking_puts_watch_first_by_score_and_missing_data_last():
    """Verifies: ranking puts watch first by score and missing data last."""

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
    """Verifies: volume surge starts at fifty percent above the average."""
    report = scout("AAPL", make_indicators(relative_volume20=relative_volume), blockers=[])
    assert report.volume_surge is is_surge
    assert report.score == pytest.approx(expected_score)
    assert any("Volume surge" in reason for reason in report.reasons) is is_surge


def test_volume_surge_reason_states_the_size_of_the_surge():
    """Verifies: volume surge reason states the size of the surge."""
    report = scout("AAPL", make_indicators(relative_volume20=1.8), blockers=[])
    assert "Volume surge: 1.80x its 20-session average (+80%)." in report.reasons


def test_a_surge_alone_does_not_make_a_downtrend_worth_watching():
    """Verifies: a surge alone does not make a downtrend worth watching."""
    report = scout("NVDA", make_indicators(close=95.0, relative_volume20=4.0), blockers=[])
    assert report.volume_surge
    assert report.verdict is ScoutVerdict.SKIP


def test_missing_data_never_reports_a_surge():
    """Verifies: missing data never reports a surge."""
    report = scout("AAPL", make_indicators(), blockers=["Stale daily data"])
    assert report.volume_surge is False


def test_between_equal_scores_the_surging_symbol_ranks_first():
    """Verifies: between equal scores the surging symbol ranks first."""

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
    """Test helper (not a test): signal kinds."""
    return {signal.kind.value: signal.volume_confirmed for signal in report.signals}


def test_new_fifty_two_week_high_with_volume_is_a_confirmed_signal():
    """Verifies: new fifty two week high with volume is a confirmed signal."""
    report = scout(
        "NVDA",
        make_indicators(prior_high252=108.0, latest_high=111.0, relative_volume20=1.8),
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
    """Verifies: the same high without volume is recorded as unconfirmed and low probability."""
    report = scout(
        "NVDA",
        make_indicators(prior_high252=108.0, latest_high=111.0, relative_volume20=0.9),
        blockers=[],
    )
    assert signal_kinds(report) == {"NEW_52_WEEK_HIGH": False}
    assert any("unconfirmed, low probability" in reason for reason in report.reasons)


def test_close_above_the_prior_days_high_is_a_breakout_and_an_intraday_poke_is_not():
    """Verifies: close above the prior days high is a breakout and an intraday poke is not."""
    breakout = scout("AAPL", make_indicators(previous_high=109.0), blockers=[])
    assert signal_kinds(breakout) == {"BREAKOUT_ABOVE_PRIOR_DAY_HIGH": True}
    poke = scout("AAPL", make_indicators(previous_high=110.5, latest_high=112.0), blockers=[])
    assert signal_kinds(poke) == {}
    exactly_at_the_level = scout("AAPL", make_indicators(previous_high=110.0), blockers=[])
    assert signal_kinds(exactly_at_the_level) == {}


def test_heavy_volume_breakdown_takes_an_uptrend_off_the_watchlist():
    """Verifies: heavy volume breakdown takes an uptrend off the watchlist."""
    confirmed = scout("AAPL", make_indicators(previous_low=111.0), blockers=[])
    assert signal_kinds(confirmed) == {"BREAKDOWN_BELOW_PRIOR_DAY_LOW": True}
    assert confirmed.score >= WATCH_SCORE_THRESHOLD
    assert confirmed.verdict is ScoutVerdict.SKIP
    assert "Heavy-volume breakdown: not a long candidate today." in confirmed.reasons
    quiet = scout("AAPL", make_indicators(previous_low=111.0, relative_volume20=0.8), blockers=[])
    assert signal_kinds(quiet) == {"BREAKDOWN_BELOW_PRIOR_DAY_LOW": False}
    assert quiet.verdict is ScoutVerdict.WATCH


def test_new_fifty_two_week_low_is_reported_for_a_falling_symbol():
    """Verifies: new fifty two week low is reported for a falling symbol."""
    report = scout(
        "INTC",
        make_indicators(close=80.0, prior_low252=85.0, latest_low=79.0, previous_low=82.0),
        blockers=[],
    )
    assert signal_kinds(report) == {
        "NEW_52_WEEK_LOW": True,
        "BREAKDOWN_BELOW_PRIOR_DAY_LOW": True,
    }
    assert all(not signal.is_bullish for signal in report.signals)
    assert report.verdict is ScoutVerdict.SKIP


def test_short_history_says_the_yearly_range_is_unavailable_instead_of_guessing():
    """Verifies: short history says the yearly range is unavailable instead of guessing."""
    report = scout("AAPL", make_indicators(), blockers=[])
    assert report.signals == []
    assert "52-week range unavailable: 80 sessions of history." in report.reasons
    with_a_year = scout(
        "AAPL", make_indicators(prior_high252=500.0, latest_high=111.0), blockers=[]
    )
    assert not any("unavailable" in reason for reason in with_a_year.reasons)


def test_a_confirmed_breakout_outranks_a_higher_score_and_an_unconfirmed_one_does_not():
    """Verifies: a confirmed breakout outranks a higher score and an unconfirmed one does not."""
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
