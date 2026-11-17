import { useState, type FormEvent } from "react";

import { parseCommand } from "../command";
import { AGENTS } from "../derive";
import type { DeskControls } from "../useDesk";

interface CommandLineProps {
  knownSymbols: string[];
  controls: DeskControls;
  notice: string | null;
}

export function CommandLine({ knownSymbols, controls, notice }: CommandLineProps) {
  const [text, setText] = useState("");
  const [feedback, setFeedback] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const command = parseCommand(text, knownSymbols);
    setFeedback(command.kind === "unknown" ? command.message : null);
    switch (command.kind) {
      case "unknown":
        return;
      case "select":
        controls.select(command.symbol);
        break;
      case "start":
        controls.start(command.symbols);
        break;
      case "pause":
        controls.pause();
        break;
      case "resume":
        controls.resume();
        break;
      case "stop":
        controls.stop();
        break;
      case "live":
        controls.goLive();
        break;
      case "speed":
        controls.setSpeed(command.barsPerSecond);
        break;
      case "killSwitch":
        controls.setKillSwitch(command.engaged);
        break;
    }
    setText("");
  };

  const message = feedback ?? notice;
  return (
    <form className="panel command-line" onSubmit={submit}>
      <label htmlFor="desk-command" className="prompt">
        DESK&gt;
      </label>
      <input
        id="desk-command"
        type="text"
        autoComplete="off"
        spellCheck={false}
        placeholder="AAPL · START · PAUSE · SPEED 8 · KILL"
        value={text}
        onChange={(event) => setText(event.target.value)}
        aria-describedby={message ? "desk-command-message" : undefined}
      />
      {message && (
        <span id="desk-command-message" className="command-message" role="alert">
          {message}
        </span>
      )}
      <div className="function-keys" aria-hidden="true">
        {AGENTS.map((agent) => (
          <span key={agent.name} className={`tag agent-${agent.name.toLowerCase()}`}>
            {agent.key} {agent.label}
          </span>
        ))}
      </div>
    </form>
  );
}
