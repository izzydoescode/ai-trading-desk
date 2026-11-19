import { STRATEGY_WORDS, type PriceLevel } from "../derive";
import { clockTime, directionGlyph, price, shortDate, signedPercent } from "../format";
import type { Candle, DeskReport } from "../types";

export const VISIBLE_CANDLES = 80;
const TREND_TONES = {
  BULLISH: "up",
  BEARISH: "down",
  NEUTRAL: "dim",
  INSUFFICIENT_DATA: "dim",
} as const;
const WIDTH = 900;
const HEIGHT = 420;
const LABEL_GUTTER = 118;
const PLOT_WIDTH = WIDTH - LABEL_GUTTER;

interface ChartPanelProps {
  symbol: string | null;
  candles: Candle[];
  levels: PriceLevel[];
  report: DeskReport | undefined;
  /** Latest traded price while polling live; absent in a replay. */
  livePrice?: number | null;
  livePriceTime?: string | null;
}

export function ChartPanel({
  symbol,
  candles,
  levels,
  report,
  livePrice = null,
  livePriceTime = null,
}: ChartPanelProps) {
  const visible = candles.slice(-VISIBLE_CANDLES);
  const indicators = report?.indicators;
  const lastClose = visible.at(-1)?.close ?? null;

  const prices = [
    ...visible.flatMap((candle) => [candle.high, candle.low]),
    ...levels.map((level) => level.price),
  ];
  const lowest = Math.min(...prices);
  const highest = Math.max(...prices);
  const padding = (highest - lowest || 1) * 0.06;
  const toY = (value: number) =>
    ((highest + padding - value) / (highest - lowest + 2 * padding)) * HEIGHT;
  const step = PLOT_WIDTH / VISIBLE_CANDLES;
  const bodyWidth = Math.max(2, step * 0.6);

  return (
    <section className="panel chart" aria-label="Chart">
      <div className="panel-head chart-head">
        <div className="title">
          <span className="tag agent-neo">F2 CHART</span>
          <span className="symbol">{symbol ?? "–"}</span>
          <span className="label">daily bars</span>
          {report && (
            <span className={`badge tone-${TREND_TONES[report.chart.trend]}`}>
              TREND {report.chart.trend === "INSUFFICIENT_DATA" ? "UNKNOWN" : report.chart.trend}
            </span>
          )}
          {report?.chart.setup && (
            <span className="badge tone-up" title={report.chart.strategy_notes.join("\n")}>
              {STRATEGY_WORDS[report.chart.setup.strategy]} SETUP
            </span>
          )}
        </div>
        <div className="readings">
          {livePrice != null && (
            <span title="Latest traded price, for display. The desk decides on closed sessions only.">
              <span className="label">LIVE </span>
              <strong className="amber">{price(livePrice)}</strong>
              <span className="label"> {clockTime(livePriceTime)}</span>
            </span>
          )}
          <span>
            <span className="label">{livePrice != null ? "CLOSE " : "LAST "}</span>
            <strong className="amber">{price(lastClose)}</strong>
          </span>
          <span className={`tone-${indicators?.return20_pct == null ? "dim" : indicators.return20_pct >= 0 ? "up" : "down"}`}>
            {directionGlyph(indicators?.return20_pct)}{" "}
            {signedPercent(indicators?.return20_pct == null ? null : indicators.return20_pct / 100)} 20D
          </span>
          <span className="label">SMA20 {price(indicators?.sma20)}</span>
          <span className="label">SMA50 {price(indicators?.sma50)}</span>
          <span className="label">RSI14 {price(indicators?.rsi14, 1)}</span>
          <span className="label">ATR14 {price(indicators?.atr14)}</span>
        </div>
      </div>
      {visible.length === 0 ? (
        <p className="empty">No bars yet. Start a replay to see {symbol ?? "the desk"} trade.</p>
      ) : (
        <>
          <svg
            className="plot"
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            role="img"
            aria-label={`${symbol} daily candles, last close ${price(lastClose)}${levels
              .map((level) => `, ${level.label.toLowerCase()} ${price(level.price)}`)
              .join("")}`}
          >
            <line className="axis" x1="0" y1="0" x2="0" y2={HEIGHT} />
            <line className="axis" x1="0" y1={HEIGHT} x2={PLOT_WIDTH} y2={HEIGHT} />
            {visible.map((candle, index) => {
              const x = index * step + step / 2;
              const rising = candle.close >= candle.open;
              const bodyTop = toY(Math.max(candle.open, candle.close));
              const bodyHeight = Math.max(1.5, Math.abs(toY(candle.open) - toY(candle.close)));
              return (
                <g key={candle.timestamp} className={rising ? "candle-up" : "candle-down"}>
                  <line x1={x} x2={x} y1={toY(candle.high)} y2={toY(candle.low)} />
                  <rect x={x - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} />
                </g>
              );
            })}
            {levels.map((level) => (
              <g key={level.label} className={`level tone-${level.tone}`}>
                <line x1="0" x2={PLOT_WIDTH} y1={toY(level.price)} y2={toY(level.price)} />
                <rect x={PLOT_WIDTH} y={toY(level.price) - 10} width={LABEL_GUTTER} height="20" />
                <text x={PLOT_WIDTH + 6} y={toY(level.price) + 4}>
                  {level.label} {price(level.price)}
                </text>
              </g>
            ))}
          </svg>
          <div className="date-axis label small">
            <span>{shortDate(visible[0]?.timestamp)}</span>
            <span>{shortDate(visible[Math.floor(visible.length / 2)]?.timestamp)}</span>
            <span>{shortDate(visible.at(-1)?.timestamp)}</span>
          </div>
        </>
      )}
    </section>
  );
}
