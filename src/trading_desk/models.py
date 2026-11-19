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
    """One daily price bar. Rejects non-finite numbers, non-positive prices, negative volume and a
    high or low that does not bound the open and close.
    """

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
    """A symbol's bars and where they came from. Bars must be unique and strictly chronological;
    provider errors travel with the data instead of being raised.
    """

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
    """Every number the desk is allowed to cite, computed from the bars by code. A value that cannot
    be computed from the available history is None, never zero or an estimate.
    """

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
    # The latest session's own range.
    latest_high: float | None = None
    latest_low: float | None = None
    # Levels from BEFORE the latest session, so the latest bar can break them.
    previous_high: float | None = None
    previous_low: float | None = None
    previous_close: float | None = None
    # Latest close against the previous close, in percent.
    day_change_pct: float | None = None
    prior_high20: float | None = None
    prior_high55: float | None = None
    prior_high252: float | None = None
    prior_low252: float | None = None
    # 20-session simple average of closes plus and minus two standard deviations.
    bollinger_upper20: float | None = None
    bollinger_lower20: float | None = None


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
        "latest_high",
        "latest_low",
        "previous_high",
        "previous_low",
        "previous_close",
        "day_change_pct",
        "prior_high20",
        "prior_high55",
        "prior_high252",
        "prior_low252",
        "bollinger_upper20",
        "bollinger_lower20",
    ]
    value: float


class Analysis(Model):
    """One agent's written view: a classification, a summary, the exact measurements it relies on
    and
    the risks it sees. Abstaining and INSUFFICIENT_DATA always go together, and a view that does
    not abstain must cite evidence.
    """

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
    """The research workflow's output for one symbol: data, indicators, the technical, contrarian
    and
    synthesis views, the final classification, limitations, dissent and the trace.
    """

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


class ScoutSignalKind(StrEnum):
    NEW_52_WEEK_HIGH = "NEW_52_WEEK_HIGH"
    NEW_52_WEEK_LOW = "NEW_52_WEEK_LOW"
    BIG_PERCENT_GAINER = "BIG_PERCENT_GAINER"
    BREAKDOWN_BELOW_PRIOR_DAY_LOW = "BREAKDOWN_BELOW_PRIOR_DAY_LOW"


class ScoutSignal(Model):
    """A price event HAWKEYE spotted, and whether volume backed it.

    A move without a volume surge is recorded but marked unconfirmed: it is a
    low-probability signal and earns the symbol no priority.
    """

    kind: ScoutSignalKind
    # The price the move is measured against: the yearly extreme, the prior day's
    # low, or for a big percent gainer the previous close.
    level: float = Field(gt=0)
    is_bullish: bool
    volume_confirmed: bool
    # The day's gain in percent; only a big percent gainer carries it.
    change_pct: float | None = None

    @model_validator(mode="after")
    def only_a_big_gainer_carries_its_gain(self) -> Self:
        if (self.change_pct is not None) != (self.kind == ScoutSignalKind.BIG_PERCENT_GAINER):
            raise ValueError("change_pct is required for a big percent gainer and no other signal")
        return self


class ScoutReport(Model):
    """HAWKEYE: is this symbol worth the desk's attention right now?"""

    symbol: str
    verdict: ScoutVerdict
    score: float | None = Field(default=None, ge=0, le=1)
    # Latest session's volume is at least 50% above its 20-session average.
    volume_surge: bool = False
    signals: list[ScoutSignal] = Field(default_factory=list)
    reasons: list[str] = Field(min_length=1)
    evidence: list[Evidence]

    @model_validator(mode="after")
    def score_matches_verdict(self) -> Self:
        if (self.score is None) != (self.verdict == ScoutVerdict.NEED_MORE_DATA):
            raise ValueError("a score is required unless more data is needed")
        return self


class TradeSide(StrEnum):
    LONG = "LONG"


class SetupStrategy(StrEnum):
    """The entry patterns NEO knows. Each has its own trigger, stop and target."""

    BREAKOUT = "BREAKOUT"
    TREND_CONTINUATION = "TREND_CONTINUATION"
    BOLLINGER_REVERSION = "BOLLINGER_REVERSION"


class TradeSetup(Model):
    """A complete long idea: the pattern that triggered and all three prices.

    There is no such thing as a setup without an entry, a stop and a target, so
    every field is required. When no pattern triggers, NEO's `ChartReport` simply
    has no setup.
    """

    strategy: SetupStrategy
    entry_reference: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    confirmation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    # True when the exit is "the next touch of the upper Bollinger band": the target then
    # moves with the band each session instead of staying at the price set on entry.
    target_follows_upper_band: bool = False

    @model_validator(mode="after")
    def coherent_levels(self) -> Self:
        if not (self.stop < self.entry_reference < self.target):
            raise ValueError("a long setup needs stop < entry < target")
        return self


class ChartReport(Model):
    """NEO's read of the chart: thesis, counter-case, context levels, and a trade
    setup only when one of its patterns actually triggered.

    A report without a setup is normal and useful: it tells the desk NEO looked,
    which patterns it checked and why none fired, and what it is watching for.
    """

    thesis: Analysis
    counter_case: Analysis
    # The overall trend decides which side NEO may trade: BULLISH allows longs only,
    # BEARISH allows shorts only, anything else allows nothing.
    trend: Classification = Classification.INSUFFICIENT_DATA
    support: float | None = Field(default=None, gt=0)
    resistance: float | None = Field(default=None, gt=0)
    # One line per pattern considered: why it triggered or what was missing.
    strategy_notes: list[str] = Field(default_factory=list)
    setup: TradeSetup | None = None
    # With no setup: what would have to happen for one to appear.
    watch_for: str | None = None

    @model_validator(mode="after")
    def says_what_it_is_waiting_for(self) -> Self:
        if self.setup is None and not self.watch_for:
            raise ValueError("a chart report without a setup must say what it is watching for")
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
    # Headline tone counts behind a BULLISH or BEARISH verdict; None when tone was not read.
    bullish_headline_count: int | None = Field(default=None, ge=0)
    bearish_headline_count: int | None = Field(default=None, ge=0)
    neutral_headline_count: int | None = Field(default=None, ge=0)

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
    # Cut a trade that is not working before it reaches its stop: once it has been held
    # this many full sessions, is closing below its entry, and has never been ahead by
    # `stalled_trade_progress_r` times its risk, it is sold at the next open. None turns
    # the rule off.
    stalled_trade_sessions: int | None = Field(default=5, ge=1)
    stalled_trade_progress_r: float = Field(default=0.5, ge=0)


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
    # The entry pattern that produced the plan, kept so every trade can be reviewed by strategy.
    strategy: SetupStrategy
    entry_reference: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    units: float = Field(gt=0)
    notional: float = Field(gt=0)
    risk_amount: float = Field(gt=0)
    reward_to_risk: float = Field(gt=0)
    target_follows_upper_band: bool = False
    # The early-exit rule this trade is held under, copied from the risk limits.
    stalled_trade_sessions: int | None = Field(default=None, ge=1)
    stalled_trade_progress_r: float = Field(default=0.5, ge=0)

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

    schema_version: Literal["3.0"] = "3.0"
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
    chart: ChartReport
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
    # Sold early because the trade was held for a while, never got going and was losing.
    STALLED = "STALLED"
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
    strategy: SetupStrategy
    target_follows_upper_band: bool = False
    # Kept for the trade record: the target as planned, and how the trade has behaved.
    planned_target: float = Field(gt=0)
    bars_held: int = Field(default=0, ge=0)
    highest_price: float = Field(gt=0)
    lowest_price: float = Field(gt=0)
    stalled_trade_sessions: int | None = Field(default=None, ge=1)
    stalled_trade_progress_r: float = Field(default=0.5, ge=0)
    # Set at a session's close when the early-exit rule fires; acted on at the next open.
    exit_at_next_open: bool = False


class ClosedTrade(Model):
    """A completed round trip, with everything needed to review it later.

    The result is in money and in R (multiples of the planned risk). The plan is
    kept beside the outcome: which strategy it was, where the stop and target were
    set, how much was risked. The excursions say how far the trade went for and
    against the desk while it was open, which is what tells a stop that was too
    tight from an entry that was simply wrong.
    """

    symbol: str
    strategy: SetupStrategy
    units: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    planned_stop: float = Field(gt=0)
    planned_target: float = Field(gt=0)
    planned_risk_amount: float = Field(gt=0)
    opened_at: AwareDatetime
    closed_at: AwareDatetime
    bars_held: int = Field(ge=0)
    exit_reason: ExitReason
    total_cost: float = Field(ge=0)
    profit_and_loss: float
    r_multiple: float
    # Best and worst open result while held, in R. Favourable is never below zero and
    # adverse never above it.
    maximum_favorable_excursion_r: float = Field(ge=0)
    maximum_adverse_excursion_r: float = Field(le=0)


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
