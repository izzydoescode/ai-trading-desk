import { AgentCards } from "./components/AgentCards";
import { Balance } from "./components/Balance";
import { ChartPanel } from "./components/ChartPanel";
import { CommandLine } from "./components/CommandLine";
import { DecisionPanel } from "./components/DecisionPanel";
import { Journal, NewsMood, Watchlist } from "./components/Feeds";
import { Floor } from "./components/Floor";
import { Header } from "./components/Header";
import { Pipeline } from "./components/Pipeline";
import { Review } from "./components/Review";
import { RiskPanel } from "./components/RiskPanel";
import { Tape } from "./components/Tape";
import {
  agentCards,
  focusSymbol,
  performance,
  pipelineStages,
  priceLevels,
  tapeQuotes,
  watchlistRows,
} from "./derive";
import { useDesk } from "./useDesk";
import { useReview } from "./useReview";

export function App() {
  const { state, controls } = useDesk();
  const { desk } = state;
  const symbol = focusSymbol(desk, state.selectedSymbol);
  const report = symbol && desk ? desk.reports_by_symbol[symbol] : undefined;
  const cards = agentCards(desk, symbol);
  const isLive = desk?.mode === "live";
  const tradeReview = useReview(desk?.run_id ?? null, desk?.account?.closed_trades.length ?? 0);

  return (
    <div className="terminal">
      <Header desk={desk} connection={state.connection} controls={controls} />
      {desk?.kill_switch_engaged && (
        <div className="halt-banner" role="alert">
          ■ HALTED · KILL SWITCH ENGAGED · queued orders cancelled, no new ideas until released
        </div>
      )}
      {desk?.status === "FAILED" && (
        <div className="halt-banner" role="alert">
          ✕ REPLAY FAILED · {desk.error ?? "unknown error"} · nothing was traded after the failure
        </div>
      )}
      {isLive && desk?.status === "RUNNING" && desk.error && (
        <div className="halt-banner" role="alert">
          ! LIVE DATA CHECK FAILED · {desk.error} · retrying on the next poll, nothing was traded
        </div>
      )}
      <CommandLine
        knownSymbols={desk?.settings?.symbols ?? []}
        controls={controls}
        notice={state.notice}
      />
      <Tape quotes={tapeQuotes(desk)} focusedSymbol={symbol} onSelect={controls.select} />
      <AgentCards cards={cards} />
      <div className="analysis">
        <ChartPanel
          symbol={symbol}
          candles={(symbol && desk?.candles_by_symbol[symbol]) || []}
          levels={priceLevels(desk, symbol)}
          report={report}
          livePrice={isLive && symbol ? desk?.latest_prices?.[symbol] : null}
          livePriceTime={desk?.prices_as_of}
        />
        <div className="decision-column">
          <DecisionPanel report={report} />
          <RiskPanel report={report} />
        </div>
      </div>
      <Floor cards={cards} working={desk?.status === "RUNNING"} barIndex={desk?.bar_index ?? null} />
      <Balance
        account={desk?.account ?? null}
        equityCurve={desk?.equity_curve ?? []}
        performance={performance(desk?.account ?? null, desk?.equity_curve ?? [])}
      />
      <div className="feeds">
        <Watchlist
          rows={watchlistRows(desk?.reports_by_symbol ?? {})}
          focusedSymbol={symbol}
          onSelect={controls.select}
        />
        <NewsMood report={report} newsSource={desk?.news_source} />
        <Journal events={desk?.recent_events ?? []} />
      </div>
      <Review
        review={tradeReview.review}
        opened={tradeReview.opened}
        onOpenDecision={tradeReview.openDecision}
        onCloseDecision={tradeReview.closeDecision}
      />
      <Pipeline stages={pipelineStages(report)} />
      <footer className="label small">
        <span>
          Paper trading only.{" "}
          {isLive
            ? "Live prices are for display; the desk decides on completed daily sessions."
            : "Replay of historical daily bars."}{" "}
          No orders reach a broker.
        </span>
        <span>Research tool, not investment advice.</span>
      </footer>
    </div>
  );
}
