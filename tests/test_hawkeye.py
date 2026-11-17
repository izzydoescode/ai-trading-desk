import pytest

from trading_desk.agents.hawkeye import WATCH_SCORE_THRESHOLD, rank_watchlist, scout
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
