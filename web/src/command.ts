/** The command line: a few words typed Bloomberg-style, turned into one action. */

export type Command =
  | { kind: "select"; symbol: string }
  | { kind: "start"; symbols: string[] }
  | { kind: "pause" }
  | { kind: "resume" }
  | { kind: "stop" }
  | { kind: "speed"; barsPerSecond: number }
  | { kind: "killSwitch"; engaged: boolean }
  | { kind: "unknown"; message: string };

export const MAXIMUM_BARS_PER_SECOND = 1000;
const SYMBOL_PATTERN = /^[A-Z][A-Z0-9.-]{0,14}$/;
export const COMMAND_HELP =
  "Try a symbol (AAPL), START, PAUSE, RESUME, STOP, SPEED 8, KILL or ARM.";

export function parseCommand(text: string, knownSymbols: string[]): Command {
  const words = text.trim().toUpperCase().split(/\s+/).filter(Boolean);
  const [verb, ...rest] = words;
  if (verb === undefined) return { kind: "unknown", message: COMMAND_HELP };
  switch (verb) {
    case "PAUSE":
      return { kind: "pause" };
    case "RESUME":
      return { kind: "resume" };
    case "STOP":
      return { kind: "stop" };
    case "KILL":
      return { kind: "killSwitch", engaged: true };
    case "ARM":
      return { kind: "killSwitch", engaged: false };
    case "START":
    case "REPLAY": {
      const invalid = rest.find((word) => !SYMBOL_PATTERN.test(word));
      if (invalid) return { kind: "unknown", message: `"${invalid}" is not a ticker symbol.` };
      return { kind: "start", symbols: rest };
    }
    case "SPEED": {
      const barsPerSecond = Number(rest[0]);
      if (!(barsPerSecond > 0 && barsPerSecond <= MAXIMUM_BARS_PER_SECOND)) {
        return {
          kind: "unknown",
          message: `SPEED needs bars per second between 0 and ${MAXIMUM_BARS_PER_SECOND}.`,
        };
      }
      return { kind: "speed", barsPerSecond };
    }
  }
  if (knownSymbols.includes(verb)) return { kind: "select", symbol: verb };
  if (SYMBOL_PATTERN.test(verb)) {
    return {
      kind: "unknown",
      message: `${verb} is not in this replay. START ${verb} begins a new one.`,
    };
  }
  return { kind: "unknown", message: COMMAND_HELP };
}
