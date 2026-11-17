import {
  agentCards,
  focusSymbol,
  performance,
  pipelineStages,
  priceLevels,
  watchlistRows,
} from "./derive";
import { cloneSnapshot, reportWith, tradeReport } from "./test/fixtures";
import type { Account, ClosedTrade, Position } from "./types";

const position: Position = {
  symbol: "MSFT",
  units: 10,
  entry_price: 400,
  stop: 380,
  target: 460,
  opened_at: "2025-03-14T21:00:00Z",
  planned_risk_amount: 200,
};

const trade = (profitAndLoss: number, rMultiple: number): ClosedTrade => ({
  symbol: "AAPL",
  units: 10,
  entry_price: 100,
  exit_price: 100 + profitAndLoss / 10,
  opened_at: "2025-03-14T21:00:00Z",
  closed_at: "2025-03-20T21:00:00Z",
  exit_reason: profitAndLoss > 0 ? "TARGET" : "STOP",
  total_cost: 1,
  profit_and_loss: profitAndLoss,
  r_multiple: rMultiple,
});

const account = (overrides: Partial<Account> = {}): Account => ({
  ...cloneSnapshot().account!,
  ...overrides,
});

describe("focus symbol", () => {
  it("follows the user's pick first", () => {
    expect(focusSymbol(cloneSnapshot(), "MSFT")).toBe("MSFT");
    expect(focusSymbol(null, "MSFT")).toBe("MSFT");
  });

  it("has nothing to follow without a desk", () => {
    expect(focusSymbol(null, null)).toBeNull();
  });

  it("prefers an open position, then a queued order, then an approved trade", () => {
    const desk = cloneSnapshot();
    desk.reports_by_symbol = { AAPL: tradeReport() };
    expect(focusSymbol(desk, null)).toBe("AAPL");
    desk.account = account({ pending_symbols: ["NVDA"] });
    expect(focusSymbol(desk, null)).toBe("NVDA");
    desk.account = account({ positions: [position], pending_symbols: ["NVDA"] });
    expect(focusSymbol(desk, null)).toBe("MSFT");
  });

  it("falls back to the best scout score, then the first configured symbol", () => {
    const desk = cloneSnapshot();
    desk.reports_by_symbol = {
      AAPL: reportWith((report) => {
        report.decision.action = "WAIT";
        report.scout.score = 0.4;
      }),
      MSFT: reportWith((report) => {
        report.symbol = "MSFT";
        report.decision.action = "WAIT";
        report.scout.score = 0.9;
      }),
    };
    expect(focusSymbol(desk, null)).toBe("MSFT");
    desk.reports_by_symbol = {};
    expect(focusSymbol(desk, null)).toBe("AAPL");
    desk.settings = null;
    expect(focusSymbol(desk, null)).toBeNull();
  });
});

describe("watchlist", () => {
  it("ranks by score with unscored symbols last and words for every verdict", () => {
    const rows = watchlistRows({
      A: reportWith((report) => {
        report.symbol = "A";
        report.scout = { ...report.scout, verdict: "SKIP", score: 0.3 };
      }),
      B: reportWith((report) => {
        report.symbol = "B";
        report.scout = { ...report.scout, verdict: "NEED_MORE_DATA", score: null };
      }),
      C: reportWith((report) => {
        report.symbol = "C";
      }),
    });
    expect(rows.map((row) => [row.symbol, row.verdict, row.tone])).toEqual([
      ["C", "WATCH", "up"],
      ["A", "SKIP", "dim"],
      ["B", "NEED DATA", "caution"],
    ]);
  });
});

describe("agent cards", () => {
  it("shows every agent offline without a desk and idle before the first bar", () => {
    expect(agentCards(null, null).map((card) => card.status)).toEqual(Array(6).fill("OFFLINE"));
    const idle = agentCards(cloneSnapshot({ reports_by_symbol: {} }), "AAPL");
    expect(idle.map((card) => card.name)).toEqual([
      "HAWKEYE",
      "NEO",
      "SNOWDEN",
      "FAMA",
      "ODIN",
      "PICARD",
    ]);
    expect(idle.every((card) => card.status === "IDLE" && card.metricValue === "–")).toBe(true);
  });

  it("describes an approved trade from the real report", () => {
    const desk = cloneSnapshot();
    desk.reports_by_symbol = { AAPL: tradeReport() };
    const cards = Object.fromEntries(agentCards(desk, "AAPL").map((card) => [card.name, card]));
    expect(cards.HAWKEYE).toMatchObject({ status: "DONE", metricValue: "AAPL 0.82" });
    expect(cards.HAWKEYE!.line).toBe("Ranked 1 symbol: 1 watch, 0 skip, 0 need data.");
    expect(cards.NEO).toMatchObject({ status: "DONE", metricValue: "UP" });
    expect(cards.NEO!.line).toContain("invalidates");
    expect(cards.SNOWDEN).toMatchObject({ status: "ABSTAIN", tone: "dim", metricValue: "–" });
    expect(cards.FAMA).toMatchObject({ status: "ABSTAIN", metricValue: "–" });
    expect(cards.ODIN).toMatchObject({ status: "APPROVE", tone: "up", line: "9 of 9 checks pass." });
    expect(cards.ODIN!.metricValue).toMatch(/^0\.\d\d%$/);
    expect(cards.PICARD).toMatchObject({ status: "TRADE", metricValue: "TRADE" });
  });

  it("describes a rejection, supplied news and a mood reading", () => {
    const desk = cloneSnapshot({ account: null });
    desk.reports_by_symbol = {
      AAPL: reportWith((report) => {
        report.chart.thesis = { ...report.chart.thesis, classification: "INSUFFICIENT_DATA", abstain: true };
        report.chart.stop = null;
        report.news = {
          ...report.news,
          abstain: false,
          summary: "Two dated items.",
          verified_facts: [
            { published_at: "2025-03-12T12:00:00Z", source: "Wire", headline: "A", url: null, is_commentary: false },
          ],
          commentary: [
            { published_at: "2025-03-11T12:00:00Z", source: "Blog", headline: "B", url: null, is_commentary: true },
          ],
        };
        report.sentiment = { ...report.sentiment, abstain: false, mood_score: 58, summary: "Neutral." };
        report.risk = { ...report.risk, verdict: "REJECT", failed_checks: ["Open ideas", "Position size"], plan: null };
        report.decision = { ...report.decision, action: "REJECT", plan: null };
      }),
    };
    const cards = Object.fromEntries(agentCards(desk, "AAPL").map((card) => [card.name, card]));
    expect(cards.NEO).toMatchObject({ status: "ABSTAIN", metricValue: "N/A" });
    expect(cards.NEO!.line).toContain("confirmed");
    expect(cards.SNOWDEN).toMatchObject({ status: "DONE", metricValue: "1 of 2" });
    expect(cards.FAMA).toMatchObject({ status: "DONE", metricValue: "58 / 100" });
    expect(cards.ODIN).toMatchObject({
      status: "REJECT",
      tone: "down",
      line: "Failed: Open ideas, Position size.",
      metricValue: "–",
    });
    expect(cards.PICARD).toMatchObject({ status: "REJECT", tone: "down" });
  });

  it("marks every agent halted while the kill switch is engaged", () => {
    const halted = cloneSnapshot({ kill_switch_engaged: true });
    expect(agentCards(halted, "AAPL").every((card) => card.status === "HALTED")).toBe(true);
    const haltedBeforeAnyBar = cloneSnapshot({ kill_switch_engaged: true, reports_by_symbol: {} });
    const cards = agentCards(haltedBeforeAnyBar, "AAPL");
    expect(cards.every((card) => card.status === "HALTED" && card.tone === "down")).toBe(true);
    expect(cards[0]!.line).toContain("Kill switch engaged");
  });
});

describe("pipeline", () => {
  it("is empty before a decision", () => {
    expect(pipelineStages(undefined)).toEqual([]);
  });

  it("labels each stage of an approved trade", () => {
    expect(pipelineStages(tradeReport()).map((stage) => stage.label)).toEqual([
      "DATA OK",
      "HAWKEYE WATCH",
      "NEO SETUP",
      "SNOWDEN NO DATA",
      "FAMA NO DATA",
      "ODIN APPROVE",
      "PICARD TRADE",
    ]);
  });

  it("labels blocked data, missing setups, red flags and sentiment", () => {
    const stages = pipelineStages(
      reportWith((report) => {
        report.scout.verdict = "NEED_MORE_DATA";
        report.chart.stop = null;
        report.news = { ...report.news, abstain: false, red_flags: ["Guidance cut"] };
        report.sentiment = { ...report.sentiment, abstain: false, verdict: "NEUTRAL" };
        report.risk = { ...report.risk, verdict: "REJECT", failed_checks: ["Data quality"] };
        report.decision.action = "REJECT";
      }),
    );
    expect(stages.map((stage) => [stage.label, stage.tone])).toEqual([
      ["DATA BLOCKED", "down"],
      ["HAWKEYE NEED DATA", "dim"],
      ["NEO NO SETUP", "dim"],
      ["SNOWDEN RED FLAG", "down"],
      ["FAMA NEUTRAL", "up"],
      ["ODIN REJECT", "down"],
      ["PICARD REJECT", "down"],
    ]);
    const clear = pipelineStages(
      reportWith((report) => {
        report.news = { ...report.news, abstain: false, red_flags: [] };
      }),
    );
    expect(clear[3]).toMatchObject({ label: "SNOWDEN CLEAR", tone: "up" });
  });
});

describe("performance", () => {
  it("reports nothing rather than zero when there is no data", () => {
    expect(performance(null, [])).toEqual({
      tradeCount: 0,
      winRate: null,
      profitFactor: null,
      averageWinR: null,
      averageLossR: null,
      maximumDrawdown: null,
      exposure: null,
      totalReturn: null,
      profitAndLoss: null,
    });
  });

  it("computes win rate, profit factor, R averages, drawdown and exposure", () => {
    const result = performance(
      account({
        starting_cash: 100_000,
        equity: 101_000,
        closed_trades: [trade(900, 3), trade(600, 2), trade(-250, -1), trade(-250, -1)],
      }),
      [
        { timestamp: "t1", equity: 100_000, has_open_position: false },
        { timestamp: "t2", equity: 102_000, has_open_position: true },
        { timestamp: "t3", equity: 99_960, has_open_position: true },
        { timestamp: "t4", equity: 101_000, has_open_position: false },
      ],
    );
    expect(result.tradeCount).toBe(4);
    expect(result.winRate).toBe(0.5);
    expect(result.profitFactor).toBe(3);
    expect(result.averageWinR).toBe(2.5);
    expect(result.averageLossR).toBe(-1);
    expect(result.maximumDrawdown).toBeCloseTo(-0.02);
    expect(result.exposure).toBe(0.5);
    expect(result.totalReturn).toBeCloseTo(0.01);
    expect(result.profitAndLoss).toBe(1000);
  });

  it("leaves profit factor missing when no trade has lost", () => {
    const result = performance(account({ closed_trades: [trade(500, 2)] }), []);
    expect(result.profitFactor).toBeNull();
    expect(result.averageLossR).toBeNull();
    expect(result.winRate).toBe(1);
  });
});

describe("price levels", () => {
  it("has none without a desk, a symbol or a setup", () => {
    expect(priceLevels(null, "AAPL")).toEqual([]);
    expect(priceLevels(cloneSnapshot(), null)).toEqual([]);
    const desk = cloneSnapshot();
    desk.reports_by_symbol = { AAPL: reportWith((report) => (report.chart.stop = null)) };
    expect(priceLevels(desk, "AAPL")).toEqual([]);
    expect(priceLevels(desk, "MSFT")).toEqual([]);
  });

  it("uses the latest setup, or the open position when there is one", () => {
    const desk = cloneSnapshot();
    desk.reports_by_symbol = { AAPL: tradeReport() };
    expect(priceLevels(desk, "AAPL").map((level) => [level.label, level.price])).toEqual([
      ["TARGET", 129.5238],
      ["ENTRY", 116.6436],
      ["STOP", 112.3502],
    ]);
    desk.account = account({ positions: [{ ...position, symbol: "AAPL" }] });
    expect(priceLevels(desk, "AAPL").map((level) => level.price)).toEqual([460, 400, 380]);
  });
});
