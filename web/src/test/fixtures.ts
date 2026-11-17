import finishedSnapshotJson from "./fixtures/finished-snapshot.json";
import messageSampleJson from "./fixtures/message-stream.json";
import reviewJson from "./fixtures/review.json";
import tradeDecisionJson from "./fixtures/trade-decision.json";
import type { DeskReport, ReviewResponse, ServerMessage, Snapshot } from "../types";

/** Real output captured from the desk server replaying the bundled fixture. */
export const finishedSnapshot = finishedSnapshotJson as unknown as Snapshot;
export const messageSample = messageSampleJson as unknown as ServerMessage[];

/** GET /api/review as the server computes it for the fixture's trade and three variants
 *  of it (a stopped loser, a target winner and a stalled short). */
export const reviewSample = reviewJson as unknown as ReviewResponse;
/** The six-agent report the journal kept for the fixture's trade. */
export const tradeDecisionSample = tradeDecisionJson as unknown as DeskReport;

export function cloneSnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return { ...structuredClone(finishedSnapshot), ...overrides };
}

/** The bar on which the desk approved its trade. */
export function tradeReport(): DeskReport {
  const tradeBar = messageSample.find(
    (message) =>
      message.type === "bar" &&
      Object.values(message.reports_by_symbol).some((report) => report.decision.action === "TRADE"),
  );
  if (tradeBar?.type !== "bar") throw new Error("fixture has no trade bar");
  return structuredClone(tradeBar.reports_by_symbol.AAPL!);
}

export function reportWith(change: (report: DeskReport) => void): DeskReport {
  const report = tradeReport();
  change(report);
  return report;
}

/** A status update as the server sends it. */
export function statusMessage(overrides: Partial<Snapshot>): ServerMessage {
  return { ...cloneSnapshot(), ...overrides, type: "status" };
}
