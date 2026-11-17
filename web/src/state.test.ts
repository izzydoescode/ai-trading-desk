import { CANDLE_HISTORY_LIMIT, deskReducer, INITIAL_STATE, RECENT_EVENT_LIMIT, type DeskState } from "./state";
import { cloneSnapshot, messageSample, statusMessage } from "./test/fixtures";
import type { BarMessage, DeskEvent, ServerMessage } from "./types";

function fold(messages: ServerMessage[], state: DeskState = INITIAL_STATE): DeskState {
  return messages.reduce((current, message) => deskReducer(current, { kind: "message", message }), state);
}

const firstBar = messageSample.find((message) => message.type === "bar") as BarMessage;
const anEvent = (sequence: number): DeskEvent => ({
  run_id: "run-1",
  sequence,
  timestamp: "2025-03-13T21:00:00Z",
  symbol: "AAPL",
  agent: "PICARD",
  kind: "WAIT",
  reason: "waiting",
  detail: {},
});

describe("desk state", () => {
  it("ignores updates until the first snapshot arrives", () => {
    const state = fold([firstBar, { type: "event", event: anEvent(0) }]);
    expect(state.desk).toBeNull();
  });

  it("folds a recorded server stream into the final desk", () => {
    const state = fold(messageSample);
    const desk = state.desk!;
    expect(desk.status).toBe("FINISHED");
    expect(desk.equity_curve).toHaveLength(messageSample.filter((m) => m.type === "bar").length);
    expect(desk.candles_by_symbol.AAPL).toHaveLength(desk.equity_curve.length);
    expect(desk.reports_by_symbol.AAPL).toBeDefined();
    expect(desk.recent_events.map((event) => event.kind)).toEqual(
      expect.arrayContaining(["SUBMIT", "FILL", "EXIT"]),
    );
    expect(desk.run_id).toBe(desk.recent_events[0]!.run_id);
    expect(desk.account!.closed_trades).toHaveLength(1);
  });

  it("does not mutate the previous state", () => {
    const before = fold([cloneSnapshot()]);
    const frozenCurveLength = before.desk!.equity_curve.length;
    const after = fold([firstBar], before);
    expect(before.desk!.equity_curve).toHaveLength(frozenCurveLength);
    expect(after.desk!.equity_curve).toHaveLength(frozenCurveLength + 1);
  });

  it("caps candle history and the journal", () => {
    let state = fold([cloneSnapshot({ candles_by_symbol: {}, recent_events: [] })]);
    for (let index = 0; index < CANDLE_HISTORY_LIMIT + 5; index += 1) {
      state = fold([firstBar], state);
    }
    for (let index = 0; index < RECENT_EVENT_LIMIT + 5; index += 1) {
      state = fold([{ type: "event", event: anEvent(index) }], state);
    }
    expect(state.desk!.candles_by_symbol.AAPL).toHaveLength(CANDLE_HISTORY_LIMIT);
    expect(state.desk!.recent_events).toHaveLength(RECENT_EVENT_LIMIT);
    expect(state.desk!.recent_events.at(-1)!.sequence).toBe(RECENT_EVENT_LIMIT + 4);
  });

  it("keeps the run id when a control event carries none", () => {
    const state = fold([
      cloneSnapshot({ run_id: "run-1" }),
      { type: "event", event: { ...anEvent(1), run_id: null, sequence: null } },
    ]);
    expect(state.desk!.run_id).toBe("run-1");
  });

  it("applies status and account updates", () => {
    const state = fold([
      cloneSnapshot(),
      statusMessage({ status: "PAUSED", kill_switch_engaged: true }),
      { type: "account", account: null },
    ]);
    expect(state.desk!.status).toBe("PAUSED");
    expect(state.desk!.kill_switch_engaged).toBe(true);
    expect(state.desk!.account).toBeNull();
    expect(state.desk!.equity_curve.length).toBeGreaterThan(0); // status carries no data
  });

  it("tracks connection, selection and notices", () => {
    let state = deskReducer(INITIAL_STATE, { kind: "connection", connection: "open" });
    state = deskReducer(state, { kind: "select", symbol: "AAPL" });
    state = deskReducer(state, { kind: "notice", notice: "A replay is already running" });
    expect(state).toMatchObject({
      connection: "open",
      selectedSymbol: "AAPL",
      notice: "A replay is already running",
    });
  });

  it("drops a selection the new run does not include, and keeps one it does", () => {
    const selected = deskReducer(INITIAL_STATE, { kind: "select", symbol: "AAPL" });
    expect(fold([cloneSnapshot()], selected).selectedSymbol).toBe("AAPL");
    const otherRun = cloneSnapshot();
    otherRun.settings!.symbols = ["MSFT"];
    expect(fold([otherRun], selected).selectedSymbol).toBeNull();
    expect(fold([cloneSnapshot({ settings: null })], selected).selectedSymbol).toBe("AAPL");
  });
});
