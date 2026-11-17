import type { PipelineStage } from "../derive";

export function Pipeline({ stages }: { stages: PipelineStage[] }) {
  return (
    <nav className="panel pipeline" aria-label="Decision pipeline">
      <span className="label">PIPELINE</span>
      {stages.length === 0 ? (
        <span className="label">waiting for the first decision</span>
      ) : (
        <ol>
          {stages.map((stage) => (
            <li
              key={stage.agent}
              className={`badge tone-${stage.tone}${stage.agent === "DATA" ? "" : ` agent-outline agent-${stage.agent.toLowerCase()}`}`}
            >
              {stage.label}
            </li>
          ))}
        </ol>
      )}
    </nav>
  );
}
