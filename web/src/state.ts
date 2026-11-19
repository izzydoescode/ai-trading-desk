/** Folds the server's message stream into the one object the terminal renders. */

import type { ServerMessage, Snapshot, StatusFields } from "./types";

export const CANDLE_HISTORY_LIMIT = 400;
export const RECENT_EVENT_LIMIT = 200;

export type ConnectionState = "connecting" | "open" | "closed";

export interface DeskState {
  connection: ConnectionState;
  desk: Snapshot | null;
  /** Symbol the user picked; null means "follow the desk". */
  selectedSymbol: string | null;
  /** Last control error, shown until the next successful action. */
  notice: string | null;
}

export type DeskAction =
  | { kind: "message"; message: ServerMessage }
  | { kind: "connection"; connection: ConnectionState }
  | { kind: "select"; symbol: string | null }
  | { kind: "notice"; notice: string | null };

export const INITIAL_STATE: DeskState = {
  connection: "connecting",
  desk: null,
  selectedSymbol: null,
  notice: null,
};

const STATUS_KEYS = [
  "status",
  "mode",
  "venue",
  "kill_switch_engaged",
  "bars_per_second",
  "bar_index",
  "bar_count",
  "bar_time",
  "error",
] as const satisfies readonly (keyof StatusFields)[];

function statusFieldsOf(message: StatusFields): StatusFields {
  return Object.fromEntries(STATUS_KEYS.map((key) => [key, message[key]])) as unknown as StatusFields;
}

function applyMessage(desk: Snapshot | null, message: ServerMessage): Snapshot | null {
  if (message.type === "snapshot") return message;
  // Everything else is an update to a snapshot we must already hold.
  if (desk === null) return null;
  switch (message.type) {
    case "status":
      return { ...desk, ...statusFieldsOf(message) };
    case "account":
      return { ...desk, account: message.account };
    case "prices":
      return {
        ...desk,
        latest_prices: message.latest_prices,
        prices_as_of: message.prices_as_of,
      };
    case "event":
      return {
        ...desk,
        run_id: message.event.run_id ?? desk.run_id,
        recent_events: [...desk.recent_events, message.event].slice(-RECENT_EVENT_LIMIT),
      };
    case "bar": {
      const candlesBySymbol = { ...desk.candles_by_symbol };
      for (const [symbol, candle] of Object.entries(message.candles_by_symbol)) {
        candlesBySymbol[symbol] = [...(candlesBySymbol[symbol] ?? []), candle].slice(
          -CANDLE_HISTORY_LIMIT,
        );
      }
      return {
        ...desk,
        ...statusFieldsOf(message),
        account: message.account,
        equity_curve: [...desk.equity_curve, message.equity_point],
        candles_by_symbol: candlesBySymbol,
        reports_by_symbol: { ...desk.reports_by_symbol, ...message.reports_by_symbol },
      };
    }
  }
}

export function deskReducer(state: DeskState, action: DeskAction): DeskState {
  switch (action.kind) {
    case "connection":
      return { ...state, connection: action.connection };
    case "select":
      return { ...state, selectedSymbol: action.symbol };
    case "notice":
      return { ...state, notice: action.notice };
    case "message": {
      const desk = applyMessage(state.desk, action.message);
      // A new run may not include the symbol the user had picked.
      const selectionStillValid =
        state.selectedSymbol === null ||
        desk === null ||
        desk.settings === null ||
        desk.settings.symbols.includes(state.selectedSymbol);
      return {
        ...state,
        desk,
        selectedSymbol: selectionStillValid ? state.selectedSymbol : null,
      };
    }
  }
}
