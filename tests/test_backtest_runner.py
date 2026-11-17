from datetime import UTC, datetime

import pytest

from trading_desk.agents.neo import SetupRules
from trading_desk.backtest.runner import (
    BacktestReport,
    HoldoutAlreadyUsedError,
    HoldoutLedger,
    configuration_hash,
    run_backtest,
)
from trading_desk.markets import US_EQUITIES
from trading_desk.models import RiskLimits
from trading_desk.tools.market_data import FixtureProvider

START = datetime(2025, 1, 2, tzinfo=UTC)
SPLIT = datetime(2025, 4, 1, tzinfo=UTC)
END = datetime(2025, 4, 24, tzinfo=UTC)


async def backtest(**options):
    """Test helper (not a test): backtest."""
    return await run_backtest(["AAPL"], FixtureProvider(), start=START, end=END, **options)


async def test_default_run_scores_in_sample_only():
    """Verifies: default run scores in sample only."""
    report = await backtest()
    assert report.out_of_sample is None
    assert report.out_of_sample_note == "Not evaluated: no split date was given."
    assert report.in_sample.bars == 80
    assert report.in_sample.trade_count >= 1
    assert report.data_source == "bundled-synthetic-fixture"
    assert report.caveats
    assert BacktestReport.model_validate_json(report.model_dump_json()) == report


async def test_split_keeps_the_out_of_sample_window_untouched_by_default():
    """Verifies: split keeps the out of sample window untouched by default."""
    report = await backtest(split=SPLIT)
    assert report.out_of_sample is None
    assert "left untouched" in report.out_of_sample_note
    assert report.in_sample.end < SPLIT


async def test_out_of_sample_is_scored_once_and_recorded_first(tmp_path):
    """Verifies: out of sample is scored once and recorded first."""
    ledger = HoldoutLedger(tmp_path / "research" / "holdout.jsonl")
    report = await backtest(split=SPLIT, evaluate_out_of_sample=True, holdout_ledger=ledger)
    assert report.out_of_sample.start >= SPLIT
    assert report.out_of_sample_note == "Evaluated once for this configuration."
    assert len(ledger.previous_uses(report.configuration_hash)) == 1
    with pytest.raises(HoldoutAlreadyUsedError):
        await backtest(split=SPLIT, evaluate_out_of_sample=True, holdout_ledger=ledger)


async def test_explicit_reuse_is_allowed_but_marked_contaminated(tmp_path):
    """Verifies: explicit reuse is allowed but marked contaminated."""
    ledger = HoldoutLedger(tmp_path / "holdout.jsonl")
    await backtest(split=SPLIT, evaluate_out_of_sample=True, holdout_ledger=ledger)
    report = await backtest(
        split=SPLIT, evaluate_out_of_sample=True, holdout_ledger=ledger, allow_holdout_reuse=True
    )
    assert "contaminated" in report.out_of_sample_note
    uses = ledger.previous_uses(report.configuration_hash)
    assert [use["reuse"] for use in uses] == [False, True]


async def test_a_changed_configuration_is_a_different_exam(tmp_path):
    """Verifies: a changed configuration is a different exam."""
    ledger = HoldoutLedger(tmp_path / "holdout.jsonl")
    first_report = await backtest(split=SPLIT, evaluate_out_of_sample=True, holdout_ledger=ledger)
    second_report = await backtest(
        split=SPLIT,
        evaluate_out_of_sample=True,
        holdout_ledger=ledger,
        limits=RiskLimits(minimum_reward_to_risk=2.2),
    )
    assert first_report.configuration_hash != second_report.configuration_hash


@pytest.mark.parametrize(
    "options",
    [
        {"split": START},
        {"split": datetime(2026, 1, 1, tzinfo=UTC)},
        {"evaluate_out_of_sample": True},
        {"split": SPLIT, "evaluate_out_of_sample": True},
    ],
)
async def test_invalid_split_or_missing_ledger_is_refused(options):
    """Verifies: invalid split or missing ledger is refused."""
    with pytest.raises(ValueError):
        await backtest(**options)


async def test_backtest_is_repeatable():
    """Verifies: backtest is repeatable."""
    first_report, second_report = await backtest(), await backtest()
    assert first_report.in_sample == second_report.in_sample
    assert first_report.configuration_hash == second_report.configuration_hash


def test_configuration_hash_ignores_symbol_order_but_not_rules():
    """Verifies: configuration hash ignores symbol order but not rules."""

    def fingerprint(symbols, setup_rules=None):
        return configuration_hash(
            symbols, RiskLimits(), setup_rules or SetupRules(), US_EQUITIES, SPLIT, END
        )

    assert fingerprint(["AAPL", "MSFT"]) == fingerprint(["MSFT", "AAPL"])
    assert fingerprint(["AAPL"]) != fingerprint(["AAPL"], SetupRules(stop_atr_multiple=3))
