"""US stocks and ETFs on daily bars: the first market the desk trades."""

from datetime import timedelta

from trading_desk.markets.profile import AssetClass, MarketProfile

US_EQUITIES = MarketProfile(
    name="US equities (daily)",
    asset_class=AssetClass.EQUITY,
    # SMA50 is the longest indicator, so fewer than 50 bars cannot support a trend read.
    minimum_history_candles=50,
    # Five calendar days tolerates weekends and most holidays without an exchange calendar.
    maximum_data_age=timedelta(days=5),
    maximum_candle_gap=timedelta(days=5),
    trades_around_the_clock=False,
    allows_fractional_units=False,
    commission_rate=0.0,
    slippage_rate=0.0005,
    benchmark_symbol="SPY",
    default_watchlist=(
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "AMD",
        "SPY",
        "QQQ",
    ),
)
