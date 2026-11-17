import { STRATEGY_WORDS } from "../derive";
import { money, percent, plainReason, price, shortDate, signedMoney } from "../format";
import type { DeskReport, GroupResult, ReviewedTrade, TradeReview } from "../types";
import type { OpenedDecision } from "../useReview";

const PLOT_WIDTH = 360;
const PLOT_HEIGHT = 220;
const PLOT_MARGIN = 30;

/** "+1.25R", or a dash when there is nothing to average. */
export function signedR(value: number | null | undefined): string {
  if (value == null) return "–";
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}R`;
}

interface ReviewProps {
  review: TradeReview | null;
  opened: OpenedDecision | null;
  onOpenDecision: (tradeNumber: number) => void;
  onCloseDecision: () => void;
}

/** Past trades: what worked, what did not, and the saved reasoning behind each one. */
export function Review({ review, opened, onOpenDecision, onCloseDecision }: ReviewProps) {
  const hasTrades = review !== null && review.trade_count > 0;
  return (
    <section className="panel review" aria-label="Trade review">
      <div className="panel-head">
        <div className="title">
          <span className="tag">F9 REVIEW</span>
          <span className="label">closed paper trades of this run</span>
        </div>
        {hasTrades && (
          <span className={`strong tone-${review.profit_and_loss >= 0 ? "up" : "down"}`}>
            {review.win_count}W / {review.loss_count}L · {signedMoney(review.profit_and_loss)}
          </span>
        )}
      </div>
      {!hasTrades ? (
        <p className="empty">No closed trades yet. Results appear here as trades close.</p>
      ) : (
        <>
          <dl className="tiles tiles-4">
            {summaryTiles(review).map(([name, value]) => (
              <div className="tile" key={name}>
                <dt>{name}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          <div className="review-grid">
            <Breakdown heading="BY STRATEGY" groups={review.by_strategy} labelOf={strategyWord} />
            <Breakdown heading="BY EXIT" groups={review.by_exit_reason} labelOf={plainReason} />
            <Breakdown heading="BY SYMBOL" groups={review.by_symbol} />
            <Breakdown heading="BY HOLDING TIME" groups={review.by_holding_time} />
            {review.by_side.length > 1 && <Breakdown heading="BY SIDE" groups={review.by_side} />}
            <ExcursionPlot trades={review.trades} />
          </div>
          <TradesTable trades={review.trades} opened={opened} onOpenDecision={onOpenDecision} />
          {opened && <DecisionDetail opened={opened} onClose={onCloseDecision} />}
        </>
      )}
    </section>
  );
}

function strategyWord(label: string): string {
  return STRATEGY_WORDS[label as keyof typeof STRATEGY_WORDS] ?? label;
}

/** "$1,780.67 · +2.36R", or one dash when there were no such trades. */
function amountAndR(amount: number | null, rMultiple: number | null): string {
  return amount == null ? "–" : `${money(amount)} · ${signedR(rMultiple)}`;
}

function summaryTiles(review: TradeReview): [string, string][] {
  return [
    ["TRADES", String(review.trade_count)],
    ["WIN RATE", percent(review.win_rate, 0)],
    ["AVG WIN", amountAndR(review.average_win_amount, review.average_win_r)],
    ["AVG LOSS", amountAndR(review.average_loss_amount, review.average_loss_r)],
    ["AVG RESULT / TRADE", signedR(review.expectancy_r)],
    ["BIGGEST WIN", money(review.largest_win?.profit_and_loss)],
    ["BIGGEST LOSS", money(review.largest_loss?.profit_and_loss)],
    [
      "AVG SESSIONS HELD",
      review.average_bars_held == null ? "–" : review.average_bars_held.toFixed(1),
    ],
  ];
}

interface BreakdownProps {
  heading: string;
  groups: GroupResult[];
  labelOf?: (label: string) => string;
}

function Breakdown({ heading, groups, labelOf = (label) => label }: BreakdownProps) {
  return (
    <table className="breakdown">
      <caption className="label">{heading}</caption>
      <thead>
        <tr>
          <th scope="col"></th>
          <th scope="col" className="number">
            TRADES
          </th>
          <th scope="col" className="number">
            WIN RATE
          </th>
          <th scope="col" className="number">
            P&amp;L
          </th>
          <th scope="col" className="number">
            AVG R
          </th>
        </tr>
      </thead>
      <tbody>
        {groups.map((group) => (
          <tr key={group.label}>
            <th scope="row">{labelOf(group.label)}</th>
            <td className="number">{group.trade_count}</td>
            <td className="number">{percent(group.win_rate, 0)}</td>
            <td className={`number tone-${group.profit_and_loss >= 0 ? "up" : "down"}`}>
              {signedMoney(group.profit_and_loss)}
            </td>
            <td className="number">{signedR(group.average_r)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Each trade as a point: how far it went against the desk (across) and for it (up)
 *  while it was open. Winners are ▲, losers ▼, so colour is never the only signal. */
function ExcursionPlot({ trades }: { trades: ReviewedTrade[] }) {
  const worst = Math.min(-1, ...trades.map(({ trade }) => trade.maximum_adverse_excursion_r));
  const best = Math.max(1, ...trades.map(({ trade }) => trade.maximum_favorable_excursion_r));
  const toX = (adverse: number) =>
    PLOT_MARGIN + ((adverse - worst) / -worst) * (PLOT_WIDTH - PLOT_MARGIN - 8);
  const toY = (favorable: number) =>
    PLOT_HEIGHT - PLOT_MARGIN - (favorable / best) * (PLOT_HEIGHT - PLOT_MARGIN - 12);
  return (
    <figure className="excursions">
      <figcaption className="label">
        HOW FAR EACH TRADE WENT · across: worst point against (R) · up: best point for (R)
      </figcaption>
      <svg
        viewBox={`0 0 ${PLOT_WIDTH} ${PLOT_HEIGHT}`}
        role="img"
        aria-label={`Best and worst open result of ${trades.length} closed trades`}
      >
        <line className="axis" x1={PLOT_MARGIN} y1={toY(0)} x2={PLOT_WIDTH - 8} y2={toY(0)} />
        <line className="axis" x1={toX(0)} y1={12} x2={toX(0)} y2={toY(0)} />
        <text className="axis-label" x={PLOT_MARGIN} y={PLOT_HEIGHT - 8}>
          {worst.toFixed(1)}R
        </text>
        <text className="axis-label" x={toX(0)} y={PLOT_HEIGHT - 8} textAnchor="end">
          0
        </text>
        <text className="axis-label" x={PLOT_MARGIN - 4} y={18} textAnchor="end">
          {best.toFixed(1)}R
        </text>
        {trades.map(({ trade_number, trade }) => {
          const won = trade.profit_and_loss > 0;
          return (
            <text
              key={trade_number}
              className={`point tone-${won ? "up" : "down"}`}
              x={toX(trade.maximum_adverse_excursion_r)}
              y={toY(trade.maximum_favorable_excursion_r)}
              textAnchor="middle"
              dominantBaseline="central"
            >
              {won ? "▲" : "▼"}
              <title>
                {`#${trade_number + 1} ${trade.symbol} ${signedR(trade.r_multiple)} · best ${signedR(
                  trade.maximum_favorable_excursion_r,
                )} · worst ${signedR(trade.maximum_adverse_excursion_r)}`}
              </title>
            </text>
          );
        })}
      </svg>
    </figure>
  );
}

interface TradesTableProps {
  trades: ReviewedTrade[];
  opened: OpenedDecision | null;
  onOpenDecision: (tradeNumber: number) => void;
}

function TradesTable({ trades, opened, onOpenDecision }: TradesTableProps) {
  return (
    <table className="review-trades">
      <caption className="label">EVERY CLOSED TRADE · newest first</caption>
      <thead>
        <tr>
          <th scope="col">#</th>
          <th scope="col">SYMBOL</th>
          <th scope="col">SIDE</th>
          <th scope="col">STRATEGY</th>
          <th scope="col">OPENED</th>
          <th scope="col">CLOSED</th>
          <th scope="col" className="number">
            SESSIONS
          </th>
          <th scope="col">EXIT</th>
          <th scope="col" className="number">
            P&amp;L
          </th>
          <th scope="col" className="number">
            R
          </th>
          <th scope="col" className="number">
            BEST
          </th>
          <th scope="col" className="number">
            WORST
          </th>
          <th scope="col" className="number">
            WHY
          </th>
        </tr>
      </thead>
      <tbody>
        {[...trades].reverse().map(({ trade_number, trade, has_saved_decision }) => (
          <tr key={trade_number} className={opened?.tradeNumber === trade_number ? "focused" : ""}>
            <th scope="row">{trade_number + 1}</th>
            <td className="amber">{trade.symbol}</td>
            <td>{trade.side}</td>
            <td>{strategyWord(trade.strategy)}</td>
            <td>{shortDate(trade.opened_at)}</td>
            <td>{shortDate(trade.closed_at)}</td>
            <td className="number">{trade.bars_held}</td>
            <td>{plainReason(trade.exit_reason)}</td>
            <td className={`number strong tone-${trade.profit_and_loss > 0 ? "up" : "down"}`}>
              {trade.profit_and_loss > 0 ? "▲" : "▼"} {signedMoney(trade.profit_and_loss)}
            </td>
            <td className="number">{signedR(trade.r_multiple)}</td>
            <td className="number">{signedR(trade.maximum_favorable_excursion_r)}</td>
            <td className="number">{signedR(trade.maximum_adverse_excursion_r)}</td>
            <td className="number">
              {has_saved_decision ? (
                <button
                  type="button"
                  aria-label={`Why trade ${trade_number + 1} was taken`}
                  aria-expanded={opened?.tradeNumber === trade_number}
                  onClick={() => onOpenDecision(trade_number)}
                >
                  WHY
                </button>
              ) : (
                <span className="label" title="No decision report was saved for this trade">
                  –
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DecisionDetail({ opened, onClose }: { opened: OpenedDecision; onClose: () => void }) {
  return (
    <div className="decision-detail" role="region" aria-label="Saved decision">
      <div className="panel-head">
        <span className="heading">WHY TRADE {opened.tradeNumber + 1} WAS TAKEN</span>
        <button type="button" onClick={onClose}>
          CLOSE
        </button>
      </div>
      {opened.error && (
        <p className="tone-down" role="alert">
          {opened.error}
        </p>
      )}
      {!opened.error && !opened.report && <p className="empty">Loading the saved report…</p>}
      {opened.report && <SavedDecision report={opened.report} />}
    </div>
  );
}

function SavedDecision({ report }: { report: DeskReport }) {
  const plan = report.decision.plan;
  const views: [string, string, string][] = [
    ["HAWKEYE", "agent-hawkeye", `${report.scout.verdict}. ${report.scout.reasons.join(" ")}`],
    ["NEO", "agent-neo", report.chart.thesis.summary],
    ["NEO counter-case", "agent-neo", report.chart.counter_case.summary],
    ["SNOWDEN", "agent-snowden", report.news.summary],
    ["FAMA", "agent-fama", report.sentiment.summary],
    ["PICARD", "agent-picard", report.decision.summary],
  ];
  return (
    <>
      <p className="label">
        Decided on {shortDate(report.evaluated_as_of)} at a close of{" "}
        {price(report.indicators.close)} · SMA20 {price(report.indicators.sma20)} · SMA50{" "}
        {price(report.indicators.sma50)} · RSI14 {price(report.indicators.rsi14, 1)} · ATR14{" "}
        {price(report.indicators.atr14)}
      </p>
      {plan && (
        <dl className="tiles tiles-4">
          {(
            [
              ["PLANNED ENTRY", price(plan.entry_reference)],
              ["STOP", price(plan.stop)],
              ["TARGET", plan.target_follows_band ? `${price(plan.target)} · follows band` : price(plan.target)],
              ["RISKED", `${money(plan.risk_amount)} · ${plan.reward_to_risk.toFixed(2)} : 1`],
            ] as [string, string][]
          ).map(([name, value]) => (
            <div className="tile" key={name}>
              <dt>{name}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <ul className="views">
        {views.map(([agent, agentClass, text]) => (
          <li key={agent}>
            <span className={`strong agent-text ${agentClass}`}>{agent}</span> {text}
          </li>
        ))}
      </ul>
      {report.decision.dissent.length > 0 && (
        <p className="tone-caution">Dissent kept: {report.decision.dissent.join(" · ")}</p>
      )}
      <p className="label">
        ODIN {report.risk.verdict}:{" "}
        {report.risk.checks
          .map((check) => `${check.passed ? "✓" : "✕"} ${check.name} (${check.actual})`)
          .join(" · ")}
      </p>
    </>
  );
}
