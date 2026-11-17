import { render, screen, within } from "@testing-library/react";

import { performance } from "../derive";
import { cloneSnapshot, reportWith } from "../test/fixtures";
import { Balance } from "./Balance";
import { ChartPanel } from "./ChartPanel";
import { DecisionPanel } from "./DecisionPanel";
import { Journal, NewsMood, Watchlist } from "./Feeds";
import { Header } from "./Header";
import { RiskPanel } from "./RiskPanel";

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
    expect(screen.getByText("VERIFIED FACT")).toBeInTheDocument();
    expect(screen.getByText("COMMENTARY")).toBeInTheDocument();
    expect(screen.getByText("Guidance raised")).toBeInTheDocument();
    expect(screen.getByText("! Unverified claim")).toBeInTheDocument();
    expect(screen.getByText(/mood 58 of 100 · hype risk LOW/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Mood score 58 out of 100" })).toBeInTheDocument();
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
        rows={[{ symbol: "TSLA", score: null, verdict: "NEED DATA", tone: "caution" }]}
        focusedSymbol={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("n/a")).toBeInTheDocument();
    expect(screen.getByText("NEED DATA")).toHaveClass("tone-caution");
  });

  it("lists the latest events first and colours known agents only", () => {
    const recorded = cloneSnapshot().recent_events;
    const events = [
      ...recorded.slice(0, 20),
      recorded.find((event) => event.agent === "PICARD")!,
      recorded.find((event) => event.kind === "EXIT")!,
    ];
    render(<Journal events={events} />);
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(12);
    expect(within(rows[0]!).getByText("EXIT")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("VENUE")).not.toHaveClass("agent-text");
    expect(within(rows[1]!).getByText("PICARD")).toHaveClass("agent-picard");
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
