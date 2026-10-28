from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading_desk.models import Candle, MarketData
from trading_desk.tools.indicators import calculate, wilder


def bars(prices):
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
    i = calculate(bars(list(range(100, 160))))
    assert i.sma20 == 149.5
    assert i.sma50 == 134.5
    assert i.rsi14 == 100
    assert i.atr14 == 2
    assert i.return20_pct == pytest.approx((159 / 139 - 1) * 100)


@pytest.mark.parametrize("prices,rsi", [([100] * 50, 50), (list(range(160, 100, -1)), 0)])
def test_flat_and_declining_rsi(prices, rsi):
    assert calculate(bars(prices)).rsi14 == rsi


def test_wilder_recursive_smoothing():
    assert wilder([2, 4, 6, 10], 3) == pytest.approx(6)


def test_insufficient_history():
    assert calculate([]).close is None
    assert calculate(bars([100] * 14)).rsi14 is None
    assert calculate(bars([100] * 49)).sma50 is None


def test_reject_nonfinite_prices_and_inconsistent_ohlc():
    with pytest.raises(ValidationError):
        bars([float("nan")])
    with pytest.raises(ValidationError):
        bars([100])[0].model_validate(
            dict(timestamp="2025-01-01T00:00:00Z", open=100, close=101, low=99, high=100, volume=1)
        )


def test_duplicate_and_unordered_data_rejected():
    c = bars([100, 101])
    for invalid in ([c[0], c[0]], c[::-1]):
        with pytest.raises(ValidationError):
            MarketData(symbol="AAPL", source="test", candles=invalid)


def test_rsi_matches_wilder_worked_example():
    # Wilder's commonly published 14-period example; rounded reference tolerance.
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
    assert calculate(bars(prices[:15])).rsi14 == pytest.approx(70.4641, abs=0.0001)
    assert calculate(bars(prices)).rsi14 == pytest.approx(62.8807, abs=0.0001)
