import type { Performance } from "../derive";
import { directionGlyph, direction, money, percent, price, signedMoney, signedPercent } from "../format";
import type { Account, EquityPoint } from "../types";

const WIDTH = 640;
const HEIGHT = 120;

interface BalanceProps {
  account: Account | null;
  equityCurve: EquityPoint[];
  performance: Performance;
}

export function Balance({ account, equityCurve, performance }: BalanceProps) {
  const equities = equityCurve.map((point) => point.equity);
  const lowest = Math.min(...equities, account?.starting_cash ?? Infinity);
  const highest = Math.max(...equities, account?.starting_cash ?? -Infinity);
  const range = highest - lowest || 1;
  const points = equities
    .map((equity, index) => {
      const x = equities.length > 1 ? (index / (equities.length - 1)) * WIDTH : 0;
      const y = HEIGHT - 8 - ((equity - lowest) / range) * (HEIGHT - 16);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const tone = direction(performance.profitAndLoss);
  const rMultiple = (value: number | null) => (value === null ? "–" : `${price(value, 1)}R`);
  const stats: [string, string][] = [
    ["TRADES", String(performance.tradeCount)],
    ["WIN RATE", percent(performance.winRate, 0)],
    ["PROFIT FACTOR", price(performance.profitFactor)],
    ["AVG WIN / LOSS (R)", `${rMultiple(performance.averageWinR)} / ${rMultiple(performance.averageLossR)}`],
    ["AVG WIN $", money(performance.averageWinAmount)],
    ["AVG LOSS $", money(performance.averageLossAmount)],
    ["MAX DRAWDOWN", percent(performance.maximumDrawdown)],
    ["EXPOSURE", percent(performance.exposure, 0)],
  ];
  const position = account?.positions[0];

  return (
    <section className="panel balance" aria-label="Balance">
      <div className="balance-figure">
        <span className="label">PAPER BALANCE</span>
        <span className="figure">{money(account?.equity)}</span>
        <span className={`strong tone-${tone === "flat" ? "dim" : tone}`}>
          {directionGlyph(performance.profitAndLoss)} {signedMoney(performance.profitAndLoss)} ·{" "}
          {signedPercent(performance.totalReturn)} since start
        </span>
        <span className="label">
          {position
            ? `OPEN ${position.symbol} · ${position.units} sh @ ${price(position.entry_price)} · stop ${price(position.stop)}`
            : account?.pending_symbols.length
              ? `QUEUED ${account.pending_symbols.join(", ")} for next open`
              : "NO OPEN POSITION"}
        </span>
      </div>
      {equities.length > 1 ? (
        <svg
          className="equity-curve"
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Paper equity from ${money(equities[0])} to ${money(equities.at(-1))} over ${equities.length} bars`}
        >
          <polyline points={points} />
        </svg>
      ) : (
        <p className="empty equity-curve">Equity curve appears once bars arrive.</p>
      )}
      <dl className="tiles tiles-4">
        {stats.map(([label, value]) => (
          <div key={label} className="tile">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
