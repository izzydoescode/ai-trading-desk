# Reference repos: synthesis for trading and fintech builds

Reviewed 16 November 2026 from shallow clones of the two forks at their default branches:

- [`izzydoescode/trading-deepagents`](https://github.com/izzydoescode/trading-deepagents) — fork of `Mr-Higgs/trading-deepagents`, MIT. The trading system lives in `trading-agent/`; the rest of the repository is LangChain's Deep Agents monorepo plus a Next.js dashboard in `src/`.
- [`izzydoescode/TradingAgents`](https://github.com/izzydoescode/TradingAgents) — fork of `TauricResearch/TradingAgents`, Apache-2.0, at v0.7.x.

This is a reading of the source, configuration and research notes. Nothing was executed, and no performance figure below was reproduced; the numbers quoted are the ones each repo reports about itself.

## The short version

The two repos sit at opposite ends of one design question: **how much authority does the language model get?**

| | trading-deepagents | TradingAgents |
|---|---|---|
| Market | Forex (AUD/USD), intraday, OANDA practice account | Equities and crypto via Yahoo tickers, daily horizon |
| Who decides | Code. Rules compute the trade; a model may approve or veto | Models. About a dozen agents debate; a model writes the final rating |
| Output | Sized orders with stop and target, filled by a simulator or paper broker | A five-tier rating (Buy … Sell) with prose; no orders, no sizing |
| Repeatable | Yes, byte-for-byte in deterministic mode; cached verdicts otherwise | No; the README says so and explains why |
| Cost per decision | Zero in rule mode; one gate call plus three small analyst calls otherwise | Many calls: four analysts with tool loops, bull/bear debate, trader, three-way risk debate, two managers |
| Backtest | Full execution simulation with spread, slippage, financing, walk-forward and a locked holdout | Decision-quality scoring only: was the rating's direction right versus a benchmark |
| Self-reported result | Its own audit shows both strategies losing money over 2025 | None claimed; the README warns results are not replicable |

For a system that must be cheap to run, explainable to a client and honest in a backtest, **trading-deepagents is the architectural model and TradingAgents is the source of data-handling discipline.** This repository follows that split.

## trading-deepagents

### What it is

`trading-agent/` is a self-contained Python package (pandas, PyYAML, `oandapyV20`) with optional dependency groups for the model gate and the optimizer. Phase 1 is a deterministic Ichimoku system; Phase 2 wraps it with a model gate built on `create_deep_agent`.

### The ideas worth keeping

**1. One loop, two seams.** `engine.run()` is a single ~190-line loop used unchanged by backtest, live simulation and paper trading. Only two injected objects differ: a `MarketDataSource` (historical or live) and an `ExecutionBackend` (simulated or OANDA paper). The loop's clock is each closed bar's `close_time`; it never reads the wall clock. Consequences: a backtest is deterministic, a live run is the same code fed one bar at a time, and there is no separate "backtest version" of the strategy to drift from the real one.

**2. A signal is a proposal, and it is a frozen dataclass.** `Signal` carries entry reference, stop, target, reward-to-risk, a setup id, the strategy version and a features dictionary. Rules and models both emit the same type; everything downstream (risk, execution, logging) consumes it without knowing which produced it. That boundary is what made adding a model a plug-in rather than a rewrite.

**3. The model gate is subtractive.** `AgentStrategy` always lets the inner rule strategy compute the candidate first, and spends no tokens when there is no candidate. In the default `gate` authority the model returns approve or veto; on approve the prices pass through byte-identical. Every failure path (exception, missing structured response, unparseable output, anything that is not "approve") means no trade. A second `signal` authority lets the model modify prices and direction, but the result is re-validated within a sanity band and then goes through the same risk engine as any other signal.

**4. The model path holds no credentials.** The gate agent has no tools; analysts fetch read-only data; broker credentials exist only at two seams guarded by a practice-account tripwire. The model cannot place or alter an order because nothing it can call does that.

**5. Verdict cache.** A verdict is treated as a pure function of (prompt, model, prompt version) and stored append-only under that hash. Re-running a backtest replays stored verdicts at zero cost and restores determinism. Only successes are cached, never errors.

**6. A risk engine with ordered, named rejections.** `RiskEngine.evaluate` checks, cheapest and highest-authority first: kill switch, day halted, max trades per day, max concurrent positions, daily loss breached, stop distance valid and above a minimum, stop and target on the correct side, reward-to-risk floor, minimum units, margin, daily loss budget. Three details are easy to miss and matter:

- Expected trading cost is added to the stop distance and subtracted from the reward *before* sizing and the reward-to-risk check, so a setup that only clears 2:1 on paper is rejected.
- The daily loss cap budgets open stop risk as well as realised loss: a new trade is shrunk until realised loss plus every open stop plus this trade still fits.
- The trading day rolls at the broker's rollover time, not at midnight.

**7. Everything is an event with a reason.** Each signal, skip, rejection, fill, exit and halt is one JSONL line carrying a `reason`. Day state is rebuilt by replaying the log, which makes restarts safe. The same log feeds the journal, the analytics and the dashboard stream, and produces the "why candidates were dropped" table (for example `no_tk_cross: 13119`, `session_closed: 8785`).

**8. Realistic fills.** The simulated backend fills on the next bar, buys at the ask and sells at the bid, widens spread by hour of week from a fitted table, adds stop slippage and accrues overnight financing. The README's own warning applies to any paper system: if results look too good, suspect the simulator first.

**9. Three-stage research with separated powers.** This is the most transferable idea in the repo:

1. *Propose (model).* A research agent reads in-sample analytics and a hypothesis backlog and returns one hypothesis plus a bounded search space. It has no authority to change configuration.
2. *Judge (code).* An Optuna study per walk-forward fold (12-month train, 3-month test) with the model gate forced off. The report ranks parameters by plateau rather than best point and attaches a deflated Sharpe ratio, probability of backtest overfitting and bootstrap intervals.
3. *Final exam (once).* A locked holdout window. Each use is logged *before* the run, and a configuration hash that was already scored is refused unless explicitly overridden, which is also logged.

No single step can both propose and approve a change. The optimizer physically cannot read bars at or after the holdout start.

**10. Measure a component before trusting it.** `gate_compare` runs the deterministic strategy and the gated one over the same window and matches each veto to the trade the rules would have opened, giving the profit or loss the gate avoided or gave up. `news_compare` does the same for news blackout lengths.

**11. Versioned configuration.** All rules live in YAML; every trade is tagged with the strategy version that produced it.

### What its own evidence says

`research/backtest_audit_2025.md` and `research/latest_report.md` are unusually candid. Over 2025 on AUD/USD the breakout strategy took 306 trades with a 51% win rate and still lost: average win 0.34R against average loss 1.11R, profit factor 0.34. After logic fixes and higher-timeframe confluence both strategies lost less but still lost. The lesson is not that the engineering is poor (it is careful) but that **sound machinery does not create an edge**, and that win rate alone says nothing without the size of wins and losses.

### What to avoid or treat with care

- The repository vendors an entire agent framework to use one function. For a new build, depend on the library or write the small gate directly.
- The dashboard is coupled to a LangGraph server and six named graphs; useful as a reference for panels (signals, risk settings, trade management), heavy as a dependency.
- Several configuration fields are declared but never consumed, by the repo's own review (`docs/design.md`).
- Single instrument, single setup family. Correlation and concentration rules are not exercised.
- `docs/trading-system/` contains third-party course PDFs; do not redistribute them from a derived project.

## TradingAgents

### What it is

A LangGraph pipeline modelled on a trading firm: four analysts (market, sentiment, news, fundamentals) run in parallel with tool loops, bull and bear researchers debate, a research manager produces a plan, a trader proposes a transaction, three risk personas debate it, and a portfolio manager issues the final five-tier rating. It supports more than a dozen model providers and ships a rich terminal interface and HTML reports. Roughly ninety test files.

### The ideas worth keeping

**1. Point-in-time integrity as a first-class rule.** `dataflows/date_window.py` centralises it and the changelog shows it was learned the hard way, release after release:

- Any date or window a model asks for is clamped to the run date before it reaches a vendor.
- Dated items (news, social posts) are trimmed to the window with timezone normalisation; an *undated* item is kept only when the window reaches the present, because a backtest cannot prove it is not from the future.
- A feed that only serves recent items reports a historical window as **unavailable, not empty**, so "no news found" is never claimed for a period nobody observed.
- Fundamentals come from SEC EDGAR *as filed on that date*, including pre-restatement values; sources that cannot say when a figure became public are withheld from historical runs.
- A decision is entered at the first close *after* its date, because the run reads news published after that day's close.

Every one of these has a dedicated look-ahead test. This is the single most reusable body of knowledge in the repo.

**2. A vendor router with a behaviour-based error taxonomy.** Three exception types, chosen by how the router must react rather than by cause: `NoMarketDataError` (empty or stale: a real answer), `VendorUnavailableError` (throttled or failed: try the next vendor), `VendorNotConfiguredError` (no key: treat as unavailable). Vendors are tried in a configured order per data category. Optional enrichment categories degrade to a note instead of failing the run.

**3. Model tiers and a provider factory.** A quick tier for analysts and debaters and a deep tier for the managers, each of which can sit on a different provider. Provider modules are imported lazily so that importing the package never requires an SDK or a key. Capabilities (does this model accept temperature, effort, structured output) are looked up rather than assumed.

**4. Structured output where decisions are made.** Only the three decision agents use schemas. Field descriptions carry the instructions, and a validator coerces the placeholders models write into optional numeric fields ("N/A", "15%", "$1,234.50", "around 150") so one bad field is nulled instead of discarding the whole decision. The comment about a "15%" stop being read as $15 on a $600 stock is a good example of why.

**5. Grounding.** The instrument's identity is resolved deterministically from the ticker before any agent runs, and exact price and indicator claims are checked against a verified data snapshot. Both were added after reports of agents analysing the wrong company or inventing price levels.

**6. Memory with settlement.** Each decision is appended to a log. On a later run, decisions whose holding window has passed are scored (raw return and alpha against a regional benchmark) and given a one-paragraph reflection; the portfolio manager then reads recent settled decisions for the same ticker. The memory is itself filtered point-in-time in backtests.

**7. An honest scope for its backtest.** `backtest.py` states that it evaluates decision quality and "is not a portfolio simulator, and must not grow one", because turning a rating into a filled order needs a quantity, a fill price and a cash ledger that the system does not have. It reports hit rate and mean alpha per rating with a caveat that figures are indicative, not repeatable.

**8. Operational polish.** Checkpoint resume per ticker, a portfolio context that distinguishes "no portfolio given" from "flat book", a CI job in a non-UTC timezone to catch date bugs, and a clean-install smoke job to catch undeclared dependencies.

**9. JEV as a pre-filter.** With a TypeSafe key, each social post is asked two typed questions (is it about this instrument; which way does it lean) and code drops off-topic posts and prepends a stance count. If screening fails, posts pass through and the report says so. This is the right shape for a cheap probabilistic model: a filter in front of an expensive reader, with thresholds owned by code.

### What to avoid or treat with care

- **Cost and latency.** One decision involves many model calls, several of them long. Run across a watchlist every day, this is the cost profile that exhausts a usage tier.
- **Non-determinism.** The same ticker and date can produce different ratings. That is acceptable for research and awkward for a system that must explain a trade to a client or be regression-tested.
- **Debate does not guarantee accuracy.** Bull/bear and risk-persona debates produce persuasive prose; the framework's own backtest is the only check, and it is explicitly indicative.
- **No execution, sizing or risk limits in code.** Risk management is a conversation between personas, not an enforced rule set. Nothing stops a rating from being acted on at any size.
- **Free data sources are fragile.** Much of the changelog is repairs for Yahoo rate limits, feed outages and stale bars. Plan for vendor failure from the start.

## Patterns to reuse in any trading or fintech build

1. **Code owns money; models propose or veto.** Put sizing, limits, prices and the kill switch in deterministic code. Give a model the narrowest question that adds value and make every failure mean "do nothing".
2. **One engine, injected edges.** Make data source and execution venue the only things that change between backtest, replay, paper and live. Clock the loop on data, never on wall time.
3. **Point-in-time or it did not happen.** Clamp every query to the decision time, distinguish "unavailable" from "empty", drop undated items in historical runs, and fill at the next bar. Write a look-ahead test for every data path.
4. **Typed proposals.** A frozen `Signal` (or equivalent) between idea and order lets rules and models be swapped without touching risk or execution.
5. **Reason codes everywhere.** One append-only event per decision, including the non-decisions. Skips are data: they show where the funnel loses candidates.
6. **Costs inside the risk check.** Count spread, slippage and fees in reward-to-risk and sizing, and budget open risk against the daily loss cap.
7. **Separate propose, judge and final exam.** Let a model suggest; let code score out-of-sample; keep a holdout that is used once and logged before use.
8. **Cache model output by input hash.** It makes re-runs free and tests deterministic.
9. **Tier your models.** Cheap and fast for extraction and filtering, stronger only where judgement is actually exercised, each tier independently configurable.
10. **Degrade loudly.** A failed vendor or screen should leave a visible note in the output, never a silent fallback that looks like success.
11. **Measure each component in isolation.** A/B the system with and without the model gate, with and without the news filter, before believing either helps.
12. **Report the unflattering numbers.** Expectancy, profit factor, average win and loss in R, drawdown, and results after costs. A win rate on its own is not evidence.

## How this repository applies them

| Pattern | Where it lands here |
|---|---|
| Code owns money | ODIN's checks are rules; PICARD cannot choose TRADE without ODIN's approval; models may explain, never overturn |
| One engine, injected edges | `desk.py` loop with `MarketDataProvider` and `ExecutionVenue` protocols; replay, backtest and live polling share it |
| Point-in-time | Existing `as_of` filter in `graph.py`, extended to news and sentiment inputs for SNOWDEN and FAMA |
| Typed proposals | `TradePlan` in `models.py`, alongside the existing `Analysis` and `Evidence` contracts |
| Reason codes | Desk event stream, persisted with the journal and pushed to the terminal UI |
| Costs in risk | ODIN's reward-to-risk and sizing use the market profile's fee and slippage model |
| Propose / judge / exam | In-sample and out-of-sample split with a locked holdout in `backtest/`; optimizer deferred |
| Output cache | Response cache in `llm.py` keyed by role, model and input hash |
| Model tiers | Quick and deep tiers across the Claude and OpenAI providers |
| Degrade loudly | Existing abstention and `limitations` fields; vendor errors typed as unavailable, no data or not configured |

Deferred until after the first demo: walk-forward optimizer with overfitting statistics, decision memory with settlement, gate A/B tooling, fundamentals and macro analysts, JEV pre-screening of social posts.

## Licence notes

Both forks are permissively licensed (MIT and Apache-2.0), so patterns and code may be reused with attribution; Apache-2.0 additionally requires preserving notices and stating changes. This repository reuses ideas only, not source. The course PDFs under `trading-deepagents/docs/trading-system/` are third-party material and are not covered by that repo's code licence.
