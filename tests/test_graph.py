from datetime import UTC, datetime, timedelta

import pytest

from trading_desk.graph import run_research
from trading_desk.models import Classification, MarketData, Report
from trading_desk.tools.market_data import FixtureProvider

AS_OF = datetime(2025, 4, 23, 22, tzinfo=UTC)


async def test_full_workflow_and_round_trip():
    report = await run_research("aapl", FixtureProvider(), as_of=AS_OF)
    assert report.symbol == "AAPL"
    assert report.mode == "deterministic"
    assert report.data.synthetic
    assert not report.abstained
    assert {t.agent for t in report.trace} == {
        "market_data",
        "technical",
        "quality_audit",
        "contrarian",
        "synthesis",
    }
    assert all(t.run_id == report.run_id for t in report.trace)
    assert Report.model_validate_json(report.model_dump_json()) == report


async def test_missing_data_abstains_and_names_missing_inputs():
    report = await run_research("AAPL", FixtureProvider("missing"), as_of=AS_OF)
    assert report.classification == Classification.INSUFFICIENT_DATA
    assert report.abstained
    assert report.indicators.sma50 is None
    assert any("missing 42" in x for x in report.limitations)


async def test_unknown_symbol_does_not_relabel_fixture():
    report = await run_research("MSFT", FixtureProvider(), as_of=AS_OF)
    assert report.abstained
    assert not report.data.candles


async def test_stale_data_abstains():
    report = await run_research("AAPL", FixtureProvider(), as_of=AS_OF + timedelta(days=10))
    assert report.abstained
    assert any("Stale" in x for x in report.limitations)


async def test_no_lookahead():
    report = await run_research("AAPL", FixtureProvider(), as_of=AS_OF - timedelta(days=75))
    assert all(c.timestamp <= report.evaluated_as_of for c in report.data.candles)
    assert report.abstained


class FailingData:
    name = "failing"

    async def fetch(self, symbol):
        raise RuntimeError("secret-token-must-not-appear")


async def test_provider_failure_is_controlled_and_redacted():
    report = await run_research("AAPL", FailingData(), as_of=AS_OF)
    assert report.abstained
    assert "secret-token" not in report.model_dump_json()
    assert report.trace[0].status == "degraded"


class WrongSymbol:
    name = "wrong"

    async def fetch(self, symbol):
        return MarketData(symbol="MSFT", source=self.name)


async def test_symbol_mismatch_abstains():
    assert (await run_research("AAPL", WrongSymbol(), as_of=AS_OF)).abstained


async def test_invalid_timestamp_and_symbol():
    with pytest.raises(ValueError):
        await run_research("../AAPL", FixtureProvider())
    with pytest.raises(ValueError):
        await run_research("AAPL", FixtureProvider(), as_of=datetime(2025, 1, 1))


class GappedData:
    name = "gapped"

    async def fetch(self, symbol):
        data = await FixtureProvider().fetch(symbol)
        return data.model_copy(update={"candles": data.candles[:30] + data.candles[40:]})


async def test_internal_gap_blocks_full_length_history():
    r = await run_research("AAPL", GappedData(), as_of=AS_OF)
    assert r.indicators.candle_count == 70
    assert r.abstained
    assert any("gap longer" in reason for reason in r.limitations)


async def test_future_evaluation_rejected():
    with pytest.raises(ValueError, match="future"):
        await run_research("AAPL", FixtureProvider(), as_of=datetime.now(UTC) + timedelta(days=1))


class ExtremeData:
    name = "extreme"

    async def fetch(self, symbol):
        data = await FixtureProvider().fetch(symbol)
        bars = [
            c.model_copy(update={"open": 1e308, "high": 1e308, "low": 1e308, "close": 1e308})
            for c in data.candles
        ]
        return data.model_copy(update={"candles": bars})


async def test_numeric_overflow_fails_closed_with_source_preserved():
    r = await run_research("AAPL", ExtremeData(), as_of=AS_OF)
    assert r.abstained
    assert len(r.data.candles) == 80
    assert r.indicators.sma50 is None
    assert any("Indicator calculation rejected" in x for x in r.limitations)
