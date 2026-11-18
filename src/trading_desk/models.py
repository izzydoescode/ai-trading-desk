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
    relative_volume20: float | None = None
    high20: float | None = None
    low20: float | None = None


class Evidence(Model):
    """An exact measurement reference. Models may select facts, never supply new prices."""

    metric: Literal[
        "close",
        "sma20",
        "sma50",
        "rsi14",
        "atr14",
        "return20_pct",
        "relative_volume20",
        "high20",
        "low20",
    ]
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


# ---------------------------------------------------------------------------
# Six-agent desk contracts (schema 2.0). The research `Report` above is unchanged.
# ---------------------------------------------------------------------------


class ScoutVerdict(StrEnum):
    WATCH = "WATCH"
    SKIP = "SKIP"
    NEED_MORE_DATA = "NEED_MORE_DATA"


class ScoutReport(Model):
    """HAWKEYE: is this symbol worth the desk's attention right now?"""

    symbol: str
    verdict: ScoutVerdict
    score: float | None = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(min_length=1)
    evidence: list[Evidence]

    @model_validator(mode="after")
    def score_matches_verdict(self) -> Self:
        if (self.score is None) != (self.verdict == ScoutVerdict.NEED_MORE_DATA):
            raise ValueError("a score is required unless more data is needed")
        return self


class TradeSide(StrEnum):
    LONG = "LONG"


class ChartSetup(Model):
    """NEO: the thesis, its counter-case, and the price levels that define the idea."""

    thesis: Analysis
    counter_case: Analysis
    support: float | None = Field(default=None, gt=0)
    resistance: float | None = Field(default=None, gt=0)
    entry_reference: float | None = Field(default=None, gt=0)
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    confirmation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)

    @property
    def has_trade_levels(self) -> bool:
        return self.entry_reference is not None

    @model_validator(mode="after")
    def coherent_levels(self) -> Self:
        levels = (self.entry_reference, self.stop, self.target)
        if any(level is None for level in levels) and not all(level is None for level in levels):
            raise ValueError("entry, stop and target must be supplied together")
        if self.has_trade_levels and not (self.stop < self.entry_reference < self.target):
            raise ValueError("a long setup needs stop < entry < target")
        return self


class NewsItem(Model):
    published_at: AwareDatetime
    source: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    url: str | None = None
    is_commentary: bool = False


class NewsReport(Model):
    """SNOWDEN: dated, sourced catalysts only. Never an invented headline."""

    verdict: Classification
    abstain: bool
    summary: str = Field(min_length=1)
    verified_facts: list[NewsItem] = Field(default_factory=list)
    commentary: list[NewsItem] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def abstention_consistency(self) -> Self:
        if self.abstain != (self.verdict == Classification.INSUFFICIENT_DATA):
            raise ValueError("abstain must match INSUFFICIENT_DATA")
        return self


class HypeRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SentimentReport(Model):
    """FAMA: crowd mood and hype risk, separated from evidence."""

    verdict: Classification
    abstain: bool
    summary: str = Field(min_length=1)
    mood_score: int | None = Field(default=None, ge=0, le=100)
    hype_risk: HypeRisk = HypeRisk.UNKNOWN
    reasons: list[str] = Field(default_factory=list)
    contrarian_view: str = ""

    @model_validator(mode="after")
    def abstention_consistency(self) -> Self:
        if self.abstain != (self.verdict == Classification.INSUFFICIENT_DATA):
            raise ValueError("abstain must match INSUFFICIENT_DATA")
        if self.abstain and self.mood_score is not None:
            raise ValueError("an abstaining sentiment report cannot carry a mood score")
        return self


class RiskLimits(Model):
    """Hard limits ODIN enforces.

    Defaults risk 1% of equity per idea and require the trade to still pay at
    least 2.75 times its risk once slippage and commission are counted, which is
    the floor under NEO's 3-to-1 target.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    max_loss_per_idea_fraction: float = Field(default=0.01, gt=0, le=0.05)
    minimum_reward_to_risk: float = Field(default=2.75, gt=0)
    maximum_open_ideas: int = Field(default=1, ge=1)
    maximum_daily_loss_fraction: float = Field(default=0.01, gt=0, le=0.2)
    maximum_position_fraction: float = Field(default=0.2, gt=0, le=1)
    kill_switch_engaged: bool = False


class AccountSnapshot(Model):
    """What ODIN needs to know about the paper account at decision time."""

    equity: float = Field(gt=0)
    cash: float = Field(ge=0)
    open_idea_count: int = Field(default=0, ge=0)
    open_risk_amount: float = Field(default=0, ge=0)
    realized_loss_today: float = Field(default=0, ge=0)


class RiskCheck(Model):
    name: str
    limit: str
    actual: str
    passed: bool


class RiskVerdict(StrEnum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    REJECT = "REJECT"


class TradePlan(Model):
    """A sized proposal. Code computes every number; models never edit one."""

    symbol: str
    side: TradeSide
    entry_reference: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    units: float = Field(gt=0)
    notional: float = Field(gt=0)
    risk_amount: float = Field(gt=0)
    reward_to_risk: float = Field(gt=0)

    @model_validator(mode="after")
    def coherent_levels(self) -> Self:
        if not (self.stop < self.entry_reference < self.target):
            raise ValueError("a long plan needs stop < entry < target")
        return self


class RiskReport(Model):
    """ODIN: PASS or FAIL for every check, and a plan only when all of them pass."""

    verdict: RiskVerdict
    checks: list[RiskCheck] = Field(min_length=1)
    plan: TradePlan | None = None
    failed_checks: list[str] = Field(default_factory=list)
    revision: str | None = None

    @model_validator(mode="after")
    def approval_requires_every_check(self) -> Self:
        every_check_passed = all(check.passed for check in self.checks)
        if self.verdict == RiskVerdict.APPROVE:
            if not every_check_passed or self.plan is None or self.failed_checks:
                raise ValueError("approval requires every check to pass and a sized plan")
        elif self.plan is not None or not self.failed_checks or every_check_passed:
            raise ValueError("a non-approval names its failed checks and carries no plan")
        return self


class DecisionAction(StrEnum):
    TRADE = "TRADE"
    WAIT = "WAIT"
    REJECT = "REJECT"


class Decision(Model):
    """PICARD: exactly one outcome, with the reason a reader needs to act on it."""

    action: DecisionAction
    summary: str = Field(min_length=1)
    plan: TradePlan | None = None
    sources: list[str] = Field(default_factory=list)
    dissent: list[str] = Field(default_factory=list)
    required_confirmation: str | None = None
    failed_rule: str | None = None

    @model_validator(mode="after")
    def outcome_is_explained(self) -> Self:
        if (self.action == DecisionAction.TRADE) != (self.plan is not None):
            raise ValueError("a plan is present exactly when the decision is TRADE")
        if self.action == DecisionAction.WAIT and not self.required_confirmation:
            raise ValueError("WAIT must state the confirmation it is waiting for")
        if self.action == DecisionAction.REJECT and not self.failed_rule:
            raise ValueError("REJECT must name the failed rule")
        return self


class GateVerdict(Model):
    """A model's approve-or-veto on a trade the deterministic desk already approved."""

    approve: bool
    rationale: str = Field(min_length=1, max_length=600)
    concerns: list[str] = Field(default_factory=list)


class HeadlineToneLabel(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class NumberedHeadlineLabel(Model):
    number: int = Field(ge=1)
    tone: HeadlineToneLabel


class HeadlineTone(Model):
    """A model's tone label for each supplied headline, keyed by the headline's number.

    Numbers, not list position, tie a label to its headline: a model that drops or
    repeats an entry is then detected instead of silently shifting every label.
    """

    labels: list[NumberedHeadlineLabel]


class DeskReport(Model):
    """One complete six-agent decision for one symbol at one evaluation time."""

    schema_version: Literal["2.0"] = "2.0"
    run_id: str
    symbol: str
    market: str
    analysis_timestamp: AwareDatetime
    evaluated_as_of: AwareDatetime
    mode: Literal["deterministic", "llm"]
    data: MarketData
    market_data_timestamp: AwareDatetime | None
    indicators: Indicators
    account: AccountSnapshot
    limits: RiskLimits
    scout: ScoutReport
    chart: ChartSetup
    news: NewsReport
    sentiment: SentimentReport
    risk: RiskReport
    decision: Decision
    model_review: GateVerdict | None = None
    limitations: list[str]
    trace: list[Trace]
    purpose: str = "Paper trading research only; no real orders and no investment advice."

    @model_validator(mode="after")
    def risk_manager_has_final_authority(self) -> Self:
        if self.decision.action == DecisionAction.TRADE:
            if self.risk.verdict != RiskVerdict.APPROVE:
                raise ValueError("a TRADE decision requires risk approval")
            if self.decision.plan != self.risk.plan:
                raise ValueError("the decision must carry the risk-approved plan unchanged")
        return self


# ---------------------------------------------------------------------------
# Paper venue contracts.
# ---------------------------------------------------------------------------


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    END_OF_RUN = "END_OF_RUN"
    KILL_SWITCH = "KILL_SWITCH"


class Position(Model):
    """An open paper position. The stop and target come from the approved plan."""

    symbol: str
    units: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    opened_at: AwareDatetime
    entry_commission: float = Field(ge=0)
    entry_slippage_cost: float = Field(ge=0)
    planned_risk_amount: float = Field(gt=0)


class ClosedTrade(Model):
    """A completed round trip with its result in money and in R (multiples of planned risk)."""

    symbol: str
    units: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    opened_at: AwareDatetime
    closed_at: AwareDatetime
    exit_reason: ExitReason
    total_cost: float = Field(ge=0)
    profit_and_loss: float
    r_multiple: float


class VenueEventKind(StrEnum):
    SUBMIT = "SUBMIT"
    FILL = "FILL"
    CANCEL = "CANCEL"
    EXIT = "EXIT"
    HALT = "HALT"


class VenueEvent(Model):
    """Something the venue did, always with the reason it did it."""

    kind: VenueEventKind
    timestamp: AwareDatetime
    symbol: str
    reason: str = Field(min_length=1)
    price: float | None = None
    units: float | None = None
    profit_and_loss: float | None = None


class DeskEvent(Model):
    """One line of the desk's journal: who did what, to which symbol, and why.

    Skips and rejections are recorded too, so the journal shows where candidates
    are lost as well as which trades were taken.
    """

    run_id: str
    sequence: int = Field(ge=0)
    timestamp: AwareDatetime
    symbol: str
    agent: str
    kind: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    detail: dict[str, str | float | int | bool | None] = Field(default_factory=dict)
