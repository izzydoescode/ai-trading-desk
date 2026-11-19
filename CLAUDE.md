# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A research-only Python market-intelligence workflow (LangGraph + Pydantic + httpx). It validates daily OHLCV bars, computes indicators, forms a technical thesis, challenges it with an independent contrarian review, and emits a typed JSON report. It never places orders or touches brokerage accounts — keep it that way.

## Git workflow (mandatory)

- **Never commit to `main` or `master`.** All work happens on a branch and lands through a pull request. `main` is branch-protected.
- **One scope per branch and per PR**, named by type: `feat/…`, `fix/…`, `chore/…`, `docs/…`, `test/…`, `refactor/…`. A fix found while building a feature gets its own branch and PR.
- **Atomic commits.** Each commit is one logical change that passes tests on its own (a module together with its tests). Never one large end-of-branch commit, never unrelated changes in one commit.
- **Conventional Commits** on every commit and PR title: `type(scope): imperative summary`, a body explaining why when it is not obvious, and a `BREAKING CHANGE:` footer when a contract changes. Examples:
  - `feat(agents): add ODIN risk checks with daily loss cap`
  - `test(agents): cover ODIN veto when reward-to-risk is below floor`
  - `fix(paper): fill stop orders at bar open on gap-through`
  - `chore(ci): enforce 80 percent coverage gate`
- **Tests:** coverage must stay at or above 80% (enforced in CI). Cover new behaviour at the unit, integration and end-to-end layers; no test may touch the network.
- **Tests explain themselves.** Every test asserts something and has a docstring saying what it verifies. Helpers in test files are named `make_*`/`fake_*` or for the action they perform, and their docstring starts with `Test helper`, `Test fixture` or `Test double`. Every function or class longer than a dozen lines has a docstring that says what it does. `tests/test_suite_quality.py` enforces all of this.
- **Names:** use descriptive variable, constant and function names in new code. Rename existing short names only in files already being changed.
- SSH pushes may be refused on this machine; push over HTTPS with the `gh` credential helper:
  `git -c credential.helper= -c credential.helper='!gh auth git-credential' push https://github.com/izzydoescode/ai-trading-desk.git <branch>`

## Agent codenames

The desk's six roles use these names everywhere (code, UI, docs, commits): **HAWKEYE** (market scout), **NEO** (chart setup), **SNOWDEN** (news), **FAMA** (sentiment), **ODIN** (risk manager, holds the veto), **PICARD** (head trader, final decision). Agent art is original; the names are labels only.

## Commands

Uses [uv](https://docs.astral.sh/uv/) with a committed `uv.lock`; Python 3.11+.

```bash
uv sync --locked                      # install (runtime + dev group)
uv run pytest -q                      # all tests
uv run pytest tests/test_graph.py::test_no_lookahead -q   # single test
uv run ruff check .                   # lint (E, F, I, UP, B; line length 100)
uv run ruff format --check .          # format check (drop --check to apply)

# Run the CLI (deterministic rules over bundled synthetic candles; no API key needed)
uv run trading-desk analyze AAPL --as-of 2025-04-23T22:00:00Z
uv run trading-desk analyze AAPL --scenario conflict --as-of 2025-04-23T22:00:00Z
uv run trading-desk analyze AAPL --scenario missing  --as-of 2025-04-23T22:00:00Z

# Local API server for the terminal (replay of the bundled fixture, paper venue only)
uv run trading-desk desk serve            # http://127.0.0.1:8000, see /docs for the endpoints
```

```bash
# Terminal UI (web/): React + TypeScript + Vite
npm --prefix web ci
npm --prefix web run dev            # http://127.0.0.1:5173, proxies /api and /ws to desk serve on :8000
npm --prefix web run typecheck
npm --prefix web run test:coverage  # Vitest, 80% gate on lines, branches, functions, statements
npm --prefix web run build          # writes web/dist; `desk serve` then serves it at /
npm --prefix web run e2e            # Playwright: starts `desk serve` itself on :8137 (build first)
```

`.github/workflows/web.yml` runs `npm ci`, typecheck, tests with coverage, build and `npm audit` for `web/`. The audit blocks on shipped dependencies only (`--omit=dev`); build and test tools are audited in a second, report-only step, because the picomatch advisory GHSA-fc5q-39xc-vjpf has no patched release yet. Make that step blocking again when one exists.

CI (`.github/workflows/ci.yml`) runs exactly: `uv sync --locked`, pytest, `ruff check`, `ruff format --check`, and the `missing` scenario smoke run, on Python 3.11–3.14. Run all four before considering a change done.

Things that look like bugs but are not:

- The bundled fixture ends 2025-04-23. Without `--as-of`, evaluation uses current UTC time, so the fixture is stale and the run correctly returns `INSUFFICIENT_DATA`. Always pass `--as-of 2025-04-23T22:00:00Z` (tests use the `AS_OF` constant) to exercise the full scenario.
- Exit code 0 means a valid report was produced, *including abstention*. Exit 2 is a configuration or output-write error. Check `abstained` in the report, not the exit status.
- Fixtures only support the symbol `AAPL`; any other ticker abstains rather than relabelling synthetic data.
- API keys live in a git-ignored `.env` in the repo root (copy `.env.example`). The CLI loads it at start-up; values already exported in the shell win. Tests set `TRADING_DESK_SKIP_ENV_FILE` (in `tests/conftest.py`) so they never see real keys. `--provider anthropic` also accepts an `ant auth login` sign-in in place of `ANTHROPIC_API_KEY` (the SDK loads the profile); `tests/conftest.py` points `ANTHROPIC_CONFIG_DIR` at a missing folder so tests cannot use it. Never print or log a key's value.
- Tests are async without decorators (`asyncio_mode = "auto"`), and never hit the network: HTTP adapters are tested with `httpx.MockTransport`, the graph with injected fake providers.

## Architecture

The whole workflow is one LangGraph `StateGraph` built in `src/trading_desk/graph.py`; `run_research()` is the single entry point used by both the CLI and tests.

```
market_data ──► technical ─────┐
            └─► quality_audit ─┴─► contrarian ──► synthesis ──► Report
```

`technical` and `quality_audit` run as parallel branches with an explicit join before `contrarian`. The `trace` state key uses an `operator.add` reducer so both branches can append safely — any new state key written by parallel nodes needs a reducer too.

### Deterministic and LLM modes share one path

There is no separate LLM graph. Every reasoning node (`technical`, `contrarian`, `synthesis`) first computes a deterministic rule-based `Analysis` from `agents/*.py`, then passes it to the shared `reason()` helper in `graph.py`:

- `llm is None` → the rule-based result is the output (deterministic mode).
- rule-based result abstains → the model call is **skipped** and traced as `skipped`.
- otherwise → the model is called, and its output replaces the baseline only if it validates.

So `agents/` modules are both the deterministic mode *and* the gate/fallback contract for LLM mode. A model failure (malformed, refused, timed out, ungrounded) becomes a recorded abstention — never a silent switch to the rule-based answer.

### Authority ordering (the core design invariant)

Deterministic code outranks model output at every layer. When changing behaviour, preserve these:

1. **Indicators are the sole source of numbers.** `tools/indicators.py` computes SMA20/50, Wilder RSI14/ATR14, and 20-session return. Unavailable values are `None`, never zero-filled. Models cite numbers only via `Evidence(metric, value)` entries, and `reason()` rejects any evidence value that differs from the computed indicator (`ungrounded evidence`). Adding an indicator means updating `Indicators`, the `Evidence.metric` literal, and `calculate()` together.
2. **The quality audit is an abstention gate.** `quality()` in `graph.py` produces `blockers` (fewer than 50 candles, latest candle more than 5 calendar days before `as_of`, any gap over 5 calendar days, provider errors). Any blocker forces `INSUFFICIENT_DATA` downstream.
3. **Synthesis cannot override policy.** `decider()` re-imposes the deterministic baseline if it abstains, and if technical and contrarian are BULLISH vs BEARISH the synthesis must be `NEUTRAL` or `INSUFFICIENT_DATA` — a directional model answer is overridden and the trace marked `degraded`. Disagreement is always retained in `Report.dissent`.
4. **No look-ahead.** `retrieve()` drops candles later than `as_of` before indicators are computed; future `as_of` values and naive datetimes are rejected.

### Contracts (`models.py`)

All domain types inherit a base `Model` with `extra="forbid"` and `allow_inf_nan=False`. Validators enforce invariants rather than documenting them: coherent OHLC bounds, strictly chronological unique candles, `abstain == (classification == INSUFFICIENT_DATA)`, non-abstaining analyses must carry evidence, and `Report.classification` must equal the synthesis classification. `Analysis.model_json_schema()` is sent verbatim as the strict structured-output schema to the model, so changes to `Analysis`/`Evidence` change the LLM wire contract. Changing `Report` means bumping `schema_version` and regenerating the files in `examples/`.

NEO's output is a `ChartReport`: thesis, counter-case, support/resistance, one note per strategy considered, and an optional `TradeSetup`. A `TradeSetup` requires its strategy, entry, stop, target, confirmation and invalidation, so a partial setup cannot be constructed; when no pattern triggers the report has `setup=None` and must say what it is `watch_for`. ODIN then fails "Invalidation level set" and the idea is rejected. `DeskReport.schema_version` is `3.0`.

### Both sides

The symbol's own trend picks the side (`neo.overall_trend`): bullish takes long setups only, bearish takes short setups only, anything else takes nothing. Every strategy has a short mirror (breakdown below a prior low on volume; close below SMA20 below SMA50; upper-band touch as the entry with the lower band as the exit). `levels_fit_the_side` enforces `stop < entry < target` for a long and `target < entry < stop` for a short on setups and plans. HAWKEYE scores a downtrend as a short candidate and sets `ScoutReport.side`; PICARD requires the scout's side and the setup's side to agree, and applies each objection to the side it argues against (bearish news or an overbought counter-case stops a long; bullish news or an oversold counter-case stops a short; crowd hype stops longs only). ODIN's arithmetic uses distances, so it is side-neutral. The paper venue holds a short fully cash-collateralised (the notional is set aside at entry), mirrors stops, targets and gap rules, and applies slippage against the desk on both legs. Borrow fees and margin interest are **not** modelled. `SetupRules.allowed_sides` and the CLI's `--sides both|long|short` switch a side off.

### Cutting a trade early

A trade does not have to reach its stop to be closed. `RiskLimits.stalled_trade_sessions` (default 5) and `stalled_trade_progress_r` (default 0.5) define the rule, ODIN copies it onto the plan, and `PaperVenue._has_stalled` applies it at each session's close: held that many full sessions, closing below entry, and never ahead by that fraction of its risk means it is sold at the next open with exit reason `STALLED`. A trade that is ahead, or has already shown it can move, keeps its stop and target. `None` turns the rule off. The decision uses only bars up to the close; the fill is the following open.

### Trade records

Everything needed to review a trade is kept. `ClosedTrade` carries the strategy, the planned stop, target and risk, bars held, exit reason, costs, P&L in dollars and in R, and the maximum favourable and adverse excursion in R. The journal (`paper/journal.py`, SQLite) stores every desk event with its reason, every closed trade, and, in `trade_decisions`, the full six-agent report behind each TRADE (indicators, every agent's view, every risk check, the plan; the raw bars are left out). `desk serve` keeps a journal at `data/journal.sqlite` by default (`--no-journal` to turn it off); `desk backtest --journal PATH` does the same for a backtest. `PerformanceMetrics` reports average and largest win and loss in dollars, average bars held, and `results_by_strategy`.

### Provider boundaries

Both external dependencies are `typing.Protocol`s with a `name` attribute, injected into `build_graph()`:

- `MarketDataProvider.fetch(symbol) -> MarketData` (`tools/market_data.py`): `FixtureProvider` (synthetic JSON loaded via `importlib.resources` from `trading_desk.fixtures`, so fixtures ship in the wheel) and `AlphaVantageProvider` (unadjusted daily bars).
- `ReasoningProvider.reason(role, context) -> Analysis` (`llm.py`): `OpenAIProvider` calls the Responses API directly over httpx — no SDK, `store=false`, one stateless call per role with its own prompt from `PROMPTS`.

- `NewsFeed.fetch(symbol, start, end) -> list[NewsItem]` (`tools/news.py`): `FinnhubNewsFeed` fetches company news one calendar week at a time, caches finished weeks on disk, spaces requests under the free-tier rate limit and sends the key in a header. SNOWDEN's graph node asks for the week ending at `as_of`; a failed or slow feed makes SNOWDEN abstain (class name only in the trace), never "no news". News informs dissent and display; it does not gate trades, so backtests stay offline. Enable with `--news finnhub` on `desk decide`, `scan` or `serve`.
- `SentimentFeed.fetch(symbol, start, end) -> SentimentReading` (`tools/sentiment.py`): `HeadlineToneFeed` has the model label up to 40 numbered Finnhub headlines BULLISH/BEARISH/NEUTRAL (`ReasoningProvider.classify_headlines`); code checks every number is labelled exactly once, counts the labels, and FAMA's fixed thresholds do the rest. It is news tone, not social posts, and the reading's source says so. A one-sided reading can only turn TRADE into WAIT. Enable with `--sentiment headline-tone --news finnhub --provider anthropic|openai`. The same feed behind `--news-tone` lets SNOWDEN call the news BULLISH or BEARISH (at least 5 directional headlines; 60%/40% thresholds in `agents/snowden.py`); bearish news makes PICARD wait, bullish news changes nothing about a trade. Measured cost with Claude Haiku 5.5: about $0.0008 per symbol reading (reasoning disabled for this call), $0 on repeats from the model cache.

Both adapters share a pattern: three attempts with exponential backoff for transport errors, HTTP 429, and 5xx only; other HTTP errors fail immediately. The graph wraps each provider call in an overall `asyncio.wait_for` deadline. External failures are reported as errors — synthetic data is never substituted.

### Desk server (`server/`)

`DeskSession` (`server/session.py`) owns one paper venue and at most one replay. It runs the same `run_engine` loop as the backtest and uses the engine's `on_bar` hook only to pace, pause and publish; it cannot change a decision. `create_app()` (`server/app.py`) exposes it over HTTP (`/api/state`, `/api/replay/*`, `/api/kill-switch`, `/api/mode`, `/api/backtest`) and a WebSocket (`/ws/events`: one snapshot, then `bar`, `event`, `account` and `status` messages).

There is no login, so keep these properties: `desk serve` refuses any non-loopback `--host`; writes and WebSocket connections carrying a non-local `Origin` are refused; the out-of-sample window is never evaluated through the API; `live` mode answers 501. Tests drive the app in-process with `fastapi.testclient.TestClient` and inject the pacing sleep.

### Terminal UI (`web/`)

One page, no router. `api.ts` is the only file that talks to the server; `state.ts` is a pure reducer that folds the WebSocket messages (`snapshot`, `bar`, `event`, `account`, `status`) into one snapshot-shaped object; `derive.ts` turns that into what each panel shows; `components/` only render. Server field names are kept as-is (`types.ts`), so there is no mapping layer to drift. The chart is hand-drawn SVG, not a charting library. Tests replay real server output captured in `web/src/test/fixtures/` (regenerate by running a fixture replay through `DeskSession` if the message shapes change). Missing numbers render as a dash, never zero, and every state has a word or glyph as well as a colour.

### Error and logging hygiene

Two channels, two rules:

- **Reports, traces and anything published to the UI** carry only an exception's *class name*, because exception text can contain a credential-bearing URL. Tests assert that secrets in exception text do not reach a report; keep that property when adding error paths.
- **The log** carries the detail. Every handled failure is logged with its stack trace (`logger.warning(..., exc_info=True)` or `logger.exception`), with the symbol, feed or run id needed to find it. `observability.py` configures one `trading_desk` logger whose formatter redacts secrets from every line, stack traces included: the value of any environment variable ending in KEY, SECRET, TOKEN or PASSWORD, plus anything shaped like `apikey=`, `Bearer ...`, `sk-...` or `user:pass@`.

Get a logger with `get_logger("component")`. INFO is for things an operator wants to follow (replay start/finish, trades, fills, exits, kill switch, each metered model call and its cost); DEBUG is for routine skips and rejections; WARNING is for a handled failure or a refused request; ERROR is for a failed run or a configuration error. Never log a provider payload or model output wholesale. `--log-level` and `--log-file` control it; `desk serve` defaults to INFO and writes `logs/trading-desk.log` (rotating, git-ignored). Tests redirect that file to a temporary folder (`tests/conftest.py`).
