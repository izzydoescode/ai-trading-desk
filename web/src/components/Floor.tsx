import type { AgentCard } from "../derive";

const MONITOR_BAR_HEIGHTS = [30, 55, 40, 80, 65];

interface FloorProps {
  cards: AgentCard[];
  /** True while a replay is running: the floor is "at work". */
  working: boolean;
  /** Changes once per bar, restarting the hand-off animation. */
  barIndex: number | null;
}

/** One seat per agent. The bubble is what the agent says about the latest bar; the
 *  amber marker on the desk strip is the idea being handed down the line each bar.
 *  Art is original pixel shapes; the codenames are labels only. */
export function Floor({ cards, working, barIndex }: FloorProps) {
  return (
    <section
      className={`panel floor${working ? " floor-working" : ""}`}
      aria-label="Trading floor"
    >
      <div className="panel-head">
        <span className="heading">THE FLOOR</span>
        <span className="label">
          {working ? "at work · each bar passes down the line" : "who said what on the latest bar"}
        </span>
      </div>
      <div className="seats">
        {cards.map((card, seatNumber) => (
          <div
            key={card.name}
            className={`seat agent-${card.name.toLowerCase()} seat-${card.status.toLowerCase()}`}
            style={{ "--seat": seatNumber } as React.CSSProperties}
          >
            <div className="bubble small">
              <span>{card.speech}</span>
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
                    <div
                      key={index}
                      style={{ height: `${height}%`, "--bar": index } as React.CSSProperties}
                    />
                  ))}
                </div>
                <div className="stand" />
              </div>
            </div>
            <div className="desk-strip" aria-hidden="true">
              {working && barIndex !== null && <div key={barIndex} className="hand-off" />}
            </div>
            <div className="seat-name">
              {card.name} <span className={`seat-status tone-${card.tone}`}>{card.status}</span>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
