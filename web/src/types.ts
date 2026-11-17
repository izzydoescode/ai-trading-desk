/** Shapes published by the desk server. Field names match its JSON exactly. */

export type SessionStatus = "IDLE" | "RUNNING" | "PAUSED" | "FINISHED" | "FAILED";
export type DecisionAction = "TRADE" | "WAIT" | "REJECT";
export type Classification = "BULLISH" | "BEARISH" | "NEUTRAL" | "INSUFFICIENT_DATA";
export type RiskVerdict = "APPROVE" | "REVISE" | "REJECT";
export type ScoutVerdict = "WATCH" | "SKIP" | "NEED_MORE_DATA";

export interface Candle {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface EquityPoint {
  timestamp: string;
  equity: number;
  has_open_position: boolean;
}

export interface TradePlan {
  symbol: string;
  side: string;
  entry_reference: number;
  stop: number;
  target: number;
  units: number;
  notional: number;
  risk_amount: number;
  reward_to_risk: number;
}

export interface RiskCheck {
  name: string;
  limit: string;
  actual: string;
  passed: boolean;
}

export interface Analysis {
  classification: Classification;
  summary: string;
  risks: string[];
  abstain: boolean;
}

export interface NewsItem {
  published_at: string;
  source: string;
  headline: string;
  url: string | null;
  is_commentary: boolean;
}

export interface DeskReport {
  symbol: string;
  evaluated_as_of: string;
  mode: "deterministic" | "llm";
  indicators: {
    candle_count: number;
    close: number | null;
    sma20: number | null;
    sma50: number | null;
    rsi14: number | null;
    atr14: number | null;
    return20_pct: number | null;
    relative_volume20: number | null;
    high20: number | null;
    low20: number | null;
  };
  scout: { symbol: string; verdict: ScoutVerdict; score: number | null; reasons: string[] };
  chart: {
    thesis: Analysis;
    counter_case: Analysis;
    support: number | null;
    resistance: number | null;
    entry_reference: number | null;
    stop: number | null;
    target: number | null;
    confirmation: string;
    invalidation: string;
  };
  news: {
    verdict: Classification;
    abstain: boolean;
    summary: string;
    verified_facts: NewsItem[];
    commentary: NewsItem[];
    red_flags: string[];
    missing_information: string[];
  };
  sentiment: {
    verdict: Classification;
    abstain: boolean;
    summary: string;
    mood_score: number | null;
    hype_risk: string;
    reasons: string[];
    contrarian_view: string;
  };
  risk: {
    verdict: RiskVerdict;
    checks: RiskCheck[];
    plan: TradePlan | null;
    failed_checks: string[];
    revision: string | null;
  };
  decision: {
    action: DecisionAction;
    summary: string;
    plan: TradePlan | null;
    sources: string[];
    dissent: string[];
    required_confirmation: string | null;
    failed_rule: string | null;
  };
  model_review: { approve: boolean; rationale: string; concerns: string[] } | null;
}

export interface Position {
  symbol: string;
  units: number;
  entry_price: number;
  stop: number;
  target: number;
  opened_at: string;
  planned_risk_amount: number;
}

export interface ClosedTrade {
  symbol: string;
  units: number;
  entry_price: number;
  exit_price: number;
  opened_at: string;
  closed_at: string;
  exit_reason: string;
  total_cost: number;
  profit_and_loss: number;
  r_multiple: number;
}

export interface Account {
  starting_cash: number;
  equity: number;
  cash: number;
  open_idea_count: number;
  open_risk_amount: number;
  realized_loss_today: number;
  positions: Position[];
  pending_symbols: string[];
  closed_trades: ClosedTrade[];
}

export interface DeskEvent {
  run_id: string | null;
  sequence: number | null;
  timestamp: string;
  symbol: string;
  agent: string;
  kind: string;
  reason: string;
  detail: Record<string, string | number | boolean | null>;
}

export interface RiskLimits {
  max_loss_per_idea_fraction: number;
  minimum_reward_to_risk: number;
  maximum_open_ideas: number;
  maximum_daily_loss_fraction: number;
  maximum_position_fraction: number;
  kill_switch_engaged: boolean;
}

export interface StatusFields {
  status: SessionStatus;
  mode: string;
  venue: string;
  kill_switch_engaged: boolean;
  bars_per_second: number;
  bar_index: number | null;
  bar_count: number | null;
  bar_time: string | null;
  error: string | null;
}

export interface Snapshot extends StatusFields {
  type: "snapshot";
  run_id: string | null;
  market: string;
  data_source: string;
  /** Name of the news feed SNOWDEN reads, or null when none is configured. */
  news_source?: string | null;
  settings: {
    symbols: string[];
    start: string;
    end: string;
    bars_per_second: number;
    starting_cash: number;
  } | null;
  limits: RiskLimits;
  setup_rules: { stop_atr_multiple: number; target_reward_multiple: number };
  account: Account | null;
  equity_curve: EquityPoint[];
  candles_by_symbol: Record<string, Candle[]>;
  reports_by_symbol: Record<string, DeskReport>;
  recent_events: DeskEvent[];
}

export interface BarMessage extends StatusFields {
  type: "bar";
  equity_point: EquityPoint;
  candles_by_symbol: Record<string, Candle>;
  reports_by_symbol: Record<string, DeskReport>;
  account: Account | null;
}

export type ServerMessage =
  | Snapshot
  | BarMessage
  | ({ type: "status" } & StatusFields)
  | { type: "event"; event: DeskEvent }
  | { type: "account"; account: Account | null };
