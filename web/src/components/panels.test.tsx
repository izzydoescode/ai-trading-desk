import { render, screen, within } from "@testing-library/react";

import { agentCards, performance } from "../derive";
import { cloneSnapshot, reportWith } from "../test/fixtures";
import { Balance } from "./Balance";
import { ChartPanel } from "./ChartPanel";
import { DecisionPanel } from "./DecisionPanel";
import { Journal, journalLines, NewsMood, Watchlist } from "./Feeds";
import { Floor } from "./Floor";
import { Header } from "./Header";
import { RiskPanel } from "./RiskPanel";
import { Tape } from "./Tape";

const noControls = {
  start: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
  stop: vi.fn(),
  setSpeed: vi.fn(),
  setKillSwitch: vi.fn(),
  goLive: vi.fn(),
  select: vi.fn(),
};

describe("decision panel", () => {
  it("shows a rejection with the failed rule and no ticket", () => {
    render(
      <DecisionPanel
        report={reportWith((report) => {
          report.decision = {
            ...report.decision,
            action: "REJECT",
            plan: null,
            failed_rule: "Open ideas",
            dissent: [],
            summary: "Rejected by risk.",
          };
        })}
      />,
    );
    expect(screen.getByText("REJECT")).toBeInTheDocument();
    expect(screen.getByText("Failed rule: Open ideas")).toBeInTheDocument();
    expect(screen.queryByText("NOTIONAL")).not.toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("shows a model veto and a model approval differently", () => {
    const { rerender } = render(
      <DecisionPanel
        report={reportWith((report) => {
          report.model_review = { approve: false, rationale: "Evidence is thin.", concerns: [] };
        })}
      />,
    );
    expect(screen.getByText("Model review vetoed: Evidence is thin.")).toHaveClass("tone-down");
    rerender(
      <DecisionPanel
        report={reportWith((report) => {
          report.model_review = { approve: true, rationale: "Reports agree.", concerns: [] };
        })}
      />,
    );
    expect(screen.getByText("Model review approved: Reports agree.")).toHaveClass("label");
  });
});

describe("risk panel", () => {
  it("marks the failed check and shows the requested revision", () => {
    render(
      <RiskPanel
        report={reportWith((report) => {
          report.risk.verdict = "REVISE";
          report.risk.checks[5] = { ...report.risk.checks[5]!, passed: false, actual: "2.41" };
          report.risk.revision = "Move the target so reward is at least 2.75 times the risk.";
        })}
      />,
    );
    expect(screen.getByText("REVISE 8/9")).toBeInTheDocument();
    const failedRow = screen.getByText("✕ FAIL").closest("tr")!;
    expect(failedRow).toHaveClass("failed");
    expect(within(failedRow).getByText("2.41")).toBeInTheDocument();
    expect(screen.getByText(/Move the target/)).toBeInTheDocument();
  });
});

describe("news and mood", () => {
  it("says there is no report yet", () => {
    render(<NewsMood report={undefined} />);
    expect(screen.getByText("No news report yet.")).toBeInTheDocument();
    expect(screen.getByText("No sentiment report yet.")).toBeInTheDocument();
    expect(screen.getByText(/no mood reading/)).toBeInTheDocument();
  });

  it("separates verified facts from commentary and shows red flags and mood", () => {
    render(
      <NewsMood
        report={reportWith((report) => {
          report.news = {
            ...report.news,
            abstain: false,
            summary: "1 verified, 1 commentary.",
            verified_facts: [
              { published_at: "2025-03-12T12:00:00Z", source: "Wire", headline: "Guidance raised", url: null, is_commentary: false },
            ],
            commentary: [
              { published_at: "2025-03-11T12:00:00Z", source: "Blog", headline: "Why it will moon", url: null, is_commentary: true },
            ],
            red_flags: ["Unverified claim"],
          };
          report.sentiment = { ...report.sentiment, abstain: false, mood_score: 58, hype_risk: "LOW", summary: "Neutral crowd." };
        })}
      />,
    );
    expect(screen.getByText("DATED ITEM")).toBeInTheDocument();
    expect(screen.getByText("COMMENTARY")).toBeInTheDocument();
    expect(screen.getByText("Guidance raised")).toBeInTheDocument();
    expect(screen.getByText("! Unverified claim")).toBeInTheDocument();
    expect(screen.getByText(/mood 58 of 100 · hype risk LOW/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Mood score 58 out of 100" })).toBeInTheDocument();
  });
});

describe("news list length", () => {
  it("shows the newest four items, counts the rest and names the feed", () => {
    const item = (index: number) => ({
      published_at: `2025-03-${String(12 - index).padStart(2, "0")}T12:00:00Z`,
      source: "Reuters",
      headline: `Headline ${index}`,
      url: null,
      is_commentary: false,
    });
    render(
      <NewsMood
        newsSource="finnhub-company-news"
        report={reportWith((report) => {
          report.news = {
            ...report.news,
            abstain: false,
            summary: "7 dated item(s).",
            verified_facts: Array.from({ length: 7 }, (_, index) => item(index)),
          };
        })}
      />,
    );
    expect(screen.getAllByText("DATED ITEM")).toHaveLength(4);
    expect(screen.getByText("Headline 3")).toBeInTheDocument();
    expect(screen.queryByText("Headline 4")).not.toBeInTheDocument();
    expect(screen.getByText(/\+3 more\. 7 dated item\(s\)\./)).toBeInTheDocument();
    expect(screen.getByText(/dated sources only · finnhub-company-news/)).toBeInTheDocument();
  });
});

describe("watchlist and journal", () => {
  it("has empty states", () => {
    render(<Watchlist rows={[]} focusedSymbol={null} onSelect={vi.fn()} />);
    expect(screen.getByText("No symbols ranked yet.")).toBeInTheDocument();
  });

  it("shows unscored symbols as n/a", () => {
    render(
      <Watchlist
        rows={[
          { symbol: "TSLA", score: null, verdict: "NEED DATA", tone: "caution", relativeVolume: null, volumeSurge: false, signals: [] },
          { symbol: "NVDA", score: 0.7, verdict: "WATCH", tone: "up", relativeVolume: 1.83, volumeSurge: true, signals: [{ label: "52W HIGH ✓", tone: "up", description: "52w high at 108.00, confirmed by volume" }] },
          { symbol: "AAPL", score: 0.6, verdict: "WATCH", tone: "up", relativeVolume: 1.1, volumeSurge: false, signals: [{ label: "BIG GAINER · no vol", tone: "dim", description: "big gainer +37.5% from 80.00, not confirmed by volume" }] },
        ]}
        focusedSymbol={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("n/a")).toBeInTheDocument();
    expect(screen.getByText("NEED DATA")).toHaveClass("tone-caution");
    expect(screen.getByText("1.83x ▲ SURGE")).toHaveClass("amber");
    expect(screen.getByText("1.10x")).toHaveClass("label");
    expect(screen.getByText("52W HIGH ✓")).toHaveClass("tone-up");
    expect(screen.getByText("BIG GAINER · no vol")).toHaveClass("tone-dim");
    expect(screen.getByText("BIG GAINER · no vol")).toHaveAttribute(
      "title",
      "big gainer +37.5% from 80.00, not confirmed by volume",
    );
  });

  it("lists the latest events first, folds repeats and colours known agents only", () => {
    const recorded = cloneSnapshot().recent_events;
    render(<Journal events={recorded} />);
    const rows = screen.getAllByRole("row");
    expect(rows.length).toBeLessThanOrEqual(12);
    expect(within(rows[0]!).getByText("EXIT")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("VENUE")).not.toHaveClass("agent-text");
    // The weeks the position was held are one line, not one per bar.
    const held = screen.getByText(/position open/).closest("tr")!;
    expect(within(held).getByText(/· \d+ bars/)).toBeInTheDocument();
    expect(screen.getAllByText("PICARD")[0]).toHaveClass("agent-picard");
  });

  it("only folds lines that are identical and next to each other", () => {
    const base = cloneSnapshot().recent_events[0]!;
    const line = (kind: string, reason: string, symbol = "AAPL") => ({ ...base, kind, reason, symbol });
    const lines = journalLines([
      line("SKIP", "position_open"),
      line("SKIP", "position_open"),
      line("SKIP", "position_open", "MSFT"),
      line("WAIT", "position_open"),
      line("SKIP", "position_open"),
    ]);
    expect(lines.map((entry) => [entry.event.kind, entry.event.symbol, entry.repeatCount])).toEqual([
      ["SKIP", "AAPL", 1],
      ["WAIT", "AAPL", 1],
      ["SKIP", "MSFT", 1],
      ["SKIP", "AAPL", 2],
    ]);
    expect(journalLines(Array(40).fill(line("SKIP", "x")))).toEqual([
      { event: expect.anything(), repeatCount: 40 },
    ]);
    const distinct = Array.from({ length: 30 }, (_, index) => line("SKIP", `reason ${index}`));
    expect(journalLines(distinct)).toHaveLength(12);
    expect(journalLines(distinct, 3).map((entry) => entry.event.reason)).toEqual([
      "reason 29",
      "reason 28",
      "reason 27",
    ]);
  });
});

describe("chart", () => {
  it("invites a replay when there are no bars", () => {
    render(<ChartPanel symbol={null} candles={[]} levels={[]} report={undefined} />);
    expect(screen.getByText(/No bars yet. Start a replay to see the desk trade/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("draws one candle per bar, falling and rising, with a falling 20-day reading", () => {
    const candles = cloneSnapshot().candles_by_symbol.AAPL!;
    candles[40] = { ...candles[40]!, open: candles[40]!.high, close: candles[40]!.low };
    const { container } = render(
      <ChartPanel
        symbol="AAPL"
        candles={candles}
        levels={[]}
        report={reportWith((report) => (report.indicators.return20_pct = -2.5))}
      />,
    );
    expect(container.querySelectorAll(".candle-up, .candle-down")).toHaveLength(80);
    expect(container.querySelectorAll(".candle-down").length).toBeGreaterThan(0);
    expect(screen.getByText(/▼ -2\.50% 20D/)).toHaveClass("tone-down");
  });

  it("survives a single flat bar", () => {
    const flat = { timestamp: "2025-01-02T21:00:00Z", open: 10, high: 10, low: 10, close: 10, volume: 1 };
    render(<ChartPanel symbol="AAPL" candles={[flat]} levels={[]} report={undefined} />);
    expect(screen.getByRole("img", { name: "AAPL daily candles, last close 10.00" })).toBeInTheDocument();
  });
});

describe("balance", () => {
  const snapshot = cloneSnapshot();

  it("describes an open position", () => {
    const account = {
      ...snapshot.account!,
      equity: 99_500,
      positions: [
        { symbol: "AAPL", units: 171, entry_price: 115.89, stop: 112.35, target: 129.52, opened_at: "2025-03-14T21:00:00Z", planned_risk_amount: 754 },
      ],
    };
    render(<Balance account={account} equityCurve={[]} performance={performance(account, [])} />);
    expect(screen.getByText("OPEN AAPL · 171 sh @ 115.89 · stop 112.35")).toBeInTheDocument();
    expect(screen.getByText(/▼ -\$500\.00 · -0\.50% since start/)).toHaveClass("tone-down");
    expect(screen.getByText("Equity curve appears once bars arrive.")).toBeInTheDocument();
  });

  it("describes a queued order and an empty account", () => {
    const account = { ...snapshot.account!, pending_symbols: ["AAPL", "MSFT"] };
    const { rerender } = render(
      <Balance account={account} equityCurve={snapshot.equity_curve} performance={performance(account, snapshot.equity_curve)} />,
    );
    expect(screen.getByText("QUEUED AAPL, MSFT for next open")).toBeInTheDocument();
    rerender(<Balance account={null} equityCurve={[]} performance={performance(null, [])} />);
    expect(screen.getByText("NO OPEN POSITION")).toBeInTheDocument();
    expect(screen.getByText("– / –")).toBeInTheDocument();
  });
});

describe("header", () => {
  it("offers a speed the server reports even when it is not a preset", () => {
    render(
      <Header desk={cloneSnapshot({ bars_per_second: 500, bar_index: null })} connection="open" controls={noControls} />,
    );
    expect(screen.getByRole("option", { name: "500x" })).toBeInTheDocument();
    expect(screen.getByText("BAR –")).toBeInTheDocument();
  });

  it("has sensible defaults without a desk", () => {
    render(<Header desk={null} connection="connecting" controls={noControls} />);
    expect(screen.getByText("NO DESK")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveValue("2");
  });
});

describe("floor", () => {
  const cards = agentCards(cloneSnapshot(), "AAPL");

  it("shows what each agent says and its state in words", () => {
    render(<Floor cards={cards} working={false} barIndex={79} />);
    const floor = screen.getByRole("region", { name: "Trading floor" });
    expect(floor).not.toHaveClass("floor-working");
    expect(within(floor).getByText("who said what on the latest bar")).toBeInTheDocument();
    for (const card of cards) {
      expect(within(floor).getByText(card.speech)).toBeInTheDocument();
    }
    expect(floor.querySelectorAll(".seat")).toHaveLength(6);
    expect(floor.querySelectorAll(".hand-off")).toHaveLength(0);
  });

  it("hands the bar down the line only while the desk is running", () => {
    const { container, rerender } = render(<Floor cards={cards} working barIndex={12} />);
    expect(container.querySelector(".floor")).toHaveClass("floor-working");
    expect(container.querySelectorAll(".hand-off")).toHaveLength(6);
    expect(screen.getByText("at work · each bar passes down the line")).toBeInTheDocument();
    rerender(<Floor cards={cards} working barIndex={null} />);
    expect(container.querySelectorAll(".hand-off")).toHaveLength(0);
  });
});

describe("tape", () => {
  it("renders nothing without quotes", () => {
    const { container } = render(<Tape quotes={[]} focusedSymbol={null} onSelect={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows each quote with a direction in words and lets the user pick one", async () => {
    const onSelect = vi.fn();
    render(
      <Tape
        quotes={[
          { symbol: "AAPL", last: 102, change: 0.02 },
          { symbol: "MSFT", last: 400, change: -0.011 },
          { symbol: "NVDA", last: 90, change: null },
        ]}
        focusedSymbol="AAPL"
        onSelect={onSelect}
      />,
    );
    const tape = screen.getByRole("navigation", { name: "Tape" });
    expect(within(tape).getByText("▲ +2.00%")).toHaveClass("tone-up");
    expect(within(tape).getByText("▼ -1.10%")).toHaveClass("tone-down");
    expect(within(tape).getByText("· –")).toHaveClass("tone-dim");
    expect(within(tape).getByRole("button", { name: "AAPL" })).toHaveAttribute("aria-pressed", "true");
    within(tape).getByRole("button", { name: "MSFT" }).click();
    expect(onSelect).toHaveBeenCalledWith("MSFT");
  });
});
