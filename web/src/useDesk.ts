import { useCallback, useEffect, useMemo, useReducer } from "react";

import { ApiError, connectEvents, eventsUrl, postControl } from "./api";
import { deskReducer, INITIAL_STATE, type DeskState } from "./state";

export interface DeskControls {
  start: (symbols?: string[]) => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
  setSpeed: (barsPerSecond: number) => void;
  setKillSwitch: (engaged: boolean) => void;
  goLive: () => void;
  select: (symbol: string | null) => void;
}

/** Connects the page to the desk and exposes its state and controls. */
export function useDesk(): { state: DeskState; controls: DeskControls } {
  const [state, dispatch] = useReducer(deskReducer, INITIAL_STATE);

  useEffect(
    () =>
      connectEvents({
        url: eventsUrl(window.location),
        onMessage: (message) => dispatch({ kind: "message", message }),
        onConnectionChange: (connection) => dispatch({ kind: "connection", connection }),
      }),
    [],
  );

  const send = useCallback((path: string, body?: object) => {
    postControl(path, body).then(
      () => dispatch({ kind: "notice", notice: null }),
      (error: unknown) =>
        dispatch({
          kind: "notice",
          notice: error instanceof ApiError ? error.message : "The request failed.",
        }),
    );
  }, []);

  const controls = useMemo<DeskControls>(
    () => ({
      start: (symbols) => send("/replay/start", symbols?.length ? { symbols } : {}),
      pause: () => send("/replay/pause"),
      resume: () => send("/replay/resume"),
      stop: () => send("/replay/stop"),
      setSpeed: (barsPerSecond) => send("/replay/speed", { bars_per_second: barsPerSecond }),
      setKillSwitch: (engaged) => send("/kill-switch", { engaged }),
      goLive: () => send("/mode", { mode: "live" }),
      select: (symbol) => dispatch({ kind: "select", symbol }),
    }),
    [send],
  );

  return { state, controls };
}
