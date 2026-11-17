import { money, price, units } from "../format";
import type { DeskReport } from "../types";

const ACTION_TONES = { TRADE: "caution", WAIT: "dim", REJECT: "down" } as const;

export function DecisionPanel({ report }: { report: DeskReport | undefined }) {
  if (report === undefined) {
    return (
      <section className="panel decision" aria-label="Decision">
        <PanelTitle />
        <p className="empty">No decision yet.</p>
      </section>
    );
  }
  const { decision } = report;
  const plan = decision.plan;
  const tiles: [string, string, string?][] = plan
    ? [
        ["SIDE", plan.side, "up"],
        ["SIZE", `${units(plan.units)} SH`],
        ["ENTRY", price(plan.entry_reference), "amber"],
        ["NOTIONAL", money(plan.notional)],
        ["STOP", price(plan.stop), "down"],
        ["TARGET", price(plan.target), "up"],
        ["MAX LOSS", money(plan.risk_amount), "down"],
        ["REWARD:RISK", price(plan.reward_to_risk)],
      ]
    : [];
  return (
    <section
      className={`panel decision decision-${decision.action.toLowerCase()}`}
      aria-label="Decision"
    >
      <div className="panel-head">
        <PanelTitle />
        <span className={`verdict tone-${ACTION_TONES[decision.action]}`}>{decision.action}</span>
      </div>
      {tiles.length > 0 && (
        <dl className="tiles tiles-4">
          {tiles.map(([label, value, tone]) => (
            <div key={label} className="tile">
              <dt>{label}</dt>
              <dd className={tone ? `tone-${tone}` : undefined}>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <p>{decision.summary}</p>
      {decision.required_confirmation && (
        <p className="label">Needs: {decision.required_confirmation}</p>
      )}
      {decision.failed_rule && <p className="tone-down">Failed rule: {decision.failed_rule}</p>}
      {report.model_review && (
        <p className={report.model_review.approve ? "label" : "tone-down"}>
          Model review {report.model_review.approve ? "approved" : "vetoed"}:{" "}
          {report.model_review.rationale}
        </p>
      )}
      {decision.dissent.length > 0 && (
        <ul className="dissent label small" aria-label="Dissent kept on record">
          {decision.dissent.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PanelTitle() {
  return (
    <div className="title">
      <span className="tag agent-picard">F6 DECIDE</span>
      <span className="label">PICARD · final call</span>
    </div>
  );
}
