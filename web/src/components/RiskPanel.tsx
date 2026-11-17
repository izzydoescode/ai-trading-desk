import type { DeskReport } from "../types";

const VERDICT_TONES = { APPROVE: "up", REVISE: "caution", REJECT: "down" } as const;

export function RiskPanel({ report }: { report: DeskReport | undefined }) {
  const checks = report?.risk.checks ?? [];
  const passed = checks.filter((check) => check.passed).length;
  return (
    <section className="panel risk" aria-label="Risk checks">
      <div className="panel-head">
        <div className="title">
          <span className="tag agent-odin">F5 RISK</span>
          <span className="label">ODIN · veto is final</span>
        </div>
        {report && (
          <span className={`strong tone-${VERDICT_TONES[report.risk.verdict]}`}>
            {report.risk.verdict} {passed}/{checks.length}
          </span>
        )}
      </div>
      {checks.length === 0 ? (
        <p className="empty">No risk review yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">CHECK</th>
              <th scope="col" className="number">
                LIMIT
              </th>
              <th scope="col" className="number">
                THIS TRADE
              </th>
              <th scope="col" className="number">
                RESULT
              </th>
            </tr>
          </thead>
          <tbody>
            {checks.map((check) => (
              <tr key={check.name} className={check.passed ? undefined : "failed"}>
                <th scope="row">{check.name}</th>
                <td className="number label">{check.limit}</td>
                <td className="number amber">{check.actual}</td>
                <td className={`number strong tone-${check.passed ? "up" : "down"}`}>
                  {check.passed ? "✓ PASS" : "✕ FAIL"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {report?.risk.revision && <p className="tone-caution">{report.risk.revision}</p>}
    </section>
  );
}
