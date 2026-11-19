"""SMA, Wilder RSI/ATR, and close-to-close returns, computed without an LLM."""

from trading_desk.models import Candle, Indicators


def wilder(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (result * (period - 1) + value) / period
    return result


RANGE_LOOKBACK_SESSIONS = 20
BREAKOUT_LOOKBACK_SESSIONS = 55
YEAR_LOOKBACK_SESSIONS = 252
BOLLINGER_SESSIONS = 20
BOLLINGER_STANDARD_DEVIATIONS = 2.0


def prior_extreme(candles: list[Candle], lookback_sessions: int, *, highest: bool) -> float | None:
    """Highest high (or lowest low) of the `lookback_sessions` sessions BEFORE the latest.

    The latest session is excluded so it can be tested against the level. Unavailable
    (None) without a full window: a 52-week high needs a year of bars before today.
    """
    if len(candles) < lookback_sessions + 1:
        return None
    prior_candles = candles[-lookback_sessions - 1 : -1]
    if highest:
        return max(candle.high for candle in prior_candles)
    return min(candle.low for candle in prior_candles)


def bollinger_bands(closes: list[float]) -> tuple[float | None, float | None]:
    """(upper, lower) band: 20-session average close plus and minus two population
    standard deviations. Unavailable without 20 closes."""
    if len(closes) < BOLLINGER_SESSIONS:
        return None, None
    window = closes[-BOLLINGER_SESSIONS:]
    average = sum(window) / BOLLINGER_SESSIONS
    deviation = (sum((close - average) ** 2 for close in window) / BOLLINGER_SESSIONS) ** 0.5
    band_width = BOLLINGER_STANDARD_DEVIATIONS * deviation
    return average + band_width, average - band_width


def relative_volume(candles: list[Candle], lookback_sessions: int) -> float | None:
    """Latest volume divided by the average of the `lookback_sessions` sessions before it.

    The latest session is excluded from the average so a volume spike is measured
    against normal activity. Unavailable (None) without enough history or when the
    prior sessions traded no volume.
    """
    if len(candles) < lookback_sessions + 1:
        return None
    prior_volumes = [candle.volume for candle in candles[-lookback_sessions - 1 : -1]]
    average_prior_volume = sum(prior_volumes) / lookback_sessions
    return candles[-1].volume / average_prior_volume if average_prior_volume > 0 else None


def day_change_percent(closes: list[float]) -> float | None:
    """Latest close against the previous close, in percent. Unavailable with one session."""
    if len(closes) < 2:
        return None
    return (closes[-1] / closes[-2] - 1) * 100


def calculate(candles: list[Candle]) -> Indicators:
    closes = [c.close for c in candles]
    changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = wilder([max(c, 0) for c in changes], 14)
    loss = wilder([max(-c, 0) for c in changes], 14)
    rsi = None
    if gain is not None and loss is not None:
        rsi = 50.0 if gain == loss == 0 else (100.0 if loss == 0 else 100 - 100 / (1 + gain / loss))
    ranges = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in zip(candles, candles[1:], strict=False)
    ]
    recent_candles = candles[-RANGE_LOOKBACK_SESSIONS:]
    has_full_range = len(candles) >= RANGE_LOOKBACK_SESSIONS
    bollinger_upper, bollinger_lower = bollinger_bands(closes)
    return Indicators(
        candle_count=len(candles),
        close=closes[-1] if closes else None,
        sma20=sum(closes[-20:]) / 20 if len(closes) >= 20 else None,
        sma50=sum(closes[-50:]) / 50 if len(closes) >= 50 else None,
        rsi14=rsi,
        atr14=wilder(ranges, 14),
        return20_pct=(closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None,
        relative_volume20=relative_volume(candles, RANGE_LOOKBACK_SESSIONS),
        high20=max(candle.high for candle in recent_candles) if has_full_range else None,
        low20=min(candle.low for candle in recent_candles) if has_full_range else None,
        latest_high=candles[-1].high if candles else None,
        latest_low=candles[-1].low if candles else None,
        previous_high=candles[-2].high if len(candles) >= 2 else None,
        previous_low=candles[-2].low if len(candles) >= 2 else None,
        previous_close=closes[-2] if len(closes) >= 2 else None,
        day_change_pct=day_change_percent(closes),
        prior_high20=prior_extreme(candles, RANGE_LOOKBACK_SESSIONS, highest=True),
        prior_high55=prior_extreme(candles, BREAKOUT_LOOKBACK_SESSIONS, highest=True),
        prior_high252=prior_extreme(candles, YEAR_LOOKBACK_SESSIONS, highest=True),
        prior_low252=prior_extreme(candles, YEAR_LOOKBACK_SESSIONS, highest=False),
        bollinger_upper20=bollinger_upper,
        bollinger_lower20=bollinger_lower,
    )
