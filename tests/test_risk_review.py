from test_graph import AS_OF

from trading_desk.agents.technical import analyze
from trading_desk.graph import run_research
from trading_desk.models import Analysis, Classification, Evidence
from trading_desk.tools.market_data import FixtureProvider


async def test_counter_evidence_is_retained():
    r = await run_research("AAPL", FixtureProvider("conflict"), as_of=AS_OF)
    assert r.technical.classification == Classification.BULLISH
    assert r.contrarian.classification == Classification.BEARISH
    assert r.classification == Classification.NEUTRAL
    assert r.dissent
    assert r.contrarian.evidence


class MalformedLLM:
    name = "malformed-test-provider"

    async def reason(self, role, context):
        return Analysis.model_validate({"classification": "GUARANTEED_WIN"})


async def test_invalid_llm_output_fails_closed():
    r = await run_research("AAPL", FixtureProvider(), MalformedLLM(), AS_OF)
    assert r.mode == "llm"
    assert r.abstained
    assert r.trace[-1].status == "skipped"
    assert any(t.status == "degraded" for t in r.trace)


class UngroundedLLM:
    name = "ungrounded-test-provider"

    async def reason(self, role, context):
        return Analysis(
            classification=Classification.BULLISH,
            summary="Claim",
            evidence=[Evidence(metric="close", value=999999)],
            risks=[],
            abstain=False,
        )


async def test_fabricated_measurements_rejected():
    r = await run_research("AAPL", FixtureProvider(), UngroundedLLM(), AS_OF)
    assert r.abstained
    assert not r.technical.evidence


class OverridingLLM:
    name = "policy-test-provider"

    async def reason(self, role, context):
        if role == "technical":
            from trading_desk.models import Indicators

            return analyze(Indicators.model_validate(context["indicators"]))
        return Analysis(
            classification=Classification.BEARISH
            if role == "contrarian"
            else Classification.BULLISH,
            summary="Counter-thesis" if role == "contrarian" else "Ignoring disagreement",
            evidence=[Evidence(metric="close", value=context["indicators"]["close"])],
            risks=["Counter-scenario"],
            abstain=False,
        )


async def test_synthesis_cannot_override_directional_conflict():
    r = await run_research("AAPL", FixtureProvider(), OverridingLLM(), AS_OF)
    assert r.classification == Classification.NEUTRAL
    assert r.trace[-1].status == "degraded"


async def test_bad_data_skips_downstream_model_spend():
    r = await run_research("AAPL", FixtureProvider("missing"), MalformedLLM(), AS_OF)
    assert r.abstained
    assert all(
        t.status == "skipped"
        for t in r.trace
        if t.agent in {"technical", "contrarian", "synthesis"}
    )


class RecordingLLM:
    name = "recording-test-provider"

    def __init__(self):
        self.calls = []

    async def reason(self, role, context):
        from trading_desk.agents import contrarian, synthesis
        from trading_desk.models import Indicators

        self.calls.append((role, context))
        i = Indicators.model_validate(context["indicators"])
        if role == "technical":
            return analyze(i)
        t = Analysis.model_validate(context["technical"])
        if role == "contrarian":
            return contrarian.review(i, t, context["blockers"])
        return synthesis.synthesize(
            t, Analysis.model_validate(context["contrarian"]), context["blockers"]
        )


async def test_llm_roles_receive_explicit_dependencies():
    llm = RecordingLLM()
    r = await run_research("AAPL", FixtureProvider(), llm, AS_OF)
    assert r.mode == "llm"
    assert not r.abstained
    assert [role for role, _ in llm.calls] == ["technical", "contrarian", "synthesis"]
    assert "technical" not in llm.calls[0][1]
    assert "technical" in llm.calls[1][1]
    assert "contrarian" in llm.calls[2][1]
    assert all(
        t.provider == llm.name
        for t in r.trace
        if t.agent in {"technical", "contrarian", "synthesis"}
    )


def test_long_valid_agent_summaries_do_not_break_reconciliation():
    from trading_desk.agents.synthesis import synthesize

    t = Analysis(
        classification=Classification.BULLISH,
        summary="T" * 3000,
        evidence=[Evidence(metric="close", value=100)],
        risks=[],
        abstain=False,
    )
    c = t.model_copy(update={"classification": Classification.BEARISH, "summary": "C" * 3000})
    result = synthesize(t, c, [])
    assert len(result.summary) <= 3000
    assert result.classification == Classification.NEUTRAL
