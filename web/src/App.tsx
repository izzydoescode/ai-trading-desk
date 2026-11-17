import { AgentCards } from "./components/AgentCards";
import { Balance } from "./components/Balance";
import { ChartPanel } from "./components/ChartPanel";
import { CommandLine } from "./components/CommandLine";
import { DecisionPanel } from "./components/DecisionPanel";
import { Journal, NewsMood, Watchlist } from "./components/Feeds";
import { Floor } from "./components/Floor";
import { Header } from "./components/Header";
import { Pipeline } from "./components/Pipeline";
import { RiskPanel } from "./components/RiskPanel";
import {
  agentCards,
  focusSymbol,
  performance,
  pipelineStages,
  priceLevels,
  watchlistRows,
} from "./derive";
import { useDesk } from "./useDesk";

export function App() {
  const { state, controls } = useDesk();
  const { desk } = state;
  const symbol = focusSymbol(desk, state.selectedSymbol);
  const report = symbol && desk ? desk.reports_by_symbol[symbol] : undefined;
  const cards = agentCards(desk, symbol);

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
      <CommandLine
        knownSymbols={desk?.settings?.symbols ?? []}
        controls={controls}
        notice={state.notice}
      />
      <AgentCards cards={cards} />
      <div className="analysis">
        <ChartPanel
          symbol={symbol}
          candles={(symbol && desk?.candles_by_symbol[symbol]) || []}
          levels={priceLevels(desk, symbol)}
          report={report}
        />
        <div className="decision-column">
          <DecisionPanel report={report} />
          <RiskPanel report={report} />
        </div>
      </div>
      <Floor cards={cards} />
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
        <NewsMood report={report} />
        <Journal events={desk?.recent_events ?? []} />
      </div>
      <Pipeline stages={pipelineStages(report)} />
      <footer className="label small">
        <span>Paper trading only. Replay of historical daily bars. No orders reach a broker.</span>
        <span>Research tool, not investment advice.</span>
      </footer>
    </div>
  );
}
