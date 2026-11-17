"""Market profiles: everything that differs between asset classes lives here.

The decision pipeline, risk rules and paper venue read a `MarketProfile` instead of
hard-coding equity assumptions, so crypto and Solana-token desks can be added by
registering another profile rather than editing the engine.
"""

from datetime import timedelta
from enum import StrEnum

from pydantic import ConfigDict, Field

from trading_desk.models import Model


class AssetClass(StrEnum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    SOLANA_TOKEN = "solana_token"  # noqa: S105 - an asset-class label, not a credential


class MarketProfile(Model):
    """Immutable description of how one market trades and what data it needs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    name: str = Field(min_length=1)
    asset_class: AssetClass
    quote_currency: str = "USD"
    # Data-quality gates.
    minimum_history_candles: int = Field(ge=1)
    maximum_data_age: timedelta
    maximum_candle_gap: timedelta
    # Trading conventions.
    trades_around_the_clock: bool
    allows_fractional_units: bool
    # Sessions per year, used to annualise daily statistics.
    trading_periods_per_year: int = Field(default=252, ge=1)
    # Costs, as fractions of traded notional (0.0005 = 5 basis points).
    commission_rate: float = Field(ge=0, lt=1)
    slippage_rate: float = Field(ge=0, lt=1)
    # Research defaults.
    benchmark_symbol: str = Field(min_length=1)
    default_watchlist: tuple[str, ...] = Field(min_length=1)

    @property
    def round_trip_cost_rate(self) -> float:
        """Fraction of notional lost to commission and slippage entering and exiting."""
        return 2 * (self.commission_rate + self.slippage_rate)
