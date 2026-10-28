"""Domain contracts and output invariants, independent of orchestration/provider code."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Classification(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Candle(Model):
    timestamp: AwareDatetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def coherent_prices(self) -> Self:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC bounds are inconsistent")
        if self.low > self.high:
            raise ValueError("low exceeds high")
        return self


class MarketData(Model):
    symbol: str
    source: str
    interval: Literal["1d"] = "1d"
    synthetic: bool = False
    retrieved_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    candles: list[Candle] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def chronological(self) -> Self:
        times = [c.timestamp for c in self.candles]
        if times != sorted(set(times)):
            raise ValueError("candles must be unique and strictly chronological")
        return self


class Indicators(Model):
    candle_count: int
    close: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    return20_pct: float | None = None


class Evidence(Model):
    """An exact measurement reference. Models may select facts, never supply new prices."""

    metric: Literal["close", "sma20", "sma50", "rsi14", "atr14", "return20_pct"]
    value: float


class Analysis(Model):
    classification: Classification
    summary: str = Field(min_length=1, max_length=3000)
    evidence: list[Evidence]
    risks: list[str]
    abstain: bool

    @model_validator(mode="after")
    def abstention_consistency(self) -> Self:
        if self.abstain != (self.classification == Classification.INSUFFICIENT_DATA):
            raise ValueError("abstain must match INSUFFICIENT_DATA")
        if not self.abstain and not self.evidence:
            raise ValueError("non-abstaining analysis requires measured evidence")
        return self


class Trace(Model):
    run_id: str
    agent: str
    status: Literal["ok", "degraded", "skipped"]
    duration_ms: float
    provider: str
    error: str | None = None


class Report(Model):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    symbol: str
    analysis_timestamp: AwareDatetime
    evaluated_as_of: AwareDatetime
    mode: Literal["deterministic", "llm"]
    data: MarketData
    market_data_timestamp: AwareDatetime | None
    indicators: Indicators
    technical: Analysis
    contrarian: Analysis
    synthesis: Analysis
    classification: Classification
    abstained: bool
    limitations: list[str]
    dissent: list[str]
    trace: list[Trace]
    purpose: str = "Research only; no orders, position sizing, or investment guarantees."

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if self.abstained != (self.classification == Classification.INSUFFICIENT_DATA):
            raise ValueError("report abstention inconsistent")
        if self.classification != self.synthesis.classification:
            raise ValueError("report must preserve synthesis classification")
        return self
