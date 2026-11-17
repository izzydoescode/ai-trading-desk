"""Registry of supported markets. Add a profile here to open a new asset class."""

from trading_desk.markets.equities import US_EQUITIES
from trading_desk.markets.profile import AssetClass, MarketProfile

MARKET_PROFILES: dict[AssetClass, MarketProfile] = {AssetClass.EQUITY: US_EQUITIES}


def get_market_profile(asset_class: AssetClass | str) -> MarketProfile:
    """Return the registered profile, or raise naming the markets that are available."""
    try:
        return MARKET_PROFILES[AssetClass(asset_class)]
    except (KeyError, ValueError) as error:
        available = ", ".join(sorted(profile.value for profile in MARKET_PROFILES))
        raise ValueError(
            f"No market profile registered for {asset_class!r}; available: {available}"
        ) from error


__all__ = ["MARKET_PROFILES", "US_EQUITIES", "AssetClass", "MarketProfile", "get_market_profile"]
