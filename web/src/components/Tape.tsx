import type { TapeQuote } from "../derive";
import { direction, directionGlyph, price, signedPercent } from "../format";

interface TapeProps {
  quotes: TapeQuote[];
  focusedSymbol: string | null;
  onSelect: (symbol: string) => void;
}

/** The ticker row: last close and change for every symbol in the replay. */
export function Tape({ quotes, focusedSymbol, onSelect }: TapeProps) {
  if (quotes.length === 0) return null;
  return (
    <nav className="panel tape" aria-label="Tape">
      <span className="label">TAPE</span>
      <ul>
        {quotes.map((quote) => {
          const tone = direction(quote.change);
          return (
            <li key={quote.symbol}>
              <button
                type="button"
                className="link"
                aria-pressed={quote.symbol === focusedSymbol}
                onClick={() => onSelect(quote.symbol)}
              >
                {quote.symbol}
              </button>{" "}
              <span>{price(quote.last)}</span>{" "}
              <span className={`tone-${tone === "flat" ? "dim" : tone}`}>
                {directionGlyph(quote.change)} {signedPercent(quote.change)}
              </span>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
