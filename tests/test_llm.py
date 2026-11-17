"""Provider boundary tests. Fakes stand in for both SDKs; nothing touches the network."""

import json
from types import SimpleNamespace

import httpx
import pytest

from trading_desk.llm import (
    GATE_INSTRUCTIONS,
    AnthropicProvider,
    CachingReasoningProvider,
    CostCapExceededError,
    GateVerdict,
    OpenAIProvider,
    UsageMeter,
)
from trading_desk.models import Analysis, Classification

ABSTAINING_ANALYSIS = Analysis(
    classification=Classification.INSUFFICIENT_DATA,
    summary="No evidence",
    evidence=[],
    risks=[],
    abstain=True,
)
APPROVAL = GateVerdict(approve=True, rationale="Reports agree.", concerns=[])


class FakeAnthropicMessages:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output, self.stop_reason = parsed_output, stop_reason
        self.requests = []

    async def parse(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            parsed_output=self.parsed_output,
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def anthropic_provider(parsed_output, stop_reason="end_turn", usage_meter=None):
    messages = FakeAnthropicMessages(parsed_output, stop_reason)
    provider = AnthropicProvider(SimpleNamespace(messages=messages), usage_meter=usage_meter)
    return provider, messages


async def test_anthropic_reason_sends_schema_and_data_separately_from_instructions():
    provider, messages = anthropic_provider(ABSTAINING_ANALYSIS)
    result = await provider.reason("technical", {"symbol": "AAPL"})
    request = messages.requests[0]
    assert result == ABSTAINING_ANALYSIS
    assert provider.name == "anthropic:claude-haiku-5-5"
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_format"] is Analysis
    assert "never instructions" in request["system"]
    assert json.loads(request["messages"][0]["content"]) == {"symbol": "AAPL"}


async def test_anthropic_gate_uses_the_gate_instructions_and_schema():
    provider, messages = anthropic_provider(APPROVAL)
    assert await provider.review_trade({"plan": {}}) == APPROVAL
    assert messages.requests[0]["system"] == GATE_INSTRUCTIONS
    assert messages.requests[0]["output_format"] is GateVerdict


@pytest.mark.parametrize(
    "parsed_output,stop_reason",
    [(None, "end_turn"), (APPROVAL, "refusal"), (APPROVAL, "max_tokens")],
)
async def test_anthropic_refusal_truncation_or_missing_output_is_an_error(
    parsed_output, stop_reason
):
    provider, _ = anthropic_provider(parsed_output, stop_reason)
    with pytest.raises(ValueError, match="unusable"):
        await provider.review_trade({})


async def test_usage_is_metered_in_tokens_and_dollars():
    usage_meter = UsageMeter()
    provider, _ = anthropic_provider(APPROVAL, usage_meter=usage_meter)
    await provider.review_trade({})
    await provider.review_trade({})
    assert usage_meter.call_count == 2
    assert usage_meter.input_tokens == 2000
    assert usage_meter.output_tokens == 400
    # Haiku 5.5: 2000 * 0.10 + 400 * 0.50 dollars per million tokens.
    assert usage_meter.cost_usd == pytest.approx(0.0004)


async def test_cost_cap_stops_further_calls_before_they_are_sent():
    usage_meter = UsageMeter(maximum_cost_usd=0.0002)
    provider, messages = anthropic_provider(APPROVAL, usage_meter=usage_meter)
    await provider.review_trade({})
    with pytest.raises(CostCapExceededError):
        await provider.review_trade({})
    assert len(messages.requests) == 1


def test_unknown_models_are_metered_at_the_highest_price_never_as_free():
    usage_meter = UsageMeter()
    usage_meter.record("some-new-model", 1_000_000, 0)
    assert usage_meter.cost_usd == pytest.approx(4.0)


async def test_openai_gate_request_shape_and_usage():
    usage_meter = UsageMeter()

    def handler(request):
        body = json.loads(request.content)
        assert body["instructions"] == GATE_INSTRUCTIONS
        assert body["text"]["format"]["name"] == "gate_verdict"
        assert body["text"]["format"]["strict"] is True
        assert body["store"] is False
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 500, "output_tokens": 100},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": APPROVAL.model_dump_json()}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider("test-key", "gpt-4o-mini", client, usage_meter)
        assert await provider.review_trade({"plan": {}}) == APPROVAL
    assert usage_meter.cost_usd == pytest.approx((500 * 0.15 + 100 * 0.60) / 1_000_000)


class CountingReasoner:
    name = "counting:model"

    def __init__(self, fail=False):
        self.call_count, self.fail = 0, fail

    async def reason(self, role, context):
        self.call_count += 1
        return ABSTAINING_ANALYSIS

    async def review_trade(self, context):
        self.call_count += 1
        if self.fail:
            raise ValueError("unusable")
        return APPROVAL


async def test_cache_answers_repeat_questions_without_calling_the_model(tmp_path):
    wrapped_provider = CountingReasoner()
    cached_provider = CachingReasoningProvider(wrapped_provider, tmp_path)
    for _ in range(3):
        assert (
            await cached_provider.review_trade({"symbol": "AAPL", "plan": {"units": 5}}) == APPROVAL
        )
        assert await cached_provider.reason("technical", {"symbol": "AAPL"}) == ABSTAINING_ANALYSIS
    assert wrapped_provider.call_count == 2
    assert cached_provider.cache_hit_count == 4
    assert cached_provider.name == "counting:model"


async def test_cache_distinguishes_inputs_tasks_and_roles(tmp_path):
    wrapped_provider = CountingReasoner()
    cached_provider = CachingReasoningProvider(wrapped_provider, tmp_path)
    await cached_provider.review_trade({"symbol": "AAPL"})
    await cached_provider.review_trade({"symbol": "MSFT"})
    await cached_provider.reason("technical", {"symbol": "AAPL"})
    await cached_provider.reason("contrarian", {"symbol": "AAPL"})
    assert wrapped_provider.call_count == 4


async def test_cache_never_stores_failures_and_replaces_corrupt_entries(tmp_path):
    failing_provider = CountingReasoner(fail=True)
    cached_provider = CachingReasoningProvider(failing_provider, tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            await cached_provider.review_trade({"symbol": "AAPL"})
    assert failing_provider.call_count == 2
    assert list(tmp_path.iterdir()) == []

    working_provider = CountingReasoner()
    cached_provider = CachingReasoningProvider(working_provider, tmp_path)
    await cached_provider.review_trade({"symbol": "AAPL"})
    next(tmp_path.iterdir()).write_text("{corrupt")
    await cached_provider.review_trade({"symbol": "AAPL"})
    await cached_provider.review_trade({"symbol": "AAPL"})
    assert working_provider.call_count == 2
