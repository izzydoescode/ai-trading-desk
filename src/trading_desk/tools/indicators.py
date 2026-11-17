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
    )
