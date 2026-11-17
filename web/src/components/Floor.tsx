import type { AgentCard } from "../derive";

const MONITOR_BAR_HEIGHTS = [30, 55, 40, 80, 65];

/** One seat per agent. The bubble is the agent's latest line; art is original pixel shapes. */
export function Floor({ cards }: { cards: AgentCard[] }) {
  return (
    <section className="panel floor" aria-label="Trading floor">
      <div className="panel-head">
        <span className="heading">THE FLOOR</span>
        <span className="label">who is working · speech is each agent's latest line</span>
      </div>
      <div className="seats">
        {cards.map((card) => (
          <div
            key={card.name}
            className={`seat agent-${card.name.toLowerCase()} seat-${card.status.toLowerCase()}`}
          >
            <div className="bubble small">
              <span>{card.line}</span>
            </div>
            <div className="scene" aria-hidden="true">
              <div className="character">
                <div className="hat" />
                <div className="body">
                  <div className="eye" />
                  <div className="eye" />
                </div>
                <div className="legs">
                  <div />
                  <div />
                </div>
              </div>
              <div className="monitor">
                <div className="screen">
                  {MONITOR_BAR_HEIGHTS.map((height, index) => (
                    <div key={index} style={{ height: `${height}%` }} />
                  ))}
                </div>
                <div className="stand" />
              </div>
            </div>
            <div className="desk-strip" aria-hidden="true" />
            <div className="seat-name">{card.name}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
