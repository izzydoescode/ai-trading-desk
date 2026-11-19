"""Backtest runner: in-sample by default, out-of-sample only on request and on record.

Thresholds may be tuned against the in-sample window as often as you like. The
out-of-sample window is the exam: every look at it is written to a ledger BEFORE
the run, and the same configuration is refused a second look unless reuse is
explicitly allowed (which is also recorded). A result that was only seen once is
the only one worth trusting.
"""

import getpass
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime

from trading_desk.agents.neo import DEFAULT_SETUP_RULES, SetupRules
from trading_desk.backtest.metrics import PerformanceMetrics, compute_metrics
from trading_desk.desk import DEFAULT_RISK_LIMITS
from trading_desk.engine import run_engine
from trading_desk.markets import US_EQUITIES, MarketProfile
from trading_desk.models import Model, RiskLimits
from trading_desk.paper.venue import PaperVenue
from trading_desk.tools.historical_data import preload_history
from trading_desk.tools.market_data import MarketDataProvider

CAVEATS = [
    "Paper simulation on daily bars: no market impact, queue position or intraday path.",
    "Scoring, stops and targets are uncalibrated rules, not a validated strategy.",
    "Past results, including these, do not predict future results.",
]


class HoldoutAlreadyUsedError(RuntimeError):
    """This exact configuration has already been scored on the out-of-sample window."""


class HoldoutLedger:
    """Append-only record of every look at the out-of-sample window."""

    def __init__(self, ledger_path: Path):
        self.ledger_path = Path(ledger_path)

    def previous_uses(self, configuration_hash: str) -> list[dict]:
        if not self.ledger_path.is_file():
            return []
        entries = [
            json.loads(line) for line in self.ledger_path.read_text().splitlines() if line.strip()
        ]
        return [entry for entry in entries if entry["configuration_hash"] == configuration_hash]

    def claim(self, configuration_hash: str, allow_reuse: bool = False) -> bool:
        """Record a use before the run starts. Returns True when this is a reuse."""
        is_reuse = bool(self.previous_uses(configuration_hash))
        if is_reuse and not allow_reuse:
            raise HoldoutAlreadyUsedError(
                f"Out-of-sample window already used for configuration {configuration_hash}; "
                "change nothing and trust the first result, or allow reuse explicitly."
            )
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a") as ledger_file:
            ledger_file.write(
                json.dumps(
                    {
                        "used_at": datetime.now(UTC).isoformat(),
                        "configuration_hash": configuration_hash,
                        "reuse": is_reuse,
                        "user": getpass.getuser(),
                    }
                )
                + "\n"
            )
        return is_reuse


class BacktestReport(Model):
    """The saved result of a backtest: its configuration and fingerprint, the in-sample scorecard,
    the
    out-of-sample scorecard only if it was deliberately evaluated, and the caveats that apply to
    reading it.
    """

    generated_at: AwareDatetime
    market: str
    data_source: str
    symbols: list[str]
    starting_cash: float
    limits: RiskLimits
    setup_rules: SetupRules
    configuration_hash: str
    in_sample: PerformanceMetrics
    out_of_sample: PerformanceMetrics | None = None
    out_of_sample_note: str
    caveats: list[str]


def configuration_hash(
    symbols: Sequence[str],
    limits: RiskLimits,
    setup_rules: SetupRules,
    market_profile: MarketProfile,
    split: datetime | None,
    end: datetime,
) -> str:
    """Stable fingerprint of everything that defines one out-of-sample exam."""
    blob = json.dumps(
        {
            "symbols": sorted(symbols),
            "limits": limits.model_dump(mode="json"),
            "setup_rules": setup_rules.model_dump(mode="json"),
            "market": market_profile.model_dump(mode="json"),
            "split": split.isoformat() if split else None,
            "end": end.isoformat(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


async def run_backtest(
    symbols: Sequence[str],
    data_provider: MarketDataProvider,
    *,
    start: datetime,
    end: datetime,
    split: datetime | None = None,
    evaluate_out_of_sample: bool = False,
    holdout_ledger: HoldoutLedger | None = None,
    allow_holdout_reuse: bool = False,
    starting_cash: float = 100_000.0,
    market_profile: MarketProfile = US_EQUITIES,
    limits: RiskLimits = DEFAULT_RISK_LIMITS,
    setup_rules: SetupRules = DEFAULT_SETUP_RULES,
) -> BacktestReport:
    """Score the desk over `[start, split)` and, only when asked, over `[split, end]`."""
    if split is not None and not (start < split <= end):
        raise ValueError("the split must fall after the start and not after the end")
    if evaluate_out_of_sample and (split is None or holdout_ledger is None):
        raise ValueError("out-of-sample evaluation needs a split date and a holdout ledger")
    fingerprint = configuration_hash(symbols, limits, setup_rules, market_profile, split, end)
    out_of_sample_note = "Not evaluated: no split date was given."
    if split is not None:
        out_of_sample_note = "Not evaluated: the out-of-sample window was left untouched."
    if evaluate_out_of_sample:
        # Claim before running anything, so an interrupted run still counts as a look.
        is_reuse = holdout_ledger.claim(fingerprint, allow_holdout_reuse)
        out_of_sample_note = (
            "Evaluated again for this configuration: treat as contaminated."
            if is_reuse
            else "Evaluated once for this configuration."
        )

    symbols_to_load = list(dict.fromkeys([*symbols, market_profile.benchmark_symbol]))
    preloaded_provider = await preload_history(data_provider, symbols_to_load)
    benchmark_history = await preloaded_provider.fetch(market_profile.benchmark_symbol)

    async def score_window(window_start: datetime, window_end: datetime) -> PerformanceMetrics:
        engine_result = await run_engine(
            symbols,
            preloaded_provider,
            PaperVenue(starting_cash, market_profile),
            start=window_start,
            end=window_end,
            market_profile=market_profile,
            limits=limits,
            setup_rules=setup_rules,
        )
        return compute_metrics(engine_result, market_profile, benchmark_history.candles)

    in_sample_end = end if split is None else split - timedelta(microseconds=1)
    in_sample_metrics = await score_window(start, in_sample_end)
    out_of_sample_metrics = await score_window(split, end) if evaluate_out_of_sample else None
    return BacktestReport(
        generated_at=datetime.now(UTC),
        market=market_profile.name,
        data_source=data_provider.name,
        symbols=list(symbols),
        starting_cash=starting_cash,
        limits=limits,
        setup_rules=setup_rules,
        configuration_hash=fingerprint,
        in_sample=in_sample_metrics,
        out_of_sample=out_of_sample_metrics,
        out_of_sample_note=out_of_sample_note,
        caveats=CAVEATS,
    )
