/** Turns desk state into what each panel shows. Pure functions, no formatting of layout. */

import { MISSING, percent, price } from "./format";
import type {
  Account,
  Classification,
  DeskReport,
  EquityPoint,
  ScoutSignal,
  Snapshot,
  TradeSide,
} from "./types";

export type Tone = "up" | "down" | "caution" | "dim" | "amber" | "info";

export const AGENTS = [
  { name: "HAWKEYE", key: "F1", label: "SCOUT", role: "Market scout · ranks watchlist" },
  { name: "NEO", key: "F2", label: "CHART", role: "Chart setup · trend and levels" },
  { name: "SNOWDEN", key: "F3", label: "NEWS", role: "News · facts vs commentary" },
  { name: "FAMA", key: "F4", label: "MOOD", role: "Sentiment · crowd mood, hype" },
  { name: "ODIN", key: "F5", label: "RISK", role: "Risk manager · holds the veto" },
  { name: "PICARD", key: "F6", label: "DECIDE", role: "Head trader · final decision" },
] as const;

export type AgentName = (typeof AGENTS)[number]["name"];

export interface AgentCard {
  name: AgentName;
  key: string;
  label: string;
  role: string;
  status: string;
  tone: Tone;
  line: string;
  metricLabel: string;
  metricValue: string;
  /** What the agent "says" on the floor: a short version of its latest line. */
  speech: string;
}

export const STRATEGY_WORDS = {
  BREAKOUT: "BREAKOUT",
  TREND_CONTINUATION: "TREND",
  BOLLINGER_REVERSION: "BOLLINGER",
} as const;

const TREND_WORDS: Record<Classification, string> = {
  BULLISH: "UP",
  BEARISH: "DOWN",
  NEUTRAL: "FLAT",
  INSUFFICIENT_DATA: "N/A",
};

const ACTION_TONES = { TRADE: "caution", WAIT: "dim", REJECT: "down" } as const;
const VERDICT_TONES = { APPROVE: "up", REVISE: "caution", REJECT: "down" } as const;

/** The symbol the page follows: the user's pick, else an open position, else the latest
 *  approved trade, else the best scout score, else the first configured symbol. */
export function focusSymbol(desk: Snapshot | null, selectedSymbol: string | null): string | null {
  if (selectedSymbol) return selectedSymbol;
  if (desk === null) return null;
  const openPosition = desk.account?.positions[0];
  if (openPosition) return openPosition.symbol;
  const pendingSymbol = desk.account?.pending_symbols[0];
  if (pendingSymbol) return pendingSymbol;
  const reports = Object.values(desk.reports_by_symbol);
  const approved = reports.find((report) => report.decision.action === "TRADE");
  if (approved) return approved.symbol;
  const ranked = watchlistRows(desk.reports_by_symbol);
  return ranked[0]?.symbol ?? desk.settings?.symbols[0] ?? null;
}

export interface WatchlistRow {
  symbol: string;
  score: number | null;
  /** Latest volume as a multiple of its 20-session average. */
  relativeVolume: number | null;
  volumeSurge: boolean;
  signals: SignalTag[];
  /** Side the scout rates the symbol for; null when it has no trend. */
  side: TradeSide | null;
  verdict: string;
  tone: Tone;
}

export interface SignalTag {
  label: string;
  tone: Tone;
  /** Spoken description, including whether volume confirmed the move. */
  description: string;
}

const SIGNAL_LABELS: Record<ScoutSignal["kind"], string> = {
  NEW_52_WEEK_HIGH: "52W HIGH",
  NEW_52_WEEK_LOW: "52W LOW",
  BREAKOUT_ABOVE_PRIOR_DAY_HIGH: "BREAKOUT",
  BREAKDOWN_BELOW_PRIOR_DAY_LOW: "BREAKDOWN",
};

/** A confirmed move is shown in its direction's colour; an unconfirmed one is dimmed
 *  and says "no vol", because a move without volume is a low-probability signal. */
export function signalTag(signal: ScoutSignal): SignalTag {
  const label = SIGNAL_LABELS[signal.kind];
  return {
    label: signal.volume_confirmed ? `${label} ✓` : `${label} · no vol`,
    tone: signal.volume_confirmed ? (signal.is_bullish ? "up" : "down") : "dim",
    description: `${label.toLowerCase()} at ${price(signal.level)}, ${
      signal.volume_confirmed ? "confirmed by volume" : "not confirmed by volume"
    }`,
  };
}

export function watchlistRows(reportsBySymbol: Record<string, DeskReport>): WatchlistRow[] {
  return Object.values(reportsBySymbol)
    .map((report) => ({
      symbol: report.symbol,
      score: report.scout.score,
      relativeVolume: report.indicators.relative_volume20,
      volumeSurge: report.scout.volume_surge ?? false,
      signals: (report.scout.signals ?? []).map(signalTag),
      verdict: report.scout.verdict === "NEED_MORE_DATA" ? "NEED DATA" : report.scout.verdict,
      side: report.scout.side ?? null,
      tone: ({ WATCH: "up", SKIP: "dim", NEED_MORE_DATA: "caution" } as const)[
        report.scout.verdict
      ],
    }))
    .sort((first, second) => (second.score ?? -1) - (first.score ?? -1));
}

function idleCards(status: string, line: string): AgentCard[] {
  return AGENTS.map((agent) => ({
    ...agent,
    status,
    tone: status === "HALTED" ? "down" : "dim",
    line,
    metricLabel: "LAST",
    metricValue: MISSING,
    speech: { OFFLINE: "…", HALTED: "Standing down.", IDLE: "Ready." }[status] ?? "…",
  }));
}

export function agentCards(desk: Snapshot | null, symbol: string | null): AgentCard[] {
  if (desk === null) return idleCards("OFFLINE", "Waiting for the desk server.");
  const report = symbol ? desk.reports_by_symbol[symbol] : undefined;
  if (desk.kill_switch_engaged && report === undefined) {
    return idleCards("HALTED", "Kill switch engaged. No new ideas until it is released.");
  }
  if (report === undefined) return idleCards("IDLE", "Waiting for the first bar.");

  const rows = watchlistRows(desk.reports_by_symbol);
  const count = (verdict: string) => rows.filter((row) => row.verdict === verdict).length;
  const topPick = rows[0];
  const passedChecks = report.risk.checks.filter((check) => check.passed).length;
  const plan = report.risk.plan;
  const equity = desk.account?.equity ?? null;
  const verifiedCount = report.news.verified_facts.length;
  const itemCount = verifiedCount + report.news.commentary.length;

  const setup = report.chart.setup;
  const speeches: Record<AgentName, string> = {
    HAWKEYE: topPick ? `${topPick.symbol} leads the list. Passing it down.` : "Nothing worth a look.",
    NEO: setup
      ? `${setup.side === "SHORT" ? "Short. Invalid above" : "Invalid below"} ${price(setup.stop)}.`
      : "No setup I would trade.",
    SNOWDEN: report.news.abstain
      ? "No dated news supplied. I will not guess."
      : report.news.red_flags.length
        ? "Red flag in the news. Read it first."
        : `${verifiedCount} dated item${verifiedCount === 1 ? "" : "s"} checked.`,
    FAMA: report.sentiment.abstain
      ? "No read on the crowd."
      : `Mood ${report.sentiment.mood_score} of 100. Hype risk ${report.sentiment.hype_risk.toLowerCase()}.`,
    ODIN: {
      APPROVE: plan ? `Within limits. Approved at ${plan.units} shares.` : "Within limits.",
      REVISE: "Reward is too thin. Revise it.",
      REJECT: `Vetoed: ${report.risk.failed_checks[0] ?? "risk limits"}.`,
    }[report.risk.verdict],
    PICARD: {
      TRADE: "Make it so. Paper order queued.",
      WAIT: "We wait for confirmation.",
      REJECT: "No trade. The veto stands.",
    }[report.decision.action],
  };

  const cards: Omit<AgentCard, "key" | "label" | "role" | "speech">[] = [
    {
      name: "HAWKEYE",
      status: "DONE",
      tone: "up",
      line: `Ranked ${rows.length} symbol${rows.length === 1 ? "" : "s"}: ${count("WATCH")} watch, ${count("SKIP")} skip, ${count("NEED DATA")} need data.`,
      metricLabel: "TOP PICK",
      metricValue: topPick ? `${topPick.symbol} ${price(topPick.score)}` : MISSING,
    },
    {
      name: "NEO",
      status: report.chart.thesis.abstain ? "ABSTAIN" : "DONE",
      tone: report.chart.thesis.abstain ? "dim" : "up",
      line: setup ? setup.invalidation : (report.chart.watch_for ?? "No setup."),
      metricLabel: setup ? "SETUP" : "TREND",
      metricValue: setup
        ? STRATEGY_WORDS[setup.strategy]
        : TREND_WORDS[report.chart.thesis.classification],
    },
    {
      name: "SNOWDEN",
      status: report.news.abstain ? "ABSTAIN" : "DONE",
      tone: report.news.abstain ? "dim" : "up",
      line: report.news.summary,
      metricLabel: "DATED ITEMS",
      metricValue: report.news.abstain ? MISSING : `${verifiedCount} of ${itemCount}`,
    },
    {
      name: "FAMA",
      status: report.sentiment.abstain ? "ABSTAIN" : "DONE",
      tone: report.sentiment.abstain ? "dim" : "up",
      line: report.sentiment.summary,
      metricLabel: "MOOD",
      metricValue:
        report.sentiment.mood_score === null ? MISSING : `${report.sentiment.mood_score} / 100`,
    },
    {
      name: "ODIN",
      status: report.risk.verdict,
      tone: VERDICT_TONES[report.risk.verdict],
      line:
        report.risk.failed_checks.length === 0
          ? `${passedChecks} of ${report.risk.checks.length} checks pass.`
          : `Failed: ${report.risk.failed_checks.join(", ")}.`,
      metricLabel: "RISK USED",
      metricValue: plan && equity ? percent(plan.risk_amount / equity) : MISSING,
    },
    {
      name: "PICARD",
      status: report.decision.action,
      tone: ACTION_TONES[report.decision.action],
      line: report.decision.summary,
      metricLabel: "DECISION",
      metricValue: report.decision.action,
    },
  ];
  return cards.map((card) => {
    const agent = AGENTS.find((candidate) => candidate.name === card.name)!;
    return desk.kill_switch_engaged
      ? { ...agent, ...card, status: "HALTED", tone: "down" as const, speech: "Standing down." }
      : { ...agent, ...card, speech: speeches[card.name] };
  });
}

export interface PipelineStage {
  label: string;
  agent: AgentName | "DATA";
  tone: Tone;
}

export function pipelineStages(report: DeskReport | undefined): PipelineStage[] {
  if (report === undefined) return [];
  const dataBlocked = report.risk.failed_checks.includes("Data quality");
  const scoutWord = report.scout.verdict === "NEED_MORE_DATA" ? "NEED DATA" : report.scout.verdict;
  return [
    { label: dataBlocked ? "DATA BLOCKED" : "DATA OK", agent: "DATA", tone: dataBlocked ? "down" : "up" },
    { label: `HAWKEYE ${scoutWord}`, agent: "HAWKEYE", tone: report.scout.verdict === "WATCH" ? "up" : "dim" },
    {
      label: `NEO ${report.chart.setup === null ? "NO SETUP" : "SETUP"}`,
      agent: "NEO",
      tone: report.chart.setup === null ? "dim" : "up",
    },
    {
      label: `SNOWDEN ${report.news.abstain ? "NO DATA" : report.news.red_flags.length ? "RED FLAG" : "CLEAR"}`,
      agent: "SNOWDEN",
      tone: report.news.abstain ? "dim" : report.news.red_flags.length ? "down" : "up",
    },
    {
      label: `FAMA ${report.sentiment.abstain ? "NO DATA" : report.sentiment.verdict}`,
      agent: "FAMA",
      tone: report.sentiment.abstain ? "dim" : "up",
    },
    { label: `ODIN ${report.risk.verdict}`, agent: "ODIN", tone: VERDICT_TONES[report.risk.verdict] },
    {
      label: `PICARD ${report.decision.action}`,
      agent: "PICARD",
      tone: ACTION_TONES[report.decision.action],
    },
  ];
}

export interface Performance {
  tradeCount: number;
  winRate: number | null;
  profitFactor: number | null;
  averageWinR: number | null;
  averageLossR: number | null;
  /** Average winning and losing trade in dollars; a loss is negative. */
  averageWinAmount: number | null;
  averageLossAmount: number | null;
  maximumDrawdown: number | null;
  exposure: number | null;
  totalReturn: number | null;
  profitAndLoss: number | null;
}

export function performance(account: Account | null, equityCurve: EquityPoint[]): Performance {
  const trades = account?.closed_trades ?? [];
  const wins = trades.filter((trade) => trade.profit_and_loss > 0);
  const losses = trades.filter((trade) => trade.profit_and_loss <= 0);
  const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);
  const average = (values: number[]) => (values.length ? sum(values) / values.length : null);
  const grossWin = sum(wins.map((trade) => trade.profit_and_loss));
  const grossLoss = Math.abs(sum(losses.map((trade) => trade.profit_and_loss)));

  let peak = Number.NEGATIVE_INFINITY;
  let maximumDrawdown = 0;
  for (const point of equityCurve) {
    peak = Math.max(peak, point.equity);
    maximumDrawdown = Math.min(maximumDrawdown, point.equity / peak - 1);
  }
  return {
    tradeCount: trades.length,
    winRate: trades.length ? wins.length / trades.length : null,
    // With no losing trade there is nothing to divide by: shown as missing, not infinity.
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : null,
    averageWinR: average(wins.map((trade) => trade.r_multiple)),
    averageLossR: average(losses.map((trade) => trade.r_multiple)),
    averageWinAmount: average(wins.map((trade) => trade.profit_and_loss)),
    averageLossAmount: average(losses.map((trade) => trade.profit_and_loss)),
    maximumDrawdown: equityCurve.length ? maximumDrawdown : null,
    exposure: equityCurve.length
      ? equityCurve.filter((point) => point.has_open_position).length / equityCurve.length
      : null,
    totalReturn: account ? account.equity / account.starting_cash - 1 : null,
    profitAndLoss: account ? account.equity - account.starting_cash : null,
  };
}

export interface PriceLevel {
  label: "ENTRY" | "STOP" | "TARGET";
  price: number;
  tone: Tone;
}

/** Levels to draw: the open position's if there is one, else the latest setup's. */
export function priceLevels(desk: Snapshot | null, symbol: string | null): PriceLevel[] {
  if (desk === null || symbol === null) return [];
  const position = desk.account?.positions.find((candidate) => candidate.symbol === symbol);
  const setup = desk.reports_by_symbol[symbol]?.chart.setup;
  const levels = position
    ? { entry: position.entry_price, stop: position.stop, target: position.target }
    : setup
      ? { entry: setup.entry_reference, stop: setup.stop, target: setup.target }
      : null;
  if (levels === null) return [];
  return [
    { label: "TARGET", price: levels.target, tone: "up" },
    { label: "ENTRY", price: levels.entry, tone: "amber" },
    { label: "STOP", price: levels.stop, tone: "down" },
  ];
}

export interface TapeQuote {
  symbol: string;
  last: number;
  /** Change from the previous bar's close, as a fraction; null on the first bar. */
  change: number | null;
}

/** One quote per symbol with bars, in the configured order. */
export function tapeQuotes(desk: Snapshot | null): TapeQuote[] {
  if (desk === null) return [];
  const order = desk.settings?.symbols ?? Object.keys(desk.candles_by_symbol);
  return order.flatMap((symbol) => {
    const candles = desk.candles_by_symbol[symbol] ?? [];
    const latest = candles.at(-1);
    if (latest === undefined) return [];
    const previous = candles.at(-2);
    return [
      {
        symbol,
        last: latest.close,
        change: previous ? latest.close / previous.close - 1 : null,
      },
    ];
  });
}
