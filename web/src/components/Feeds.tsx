import type { WatchlistRow } from "../derive";
import { plainReason, price, shortDate } from "../format";
import type { DeskEvent, DeskReport } from "../types";

export const JOURNAL_ROWS = 12;
export const NEWS_ROWS = 4;
const NEWS_TONES = {
  BULLISH: "up",
  BEARISH: "down",
  NEUTRAL: "dim",
  INSUFFICIENT_DATA: "dim",
} as const;
const AGENT_NAMES = ["HAWKEYE", "NEO", "SNOWDEN", "FAMA", "ODIN", "PICARD"];

interface WatchlistProps {
  rows: WatchlistRow[];
  focusedSymbol: string | null;
  onSelect: (symbol: string) => void;
}

export function Watchlist({ rows, focusedSymbol, onSelect }: WatchlistProps) {
  return (
    <section className="panel watchlist" aria-label="Watchlist">
      <div className="title">
        <span className="tag agent-hawkeye">F1 SCOUT</span>
        <span className="label">HAWKEYE · watchlist rank</span>
      </div>
      {rows.length === 0 ? (
        <p className="empty">No symbols ranked yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">SYMBOL</th>
              <th scope="col" className="number">
                SCORE
              </th>
              <th scope="col" className="number">
                VOLUME
              </th>
              <th scope="col" className="number">
                VERDICT
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.symbol} className={row.symbol === focusedSymbol ? "focused" : undefined}>
                <th scope="row">
                  <button
                    type="button"
                    className="link"
                    aria-pressed={row.symbol === focusedSymbol}
                    onClick={() => onSelect(row.symbol)}
                  >
                    {row.symbol}
                  </button>
                  {row.signals.map((signal) => (
                    <span
                      key={signal.label}
                      className={`signal small tone-${signal.tone}`}
                      title={signal.description}
                    >
                      {signal.label}
                    </span>
                  ))}
                </th>
                <td className="number">{row.score === null ? "n/a" : price(row.score)}</td>
                <td className={`number ${row.volumeSurge ? "strong amber" : "label"}`}>
                  {row.relativeVolume === null ? "–" : `${price(row.relativeVolume)}x`}
                  {row.volumeSurge && " ▲ SURGE"}
                </td>
                <td className={`number strong tone-${row.tone}`}>{row.verdict}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

interface NewsMoodProps {
  report: DeskReport | undefined;
  newsSource?: string | null;
}

export function NewsMood({ report, newsSource }: NewsMoodProps) {
  const news = report?.news;
  const sentiment = report?.sentiment;
  const items = news
    ? [
        ...news.verified_facts.map((item) => ({ item, tag: "DATED ITEM", tone: "up" })),
        ...news.commentary.map((item) => ({ item, tag: "COMMENTARY", tone: "caution" })),
      ]
    : [];
  return (
    <section className="panel news" aria-label="News and sentiment">
      <div className="title">
        <span className="tag agent-snowden">F3 NEWS</span>
        <span className="label">SNOWDEN · dated sources only{newsSource ? ` · ${newsSource}` : ""}</span>
        {news && !news.abstain && (
          <span className={`badge tone-${NEWS_TONES[news.verdict]}`}>NEWS {news.verdict}</span>
        )}
      </div>
      {items.slice(0, NEWS_ROWS).map(({ item, tag, tone }) => (
        <div key={`${item.published_at}${item.headline}`} className={`news-item tone-border-${tone}`}>
          <div className="label small">
            {shortDate(item.published_at)} · {item.source} ·{" "}
            <span className={`strong tone-${tone}`}>{tag}</span>
          </div>
          <div>{item.headline}</div>
        </div>
      ))}
      <p className={items.length ? "label small" : "empty"}>
        {items.length > NEWS_ROWS && `+${items.length - NEWS_ROWS} more. `}
        {news?.summary ?? "No news report yet."}
      </p>
      {news?.red_flags.map((flag) => (
        <p key={flag} className="tone-down">
          ! {flag}
        </p>
      ))}
      <div className="title mood-title">
        <span className="tag agent-fama">F4 MOOD</span>
        <span className="label">
          FAMA ·{" "}
          {sentiment?.mood_score == null
            ? "no mood reading"
            : `mood ${sentiment.mood_score} of 100 · hype risk ${sentiment.hype_risk}`}
        </span>
      </div>
      {sentiment?.mood_score != null && (
        <div
          className="mood-bar"
          role="img"
          aria-label={`Mood score ${sentiment.mood_score} out of 100`}
        >
          <div style={{ width: `${sentiment.mood_score}%` }} />
        </div>
      )}
      <p className="label small">{sentiment?.summary ?? "No sentiment report yet."}</p>
    </section>
  );
}

interface JournalLine {
  event: DeskEvent;
  /** How many consecutive bars repeated this same line. */
  repeatCount: number;
}

/** Latest first, with runs of the same line (a position held for weeks) folded into one. */
export function journalLines(events: DeskEvent[], rowLimit = JOURNAL_ROWS): JournalLine[] {
  const lines: JournalLine[] = [];
  for (let index = events.length - 1; index >= 0 && lines.length <= rowLimit; index -= 1) {
    const event = events[index]!;
    const previous = lines.at(-1);
    const repeats =
      previous !== undefined &&
      previous.event.agent === event.agent &&
      previous.event.kind === event.kind &&
      previous.event.symbol === event.symbol &&
      previous.event.reason === event.reason;
    if (repeats) previous.repeatCount += 1;
    else lines.push({ event, repeatCount: 1 });
  }
  return lines.slice(0, rowLimit);
}

export function Journal({ events }: { events: DeskEvent[] }) {
  const lines = journalLines(events);
  return (
    <section className="panel journal" aria-label="Journal">
      <div className="panel-head">
        <span className="heading">JOURNAL</span>
        <span className="label">every signal, skip, veto and fill, with its reason</span>
      </div>
      {lines.length === 0 ? (
        <p className="empty">Nothing recorded yet.</p>
      ) : (
        <table>
          <tbody>
            {lines.map(({ event, repeatCount }, index) => (
              <tr key={`${event.sequence ?? "control"}-${event.timestamp}-${event.kind}-${index}`}>
                <td className="label">{shortDate(event.timestamp)}</td>
                <td
                  className={`strong ${AGENT_NAMES.includes(event.agent) ? `agent-text agent-${event.agent.toLowerCase()}` : ""}`}
                >
                  {event.agent}
                </td>
                <td className="strong">{event.kind}</td>
                <td className="amber">{event.symbol}</td>
                <td className="label reason">
                  {plainReason(event.reason)}
                  {repeatCount > 1 && <span className="repeat"> · {repeatCount} bars</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
