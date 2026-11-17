import { COMMAND_HELP, parseCommand } from "./command";

const KNOWN = ["AAPL", "MSFT"];

describe("command line", () => {
  it.each([
    ["pause", { kind: "pause" }],
    [" Resume ", { kind: "resume" }],
    ["STOP", { kind: "stop" }],
    ["kill", { kind: "killSwitch", engaged: true }],
    ["arm", { kind: "killSwitch", engaged: false }],
    ["start", { kind: "start", symbols: [] }],
    ["replay aapl nvda", { kind: "start", symbols: ["AAPL", "NVDA"] }],
    ["speed 8", { kind: "speed", barsPerSecond: 8 }],
    ["aapl us equity decide", { kind: "select", symbol: "AAPL" }],
    ["msft", { kind: "select", symbol: "MSFT" }],
  ])("%s", (text, expected) => {
    expect(parseCommand(text, KNOWN)).toEqual(expected);
  });

  it.each([
    ["", COMMAND_HELP],
    ["   ", COMMAND_HELP],
    ["$$$", COMMAND_HELP],
    ["speed", "SPEED needs"],
    ["speed 0", "SPEED needs"],
    ["speed fast", "SPEED needs"],
    ["speed 5000", "SPEED needs"],
    ["start aapl 12x", '"12X" is not a ticker symbol.'],
    ["tsla", "TSLA is not in this replay. START TSLA begins a new one."],
  ])("explains what is wrong with %j", (text, message) => {
    const command = parseCommand(text, KNOWN);
    expect(command.kind).toBe("unknown");
    expect(command.kind === "unknown" && command.message).toContain(message);
  });
});
