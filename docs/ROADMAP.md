# Roadmap

What is planned and not yet built, in rough order. Costs are estimates with their basis stated; replace them with measured figures once a feature has run.

## Next

### Trade review: dashboard and weekly report

The records now exist (see "Trade records" in `CLAUDE.md`): every closed trade with its strategy, plan, holding time and excursions, and the full six-agent report behind each decision. Nothing reads them yet.

- **Review page in the terminal.** A trades table that opens to the saved decision report, plus breakdowns by strategy, symbol, exit reason and holding time; average win and loss in dollars and R; excursion scatter (how far winners and losers travelled before they closed).
- **Weekly report.** A `desk report --week` command that writes one Markdown page from the same queries: trades taken, results by strategy, biggest win and loss, what was rejected and why, and the model spend for the week.
- Cost: none beyond build time. Both read the local journal.

### Short side

The trend already decides the side: a bearish trend allows shorts only. Shorts are not built, so a bearish trend currently takes nothing.

- NEO: mirror the three strategies (breakdown below a prior low on volume, downtrend continuation, upper-band touch as the entry with the lower band as the exit).
- ODIN: risk and reward maths for a short, and a short-notional limit.
- Paper venue: short positions, stop above entry, target below, mirrored gap rules and P&L.
- Assumptions to confirm: shorts fully cash-collateralised under the same 20% position cap; borrow fees not modelled ($0).

### Live polling on Alpaca

The Alpaca paper keys work (account, daily bars, snapshots, news). Nothing in the desk uses Alpaca yet, and no order is sent to any broker: every trade is simulated by the desk's own paper venue.

- A live data provider on Alpaca bars, and a session mode that evaluates as bars arrive.
- Decision needed: how a still-forming daily bar is treated.
- Cost: $0 on Alpaca's free market-data plan (IEX feed). The paid SIP feed has a monthly fee that has not been checked.

### Intraday resolution of ambiguous bars

When one daily bar touches both the stop and the target, the venue assumes the stop came first because a daily bar cannot say which was hit first. With Alpaca minute bars the venue can look inside that one day and use the real order, falling back to the worst case when minute data is missing.

## Later

### Crowd sentiment from X for FAMA

**Deferred on cost (owner's decision, 2026-11-17).** FAMA currently reads the tone of Finnhub news headlines, not social posts.

- X's API is pay-per-use at about $0.005 per post read (from third-party write-ups of X's pricing; not yet confirmed on X's own pricing page).
- At 100 posts per symbol: $0.50 per reading, $5.00 for the ten-symbol watchlist, about $105 a month at one reading per trading day. Several readings a day multiplies that: four a day is about $420 a month.
- Recent search reaches back 7 days, so it would work live but not in historical replays.
- Needs an X developer account with prepaid credits and the token in `.env`.
- Revisit when the desk has more capital or is making money. Cheaper routes to price first: fewer posts per reading, only symbols HAWKEYE has on watch, or a third-party reseller.

### Let news and sentiment do more than veto

Today bearish news or one-sided hype can only make the desk wait; bullish news never changes a trade. The trade records now include the news and sentiment reading at entry, so the question "do trades taken on bullish news do better?" can be answered from data.

- First, at no added risk: use agreement between news, sentiment and the setup to rank which approved idea to take when there are several.
- Then, only if the records show it helps: let agreement adjust position size within the existing risk limits.

### Other markets

Major crypto, then Solana tokens, as new `MarketProfile`s with their own data and cost models.

### Hosting

One container on Fly.io (about $6.85 a month for 1 GB of memory and a 1 GB volume, from Fly's pricing docs) once there is a login. The server has none today and refuses to listen on anything but this machine.

## Before any real money

Not started, and not to be started on the strength of backtests alone.

- Positive out-of-sample results after costs. The out-of-sample window has never been evaluated.
- At least 20 reviewed paper decisions.
- An intermediate step on Alpaca's paper account, placing simulated orders through Alpaca's API instead of the desk's own venue.
- Human approval on every order, a hard kill switch, and a security review of the broker integration.
- Alpaca is the likely first broker because its paper and live accounts share one API. Schwab, E*Trade and Interactive Brokers are more work to connect.
