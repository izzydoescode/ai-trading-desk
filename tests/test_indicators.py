from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading_desk.models import Candle, MarketData
from trading_desk.tools.indicators import calculate, relative_volume, wilder


def make_candles_from_closes(prices):
    """Test helper (not a test): builds candles from closes for the tests below."""
    return [
        Candle(
            timestamp=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=n),
            open=p,
            high=p + 1,
            low=p - 1,
            close=p,
            volume=100,
        )
        for n, p in enumerate(prices)
    ]


def test_known_trend_measurements():
    """Verifies: known trend measurements."""
    i = calculate(make_candles_from_closes(list(range(100, 160))))
    assert i.sma20 == 149.5
    assert i.sma50 == 134.5
    assert i.rsi14 == 100
    assert i.atr14 == 2
    assert i.return20_pct == pytest.approx((159 / 139 - 1) * 100)


@pytest.mark.parametrize("prices,rsi", [([100] * 50, 50), (list(range(160, 100, -1)), 0)])
def test_flat_and_declining_rsi(prices, rsi):
    """Verifies: flat and declining rsi."""
    assert calculate(make_candles_from_closes(prices)).rsi14 == rsi


def test_wilder_recursive_smoothing():
    """Verifies: wilder recursive smoothing."""
    assert wilder([2, 4, 6, 10], 3) == pytest.approx(6)


def test_insufficient_history():
    """Verifies: insufficient history."""
    assert calculate([]).close is None
    assert calculate(make_candles_from_closes([100] * 14)).rsi14 is None
    assert calculate(make_candles_from_closes([100] * 49)).sma50 is None


def test_reject_nonfinite_prices_and_inconsistent_ohlc():
    """Verifies: reject nonfinite prices and inconsistent ohlc."""
    with pytest.raises(ValidationError):
        make_candles_from_closes([float("nan")])
    with pytest.raises(ValidationError):
        make_candles_from_closes([100])[0].model_validate(
            dict(timestamp="2025-01-01T00:00:00Z", open=100, close=101, low=99, high=100, volume=1)
        )


def test_duplicate_and_unordered_data_rejected():
    """Verifies: duplicate and unordered data rejected."""
    c = make_candles_from_closes([100, 101])
    for invalid in ([c[0], c[0]], c[::-1]):
        with pytest.raises(ValidationError):
            MarketData(symbol="AAPL", source="test", candles=invalid)


def test_rsi_matches_wilder_worked_example():
    # Wilder's commonly published 14-period example; rounded reference tolerance.
    """Verifies: rsi matches wilder worked example."""
    prices = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
        46.41,
        46.22,
        45.64,
        46.21,
    ]
    assert calculate(make_candles_from_closes(prices[:15])).rsi14 == pytest.approx(
        70.4641, abs=0.0001
    )
    assert calculate(make_candles_from_closes(prices)).rsi14 == pytest.approx(62.8807, abs=0.0001)


def test_twenty_session_range_uses_the_latest_twenty_bars_only():
    """Verifies: twenty session range uses the latest twenty bars only."""
    indicators = calculate(make_candles_from_closes([500] + list(range(100, 125))))
    assert indicators.high20 == 125
    assert indicators.low20 == 104


def test_range_and_relative_volume_need_enough_history():
    """Verifies: range and relative volume need enough history."""
    indicators = calculate(make_candles_from_closes(list(range(100, 119))))
    assert indicators.high20 is None
    assert indicators.low20 is None
    assert indicators.relative_volume20 is None


def test_relative_volume_compares_latest_session_to_prior_average():
    """Verifies: relative volume compares latest session to prior average."""
    candles = make_candles_from_closes([100] * 21)
    candles[-1] = candles[-1].model_copy(update={"volume": 250})
    assert relative_volume(candles, 20) == pytest.approx(2.5)
    assert calculate(candles).relative_volume20 == pytest.approx(2.5)


def test_relative_volume_is_unavailable_when_prior_sessions_have_no_volume():
    """Verifies: relative volume is unavailable when prior sessions have no volume."""
    candles = [
        candle.model_copy(update={"volume": 0}) for candle in make_candles_from_closes([100] * 21)
    ]
    assert relative_volume(candles, 20) is None


def make_candles_from_ranges(rows):
    """Test helper (not a test): candles from (high, low, close) rows on consecutive days."""
    first_day = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=first_day + timedelta(days=index),
            open=close,
            high=high,
            low=low,
            close=close,
            volume=1_000,
        )
        for index, (high, low, close) in enumerate(rows)
    ]


def test_prior_levels_exclude_the_latest_session_so_it_can_break_them():
    """Verifies: prior levels exclude the latest session so it can break them."""
    from trading_desk.tools.indicators import prior_extreme

    rows = [(10 + index, 5 + index, 8 + index) for index in range(21)]  # rising highs and lows
    rows.append((100.0, 1.0, 50.0))  # the latest bar is extreme in both directions
    candles = make_candles_from_ranges(rows)
    assert prior_extreme(candles, 20, highest=True) == 30.0  # highest of the 20 before today
    assert prior_extreme(candles, 20, highest=False) == 6.0
    measurements = calculate(candles)
    assert measurements.prior_high20 == 30.0
    assert (measurements.latest_high, measurements.latest_low) == (100.0, 1.0)
    assert (measurements.previous_high, measurements.previous_low) == (30.0, 25.0)
    # The day's change is the latest close (50) against the previous close (28).
    assert measurements.previous_close == 28.0
    assert measurements.day_change_pct == pytest.approx((50.0 / 28.0 - 1) * 100)
    # high20 still includes today; prior_high20 does not.
    assert measurements.high20 == 100.0


@pytest.mark.parametrize(
    "session_count,has20,has55,has252",
    [
        (1, False, False, False),
        (20, False, False, False),
        (21, True, False, False),
        (56, True, True, False),
        (252, True, True, False),
        (253, True, True, True),
    ],
)
def test_longer_windows_stay_unavailable_until_there_is_enough_history(
    session_count, has20, has55, has252
):
    """Verifies: longer windows stay unavailable until there is enough history."""
    measurements = calculate(make_candles_from_ranges([(11.0, 9.0, 10.0)] * session_count))
    assert (measurements.prior_high20 is not None) is has20
    assert (measurements.prior_high55 is not None) is has55
    assert (measurements.prior_high252 is not None) is has252
    assert (measurements.prior_low252 is not None) is has252
    assert (measurements.previous_high is not None) is (session_count >= 2)
    assert (measurements.previous_close is not None) is (session_count >= 2)
    assert (measurements.day_change_pct is not None) is (session_count >= 2)


def test_bollinger_bands_are_two_standard_deviations_around_the_twenty_session_average():
    """Verifies: bollinger bands are two standard deviations around the twenty session average."""
    from trading_desk.tools.indicators import bollinger_bands

    closes = [10.0, 12.0] * 10  # average 11, population standard deviation 1
    upper_band, lower_band = bollinger_bands(closes)
    assert (upper_band, lower_band) == pytest.approx((13.0, 9.0))
    assert bollinger_bands(closes[:19]) == (None, None)
    assert bollinger_bands([7.0] * 20) == pytest.approx((7.0, 7.0))
    # Only the latest twenty closes count.
    assert bollinger_bands([500.0] * 5 + closes) == pytest.approx((13.0, 9.0))
    measurements = calculate(make_candles_from_closes(closes))
    assert (measurements.bollinger_upper20, measurements.bollinger_lower20) == pytest.approx(
        (13.0, 9.0)
    )
