import type { AgentCard } from "../derive";

export function AgentCards({ cards }: { cards: AgentCard[] }) {
  return (
    <section className="agents" aria-label="Agents">
      {cards.map((card) => (
        <article key={card.name} className={`panel agent-card agent-${card.name.toLowerCase()}`}>
          <div className="agent-head">
            <h2>{card.name}</h2>
            <span className={`badge tone-${card.tone}`}>{card.status}</span>
          </div>
          <div className="label small">{card.role}</div>
          <p className="agent-line">{card.line}</p>
          <div className="agent-foot">
            <span className="label small">{card.metricLabel}</span>
            <span className="metric">{card.metricValue}</span>
          </div>
        </article>
      ))}
    </section>
  );
}
