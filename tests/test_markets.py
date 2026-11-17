from datetime import timedelta

import pytest
from pydantic import ValidationError

from trading_desk.markets import (
    MARKET_PROFILES,
    US_EQUITIES,
    AssetClass,
    MarketProfile,
    get_market_profile,
)


def test_equities_profile_matches_documented_quality_gates():
    assert US_EQUITIES.asset_class is AssetClass.EQUITY
    assert US_EQUITIES.minimum_history_candles == 50
    assert US_EQUITIES.maximum_data_age == timedelta(days=5)
    assert US_EQUITIES.maximum_candle_gap == timedelta(days=5)
    assert US_EQUITIES.benchmark_symbol in US_EQUITIES.default_watchlist
    assert not US_EQUITIES.allows_fractional_units
    assert not US_EQUITIES.trades_around_the_clock


def test_round_trip_cost_counts_both_sides_of_the_trade():
    profile = US_EQUITIES.model_copy(update={"commission_rate": 0.001, "slippage_rate": 0.0005})
    assert profile.round_trip_cost_rate == pytest.approx(0.003)


@pytest.mark.parametrize("lookup_key", [AssetClass.EQUITY, "equity"])
def test_registry_resolves_enum_and_string_keys(lookup_key):
    assert get_market_profile(lookup_key) is US_EQUITIES


@pytest.mark.parametrize("unregistered_key", [AssetClass.CRYPTO, "solana_token", "bonds"])
def test_registry_names_available_markets_when_profile_is_missing(unregistered_key):
    with pytest.raises(ValueError, match="available: equity"):
        get_market_profile(unregistered_key)


def test_profiles_are_immutable():
    with pytest.raises(ValidationError):
        US_EQUITIES.minimum_history_candles = 10


@pytest.mark.parametrize(
    "invalid_override",
    [
        {"minimum_history_candles": 0},
        {"commission_rate": -0.01},
        {"slippage_rate": 1.0},
        {"default_watchlist": ()},
        {"benchmark_symbol": ""},
    ],
)
def test_profile_rejects_invalid_values(invalid_override):
    with pytest.raises(ValidationError):
        MarketProfile(**{**US_EQUITIES.model_dump(), **invalid_override})


def test_every_registered_profile_is_keyed_by_its_own_asset_class():
    for asset_class, profile in MARKET_PROFILES.items():
        assert profile.asset_class is asset_class
