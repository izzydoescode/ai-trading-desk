import { barTime } from "../format";
import type { ConnectionState } from "../state";
import type { Snapshot } from "../types";
import type { DeskControls } from "../useDesk";

export const SPEED_CHOICES = [1, 2, 4, 8, 16, 32];

interface HeaderProps {
  desk: Snapshot | null;
  connection: ConnectionState;
  controls: DeskControls;
}

export function Header({ desk, connection, controls }: HeaderProps) {
  const status = desk?.status ?? "IDLE";
  const isActive = status === "RUNNING" || status === "PAUSED";
  const killSwitchEngaged = desk?.kill_switch_engaged ?? false;
  const barPosition =
    desk?.bar_index != null && desk.bar_count != null
      ? `BAR ${desk.bar_index + 1}/${desk.bar_count}`
      : "BAR –";

  return (
    <header className={`panel header${killSwitchEngaged ? " header-halted" : ""}`}>
      <div className="brand">
        <span className="mark">DESK</span>
        <h1>AI TRADING DESK</h1>
      </div>
      <div className="run-status">
        <span className="badge tone-info">REPLAY</span>
        <span className="badge tone-up">PAPER</span>
        <span className={`badge tone-${STATUS_TONES[status]}`}>{status}</span>
        {connection !== "open" && (
          <span className="badge tone-down" role="status">
            {connection === "connecting" ? "CONNECTING" : "OFFLINE · RETRYING"}
          </span>
        )}
        <span className="label">{desk?.market.toUpperCase() ?? "NO DESK"}</span>
        <span>{barTime(desk?.bar_time)}</span>
        <span className="label">{barPosition}</span>
        <span className="label">DATA {desk?.data_source ?? "–"}</span>
      </div>
      <div className="controls">
        <label className="speed">
          <span className="label">SPEED</span>
          <select
            aria-label="Replay speed in bars per second"
            value={desk?.bars_per_second ?? 2}
            onChange={(event) => controls.setSpeed(Number(event.target.value))}
          >
            {[...new Set([...SPEED_CHOICES, desk?.bars_per_second ?? 2])]
              .sort((first, second) => first - second)
              .map((speed) => (
                <option key={speed} value={speed}>
                  {speed}x
                </option>
              ))}
          </select>
        </label>
        {status === "RUNNING" && (
          <button type="button" onClick={controls.pause}>
            PAUSE
          </button>
        )}
        {status === "PAUSED" && (
          <button type="button" onClick={controls.resume}>
            RESUME
          </button>
        )}
        {isActive ? (
          <button type="button" onClick={controls.stop}>
            STOP
          </button>
        ) : (
          <button type="button" className="primary" onClick={() => controls.start()}>
            {status === "IDLE" ? "START REPLAY" : "REPLAY AGAIN"}
          </button>
        )}
        <button type="button" onClick={controls.goLive} title="Live polling is not built yet">
          GO LIVE
        </button>
        <button
          type="button"
          className="danger"
          aria-pressed={killSwitchEngaged}
          onClick={() => controls.setKillSwitch(!killSwitchEngaged)}
        >
          {killSwitchEngaged ? "■ RELEASE KILL SWITCH" : "KILL SWITCH"}
        </button>
      </div>
    </header>
  );
}

const STATUS_TONES = {
  IDLE: "dim",
  RUNNING: "amber",
  PAUSED: "caution",
  FINISHED: "up",
  FAILED: "down",
} as const;
