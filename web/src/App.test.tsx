import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "./App";
import {
  cloneSnapshot,
  messageSample,
  reportWith,
  reviewSample,
  statusMessage,
  tradeDecisionSample,
  tradeReport,
} from "./test/fixtures";
import type { ServerMessage } from "./types";

class FakeSocket {
  static latest: FakeSocket;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.latest = this;
  }
  close() {}
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  // A fresh answer per call: the page both posts controls and loads the trade review.
  fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => vi.unstubAllGlobals());

function renderDesk(...messages: ServerMessage[]) {
  const view = render(<App />);
  act(() => {
    FakeSocket.latest.onopen!();
    for (const message of messages) FakeSocket.latest.onmessage!({ data: JSON.stringify(message) });
  });
  return view;
}

function send(...messages: ServerMessage[]) {
  act(() => {
    for (const message of messages) FakeSocket.latest.onmessage!({ data: JSON.stringify(message) });
  });
}

const lastPost = () => {
  const [path, options] = fetchMock.mock.calls.at(-1)!;
  return [path, JSON.parse(options.body)];
};

describe("terminal page", () => {
  it("shows an offline desk until the server answers", () => {
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("CONNECTING");
    const agents = screen.getByRole("region", { name: "Agents" });
    expect(within(agents).getAllByText("OFFLINE")).toHaveLength(6);
    expect(within(screen.getByRole("region", { name: "Trading floor" })).getAllByText("OFFLINE")).toHaveLength(6);
    expect(screen.getByText("No decision yet.")).toBeInTheDocument();
    expect(screen.getByText("No risk review yet.")).toBeInTheDocument();
    expect(screen.getByText("Nothing recorded yet.")).toBeInTheDocument();
    expect(screen.getByText("waiting for the first decision")).toBeInTheDocument();
    act(() => FakeSocket.latest.onclose!());
    expect(screen.getByRole("status")).toHaveTextContent("OFFLINE · RETRYING");
  });

  it("connects to the event stream on the page's own host", () => {
    renderDesk();
    expect(FakeSocket.latest.url).toBe(`ws://${window.location.host}/ws/events`);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders a whole recorded replay from the real server stream", () => {
    renderDesk(...messageSample);
    expect(screen.getByText("FINISHED")).toBeInTheDocument();
    expect(screen.getByText("BAR 80/80")).toBeInTheDocument();
    expect(screen.getByText("2025-04-23 21:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("$101,676.04")).toBeInTheDocument();
    expect(screen.getByText(/\+\$1,676\.04 · \+1\.68% since start/)).toBeInTheDocument();

    const agents = screen.getByRole("region", { name: "Agents" });
    for (const name of ["HAWKEYE", "NEO", "SNOWDEN", "FAMA", "ODIN", "PICARD"]) {
      expect(within(agents).getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("img", { name: /AAPL daily candles, last close 125\.75/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Paper equity from \$100,000\.00 to \$101,\d+\.\d\d over 80 bars/ })).toBeInTheDocument();

    const journal = screen.getByRole("region", { name: "Journal" });
    expect(within(journal).getByText("EXIT")).toBeInTheDocument();
    expect(within(journal).getByText("END OF RUN")).toBeInTheDocument();
    const balance = screen.getByRole("region", { name: "Balance" });
    expect(within(balance).getByText("TRADES").nextSibling).toHaveTextContent("1");
    expect(within(balance).getByText("WIN RATE").nextSibling).toHaveTextContent("100%");
    expect(within(balance).getByText("NO OPEN POSITION")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "REPLAY AGAIN" })).toBeInTheDocument();
  });

  it("shows the approved trade ticket, risk checks and pipeline at the decision bar", () => {
    const desk = cloneSnapshot({ status: "RUNNING" });
    desk.reports_by_symbol = { AAPL: tradeReport() };
    renderDesk(desk);
    const decision = screen.getByRole("region", { name: "Decision" });
    expect(within(decision).getByText("TRADE")).toBeInTheDocument();
    expect(within(decision).getByText("171 SH")).toBeInTheDocument();
    expect(within(decision).getByText("$19,946.06")).toBeInTheDocument();
    expect(within(decision).getByText("$754.12")).toBeInTheDocument();
    expect(within(decision).getByText("2.89")).toBeInTheDocument();
    expect(within(decision).getByRole("list", { name: "Dissent kept on record" }).children).toHaveLength(3);

    const risk = screen.getByRole("region", { name: "Risk checks" });
    expect(within(risk).getByText("APPROVE 9/9")).toBeInTheDocument();
    expect(within(risk).getAllByText("✓ PASS")).toHaveLength(9);

    const pipeline = screen.getByRole("navigation", { name: "Decision pipeline" });
    expect(within(pipeline).getByText("PICARD TRADE")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /target 129\.52, entry 116\.64, stop 112\.35/ })).toBeInTheDocument();
  });

  it("sends each header control to the server", async () => {
    const user = userEvent.setup();
    renderDesk(cloneSnapshot({ status: "RUNNING" }));
    await user.click(screen.getByRole("button", { name: "PAUSE" }));
    expect(lastPost()).toEqual(["/api/replay/pause", {}]);
    await user.click(screen.getByRole("button", { name: "STOP" }));
    expect(lastPost()).toEqual(["/api/replay/stop", {}]);
    await user.selectOptions(screen.getByRole("combobox", { name: /Replay speed/ }), "16");
    expect(lastPost()).toEqual(["/api/replay/speed", { bars_per_second: 16 }]);
    await user.click(screen.getByRole("button", { name: "GO LIVE" }));
    expect(lastPost()).toEqual(["/api/mode", { mode: "live" }]);

    send(statusMessage({ status: "PAUSED" }));
    await user.click(screen.getByRole("button", { name: "RESUME" }));
    expect(lastPost()).toEqual(["/api/replay/resume", {}]);

    send(statusMessage({ status: "IDLE" }));
    await user.click(screen.getByRole("button", { name: "START REPLAY" }));
    expect(lastPost()).toEqual(["/api/replay/start", {}]);
  });

  it("engages and releases the kill switch, and says so loudly", async () => {
    const user = userEvent.setup();
    renderDesk(cloneSnapshot());
    const killSwitch = screen.getByRole("button", { name: "KILL SWITCH" });
    expect(killSwitch).toHaveAttribute("aria-pressed", "false");
    await user.click(killSwitch);
    expect(lastPost()).toEqual(["/api/kill-switch", { engaged: true }]);

    send(statusMessage({ kill_switch_engaged: true }));
    expect(screen.getByRole("alert")).toHaveTextContent("KILL SWITCH ENGAGED");
    expect(within(screen.getByRole("region", { name: "Agents" })).getAllByText("HALTED")).toHaveLength(6);
    expect(within(screen.getByRole("region", { name: "Trading floor" })).getAllByText("Standing down.")).toHaveLength(6);
    const release = screen.getByRole("button", { name: /RELEASE KILL SWITCH/ });
    expect(release).toHaveAttribute("aria-pressed", "true");
    await user.click(release);
    expect(lastPost()).toEqual(["/api/kill-switch", { engaged: false }]);
  });

  it("shows the server's refusal next to the command line and clears it on success", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (path: string) =>
      path === "/api/mode"
        ? new Response('{"detail":"Live polling is not available yet"}', { status: 501 })
        : new Response("{}", { status: 200 }),
    );
    renderDesk(cloneSnapshot());
    await user.click(screen.getByRole("button", { name: "GO LIVE" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Live polling is not available yet");
    await user.click(screen.getByRole("button", { name: "KILL SWITCH" }));
    await vi.waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("reports a failed request that is not an API refusal", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (path: string) => {
      if (path === "/api/mode") throw new Error("boom");
      return new Response("{}", { status: 200 });
    });
    renderDesk(cloneSnapshot());
    await user.click(screen.getByRole("button", { name: "GO LIVE" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The desk server is not reachable.");
  });

  it("runs typed commands", async () => {
    const user = userEvent.setup();
    renderDesk(cloneSnapshot({ status: "RUNNING" }));
    const input = screen.getByLabelText("DESK>");
    const run = async (text: string) => {
      await user.clear(input);
      await user.type(input, `${text}{Enter}`);
    };
    await run("pause");
    expect(lastPost()).toEqual(["/api/replay/pause", {}]);
    await run("resume");
    expect(lastPost()).toEqual(["/api/replay/resume", {}]);
    await run("stop");
    expect(lastPost()).toEqual(["/api/replay/stop", {}]);
    await run("speed 32");
    expect(lastPost()).toEqual(["/api/replay/speed", { bars_per_second: 32 }]);
    await run("kill");
    expect(lastPost()).toEqual(["/api/kill-switch", { engaged: true }]);
    await run("start aapl msft");
    expect(lastPost()).toEqual(["/api/replay/start", { symbols: ["AAPL", "MSFT"] }]);
    expect(input).toHaveValue("");

    const callsBefore = fetchMock.mock.calls.length;
    await run("tsla");
    expect(screen.getByRole("alert")).toHaveTextContent("TSLA is not in this replay");
    expect(input).toHaveValue("tsla");
    expect(fetchMock.mock.calls).toHaveLength(callsBefore);
    await run("aapl");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lets the user pick a symbol from the watchlist", async () => {
    const user = userEvent.setup();
    const desk = cloneSnapshot();
    desk.settings!.symbols = ["AAPL", "MSFT"];
    desk.reports_by_symbol = {
      AAPL: tradeReport(),
      MSFT: reportWith((report) => {
        report.symbol = "MSFT";
        report.scout.score = 0.31;
        report.decision = { ...report.decision, action: "WAIT", plan: null, required_confirmation: "A close above SMA20." };
      }),
    };
    desk.candles_by_symbol.MSFT = desk.candles_by_symbol.AAPL!.slice(0, 5);
    renderDesk(desk);
    const watchlist = screen.getByRole("region", { name: "Watchlist" });
    expect(within(watchlist).getByRole("button", { name: "AAPL" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(watchlist).getByRole("button", { name: "MSFT" }));
    expect(within(watchlist).getByRole("button", { name: "MSFT" })).toHaveAttribute("aria-pressed", "true");
    const decision = screen.getByRole("region", { name: "Decision" });
    expect(within(decision).getByText("WAIT")).toBeInTheDocument();
    expect(within(decision).getByText("Needs: A close above SMA20.")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /MSFT daily candles/ })).toBeInTheDocument();
  });

  it("says plainly when a replay failed", () => {
    renderDesk(cloneSnapshot({ status: "FAILED", error: "RuntimeError" }));
    expect(screen.getByRole("alert")).toHaveTextContent("REPLAY FAILED · RuntimeError");
    send(statusMessage({ status: "FAILED", error: null }));
    expect(screen.getByRole("alert")).toHaveTextContent("unknown error");
  });

  it("shows a live run: live badge, session count, live price, and no replay speed", async () => {
    const user = userEvent.setup();
    renderDesk(cloneSnapshot({ status: "RUNNING", mode: "live", bar_index: 2, bar_count: null }));
    const header = screen.getByRole("banner");
    expect(within(header).getByText("● LIVE DATA")).toBeInTheDocument();
    expect(within(header).getByText("PAPER")).toBeInTheDocument();
    expect(within(header).getByText("SESSION 3")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Replay speed/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "GO LIVE" })).not.toBeInTheDocument();
    expect(screen.getByText(/Live prices are for display/)).toBeInTheDocument();

    const chart = screen.getByRole("region", { name: "Chart" });
    expect(within(chart).queryByText("LIVE")).not.toBeInTheDocument();
    send({
      type: "prices",
      latest_prices: { AAPL: 352.66 },
      prices_as_of: "2026-11-17T15:00:05Z",
    });
    expect(within(chart).getByText("352.66")).toBeInTheDocument();
    expect(within(chart).getByText("15:00:05 UTC")).toBeInTheDocument();
    expect(within(chart).getByText("CLOSE")).toBeInTheDocument();

    send(statusMessage({ status: "RUNNING", mode: "live", error: "ConnectError" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "LIVE DATA CHECK FAILED · ConnectError · retrying",
    );
    send(statusMessage({ status: "IDLE", mode: "live", bar_index: null }));
    expect(within(header).getByText("SESSION –")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "GO LIVE" }));
    expect(lastPost()).toEqual(["/api/mode", { mode: "live" }]);
  });

  it("goes live from the command line", async () => {
    const user = userEvent.setup();
    renderDesk(cloneSnapshot());
    await user.type(screen.getByRole("textbox"), "live{Enter}");
    expect(lastPost()).toEqual(["/api/mode", { mode: "live" }]);
  });

  it("loads the trade review and opens the saved decision behind a trade", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (path: string) =>
      path === "/api/review"
        ? new Response(JSON.stringify(reviewSample), { status: 200 })
        : path === "/api/review/trades/0/decision"
          ? new Response(JSON.stringify(tradeDecisionSample), { status: 200 })
          : new Response("{}", { status: 200 }),
    );
    renderDesk(cloneSnapshot());
    const panel = screen.getByRole("region", { name: "Trade review" });
    expect(await within(panel).findByText("2W / 2L · +$2,580.98")).toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "Why trade 1 was taken" }));
    const detail = await within(panel).findByRole("region", { name: "Saved decision" });
    expect(await within(detail).findByText("PLANNED ENTRY")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "CLOSE" }));
    expect(within(panel).queryByRole("region", { name: "Saved decision" })).not.toBeInTheDocument();
  });

  it("says so when a saved decision cannot be loaded, and keeps the review", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (path: string) => {
      if (path === "/api/review") return new Response(JSON.stringify(reviewSample), { status: 200 });
      if (path === "/api/review/trades/0/decision") throw new Error("offline");
      return new Response("{}", { status: 200 });
    });
    renderDesk(cloneSnapshot());
    const panel = screen.getByRole("region", { name: "Trade review" });
    await user.click(await within(panel).findByRole("button", { name: "Why trade 1 was taken" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "The desk server is not reachable.",
    );
    expect(within(panel).getByText("2W / 2L · +$2,580.98")).toBeInTheDocument();
  });

  it("shows an empty review when the review cannot be loaded", async () => {
    fetchMock.mockImplementation(async () => new Response("not json", { status: 200 }));
    renderDesk(cloneSnapshot());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/review"));
    expect(screen.getByText(/No closed trades yet/)).toBeInTheDocument();
  });
});
