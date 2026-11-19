import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { reviewSample, tradeDecisionSample } from "../test/fixtures";
import type { TradeReview } from "../types";
import { Review, signedR } from "./Review";

const review = reviewSample.review;

function renderReview(overrides: Partial<Parameters<typeof Review>[0]> = {}) {
  const handlers = { onOpenDecision: vi.fn(), onCloseDecision: vi.fn() };
  render(<Review review={review} opened={null} {...handlers} {...overrides} />);
  return handlers;
}

describe("trade review", () => {
  it("writes R multiples with a sign and shows a dash when there is none", () => {
    expect(signedR(2.36125)).toBe("+2.36R");
    expect(signedR(-0.65)).toBe("−0.65R");
    expect(signedR(0)).toBe("0.00R");
    expect(signedR(null)).toBe("–");
    expect(signedR(undefined)).toBe("–");
  });

  it("says there is nothing to review before any trade closes", () => {
    renderReview({ review: null });
    expect(screen.getByText(/No closed trades yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("treats a review with no trades the same as no review", () => {
    const emptyReview: TradeReview = { ...review, trade_count: 0, trades: [] };
    renderReview({ review: emptyReview });
    expect(screen.getByText(/No closed trades yet/)).toBeInTheDocument();
  });

  it("summarises wins and losses in dollars and in R", () => {
    renderReview();
    const panel = screen.getByRole("region", { name: "Trade review" });
    expect(within(panel).getByText("2W / 2L · +$2,580.98")).toBeInTheDocument();
    const tiles = within(panel).getAllByRole("definition").map((value) => value.parentElement!);
    const tile = (name: string) => tiles.find((entry) => entry.firstChild!.textContent === name)!;
    expect(tile("TRADES")).toHaveTextContent("4");
    expect(tile("WIN RATE")).toHaveTextContent("50%");
    expect(tile("AVG WIN")).toHaveTextContent("$1,780.67 · +2.36R");
    expect(tile("AVG LOSS")).toHaveTextContent("-$490.18 · −0.65R");
    expect(tile("AVG RESULT / TRADE")).toHaveTextContent("+0.86R");
    expect(tile("BIGGEST WIN")).toHaveTextContent("$1,885.30");
    expect(tile("BIGGEST LOSS")).toHaveTextContent("-$754.12");
    expect(tile("AVG SESSIONS HELD")).toHaveTextContent("12.5");
  });

  it("breaks results down by strategy, exit, symbol, holding time and side", () => {
    renderReview();
    const strategy = screen.getByRole("table", { name: "BY STRATEGY" });
    const trendRow = within(strategy).getByRole("row", { name: /^TREND/ });
    expect(trendRow).toHaveTextContent("3");
    expect(trendRow).toHaveTextContent("67%");
    expect(trendRow).toHaveTextContent("+$2,807.22");
    expect(trendRow).toHaveTextContent("+1.24R");
    expect(within(strategy).getByRole("row", { name: /^BOLLINGER/ })).toHaveTextContent("−0.30R");

    const exits = screen.getByRole("table", { name: "BY EXIT" });
    expect(within(exits).getByRole("row", { name: /^end of run/i })).toBeInTheDocument();
    expect(within(exits).getByRole("row", { name: /^stalled/i })).toHaveTextContent("0%");
    expect(within(screen.getByRole("table", { name: "BY SYMBOL" })).getAllByRole("row")).toHaveLength(4);
    const holding = within(screen.getByRole("table", { name: "BY HOLDING TIME" })).getAllByRole("rowheader");
    expect(holding.map((cell) => cell.textContent)).toEqual([
      "3-5 sessions",
      "6-10 sessions",
      "11+ sessions",
    ]);
    expect(screen.getByRole("table", { name: "BY SIDE" })).toBeInTheDocument();
  });

  it("leaves the side table out when every trade was on one side, and shows one dash for no losses", () => {
    renderReview({
      review: {
        ...review,
        by_side: review.by_side.slice(0, 1),
        average_loss_amount: null,
        average_loss_r: null,
        largest_loss: null,
        average_bars_held: null,
      },
    });
    expect(screen.queryByRole("table", { name: "BY SIDE" })).not.toBeInTheDocument();
    expect(screen.getByText("AVG LOSS").parentElement).toHaveTextContent(/^AVG LOSS–$/);
    expect(screen.getByText("BIGGEST LOSS").parentElement).toHaveTextContent(/^BIGGEST LOSS–$/);
    expect(screen.getByText("AVG SESSIONS HELD").parentElement).toHaveTextContent(/–$/);
  });

  it("plots every trade's best and worst point with a glyph as well as a colour", () => {
    renderReview();
    const plot = screen.getByRole("img", { name: "Best and worst open result of 4 closed trades" });
    expect(plot.querySelectorAll("text.point")).toHaveLength(4);
    expect(plot.querySelectorAll("text.point.tone-up")).toHaveLength(2);
    expect([...plot.querySelectorAll("text.point.tone-down")].map((point) => point.firstChild!.textContent)).toEqual(["▼", "▼"]);
    expect(plot).toHaveTextContent("#2 MSFT −1.00R · best +0.40R · worst −1.20R");
  });

  it("lists every closed trade newest first and offers the saved decision where one exists", async () => {
    const user = userEvent.setup();
    const handlers = renderReview();
    const trades = screen.getByRole("table", { name: /EVERY CLOSED TRADE/ });
    const rows = within(trades).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getByRole("rowheader").textContent)).toEqual(["4", "3", "2", "1"]);
    expect(rows[0]).toHaveTextContent("MSFT");
    expect(rows[0]).toHaveTextContent("SHORT");
    expect(rows[0]).toHaveTextContent(/stalled/i);
    expect(rows[0]).toHaveTextContent("▼ -$226.24");
    expect(rows[3]).toHaveTextContent("▲ +$1,676.04");
    expect(rows[3]).toHaveTextContent("+2.22R");

    expect(within(trades).getAllByRole("button")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Why trade 1 was taken" }));
    expect(handlers.onOpenDecision).toHaveBeenCalledWith(0);
    expect(within(rows[0]!).getByTitle(/No decision report was saved/)).toBeInTheDocument();
  });

  it("shows the saved decision: the plan, each agent's view, the dissent and ODIN's checks", async () => {
    const user = userEvent.setup();
    const handlers = renderReview({
      opened: { tradeNumber: 0, report: tradeDecisionSample, error: null },
    });
    const detail = screen.getByRole("region", { name: "Saved decision" });
    expect(detail).toHaveTextContent("WHY TRADE 1 WAS TAKEN");
    expect(detail).toHaveTextContent("Decided on 03-13 at a close of 116.64");
    expect(within(detail).getByText("PLANNED ENTRY").parentElement).toHaveTextContent("116.64");
    expect(within(detail).getByText("STOP").parentElement).toHaveTextContent("112.35");
    expect(within(detail).getByText("TARGET").parentElement).toHaveTextContent("129.52");
    expect(within(detail).getByText("RISKED").parentElement).toHaveTextContent("$754.12 · 2.89 : 1");
    for (const agent of ["HAWKEYE", "NEO", "NEO counter-case", "SNOWDEN", "FAMA", "PICARD"]) {
      expect(within(detail).getByText(agent)).toBeInTheDocument();
    }
    expect(detail).toHaveTextContent("Dissent kept: NEO counter-case=NEUTRAL vs thesis=BULLISH");
    expect(detail).toHaveTextContent("ODIN APPROVE: ✓");
    expect(screen.getByRole("button", { name: "Why trade 1 was taken" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await user.click(within(detail).getByRole("button", { name: "CLOSE" }));
    expect(handlers.onCloseDecision).toHaveBeenCalled();
  });

  it("says when the saved decision is loading, failed, or follows the band", () => {
    const { unmount } = render(
      <Review
        review={review}
        opened={{ tradeNumber: 0, report: null, error: null }}
        onOpenDecision={vi.fn()}
        onCloseDecision={vi.fn()}
      />,
    );
    expect(screen.getByText("Loading the saved report…")).toBeInTheDocument();
    unmount();

    renderReview({ opened: { tradeNumber: 0, report: null, error: "The desk refused the request (404)." } });
    expect(screen.getByRole("alert")).toHaveTextContent("The desk refused the request (404).");
    expect(screen.queryByText("Loading the saved report…")).not.toBeInTheDocument();
  });

  it("marks a band-following target and copes with a report that has no plan or dissent", () => {
    const bandReport = structuredClone(tradeDecisionSample);
    bandReport.decision.plan!.target_follows_band = true;
    const { unmount } = render(
      <Review
        review={review}
        opened={{ tradeNumber: 0, report: bandReport, error: null }}
        onOpenDecision={vi.fn()}
        onCloseDecision={vi.fn()}
      />,
    );
    expect(
      within(screen.getByRole("region", { name: "Saved decision" })).getByText("TARGET").parentElement,
    ).toHaveTextContent("129.52 · follows band");
    unmount();

    const bareReport = structuredClone(tradeDecisionSample);
    bareReport.decision.plan = null;
    bareReport.decision.dissent = [];
    renderReview({ opened: { tradeNumber: 0, report: bareReport, error: null } });
    expect(screen.queryByText("PLANNED ENTRY")).not.toBeInTheDocument();
    expect(screen.queryByText(/Dissent kept/)).not.toBeInTheDocument();
  });
});
