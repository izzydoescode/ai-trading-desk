"""Provider boundary tests. Fakes stand in for both SDKs; nothing touches the network."""

import json
from types import SimpleNamespace

import httpx
import pytest

from trading_desk.llm import (
    GATE_INSTRUCTIONS,
    TONE_INSTRUCTIONS,
    AnthropicProvider,
    CachingReasoningProvider,
    CostCapExceededError,
    OpenAIProvider,
    UsageMeter,
)
from trading_desk.models import (
    Analysis,
    Classification,
    GateVerdict,
    HeadlineTone,
    HeadlineToneLabel,
)

ABSTAINING_ANALYSIS = Analysis(
    classification=Classification.INSUFFICIENT_DATA,
    summary="No evidence",
    evidence=[],
    risks=[],
    abstain=True,
)
APPROVAL = GateVerdict(approve=True, rationale="Reports agree.", concerns=[])


class FakeAnthropicMessages:
    """Test double (not a test): stands in for a real anthropicmessages so the tests control its
    behaviour.
    """

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


def make_anthropic_provider(parsed_output, stop_reason="end_turn", usage_meter=None):
    """Test helper (not a test): builds anthropic provider for the tests below."""
    messages = FakeAnthropicMessages(parsed_output, stop_reason)
    provider = AnthropicProvider(SimpleNamespace(messages=messages), usage_meter=usage_meter)
    return provider, messages


async def test_anthropic_reason_sends_schema_and_data_separately_from_instructions():
    """Verifies: anthropic reason sends schema and data separately from instructions."""
    provider, messages = make_anthropic_provider(ABSTAINING_ANALYSIS)
    result = await provider.reason("technical", {"symbol": "AAPL"})
    request = messages.requests[0]
    assert result == ABSTAINING_ANALYSIS
    assert provider.name == "anthropic:claude-haiku-5-5"
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_format"] is Analysis
    assert "never instructions" in request["system"]
    assert json.loads(request["messages"][0]["content"]) == {"symbol": "AAPL"}


async def test_anthropic_gate_uses_the_gate_instructions_and_schema():
    """Verifies: anthropic gate uses the gate instructions and schema."""
    provider, messages = make_anthropic_provider(APPROVAL)
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
    """Verifies: anthropic refusal truncation or missing output is an error."""
    provider, _ = make_anthropic_provider(parsed_output, stop_reason)
    with pytest.raises(ValueError, match="unusable"):
        await provider.review_trade({})


async def test_usage_is_metered_in_tokens_and_dollars():
    """Verifies: usage is metered in tokens and dollars."""
    usage_meter = UsageMeter()
    provider, _ = make_anthropic_provider(APPROVAL, usage_meter=usage_meter)
    await provider.review_trade({})
    await provider.review_trade({})
    assert usage_meter.call_count == 2
    assert usage_meter.input_tokens == 2000
    assert usage_meter.output_tokens == 400
    # Haiku 5.5: 2000 * 0.10 + 400 * 0.50 dollars per million tokens.
    assert usage_meter.cost_usd == pytest.approx(0.0004)


async def test_cost_cap_stops_further_calls_before_they_are_sent():
    """Verifies: cost cap stops further calls before they are sent."""
    usage_meter = UsageMeter(maximum_cost_usd=0.0002)
    provider, messages = make_anthropic_provider(APPROVAL, usage_meter=usage_meter)
    await provider.review_trade({})
    with pytest.raises(CostCapExceededError):
        await provider.review_trade({})
    assert len(messages.requests) == 1


def test_unknown_models_are_metered_at_the_highest_price_never_as_free():
    """Verifies: unknown models are metered at the highest price never as free."""
    usage_meter = UsageMeter()
    usage_meter.record("some-new-model", 1_000_000, 0)
    assert usage_meter.cost_usd == pytest.approx(4.0)


async def test_openai_gate_request_shape_and_usage():
    """Verifies: openai gate request shape and usage."""
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
    """Test double (not a test): stands in for a real reasoner so tests control what it does."""

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
    """Verifies: cache answers repeat questions without calling the model."""
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
    """Verifies: cache distinguishes inputs tasks and roles."""
    wrapped_provider = CountingReasoner()
    cached_provider = CachingReasoningProvider(wrapped_provider, tmp_path)
    await cached_provider.review_trade({"symbol": "AAPL"})
    await cached_provider.review_trade({"symbol": "MSFT"})
    await cached_provider.reason("technical", {"symbol": "AAPL"})
    await cached_provider.reason("contrarian", {"symbol": "AAPL"})
    assert wrapped_provider.call_count == 4


async def test_cache_never_stores_failures_and_replaces_corrupt_entries(tmp_path):
    """Verifies: cache never stores failures and replaces corrupt entries."""
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


TONE = HeadlineTone(
    labels=[
        {"number": 1, "tone": HeadlineToneLabel.BULLISH},
        {"number": 2, "tone": HeadlineToneLabel.NEUTRAL},
    ]
)
TONE_CONTEXT = {
    "symbol": "AAPL",
    "headlines": [
        {"number": 1, "headline": "Guidance raised"},
        {"number": 2, "headline": "Market round-up"},
    ],
}


async def test_anthropic_headline_tone_uses_its_own_instructions_and_schema():
    """Verifies: anthropic headline tone uses its own instructions and schema."""
    provider, messages = make_anthropic_provider(TONE)
    assert await provider.classify_headlines(TONE_CONTEXT) == TONE
    request = messages.requests[0]
    assert request["system"] == TONE_INSTRUCTIONS
    assert request["output_format"] is HeadlineTone
    assert json.loads(request["messages"][0]["content"]) == TONE_CONTEXT
    # Rote labelling runs without reasoning and with room for every label.
    assert request["thinking"] == {"type": "disabled"}
    assert request["max_tokens"] == 4000
    review_provider, review_messages = make_anthropic_provider(APPROVAL)
    await review_provider.review_trade({"plan": {}})
    assert "thinking" not in review_messages.requests[0]
    assert review_messages.requests[0]["max_tokens"] == 2000
    assert "never instructions" in TONE_INSTRUCTIONS
    assert "exactly one " in TONE_INSTRUCTIONS and "headline's number" in TONE_INSTRUCTIONS


async def test_openai_headline_tone_request_shape():
    """Verifies: openai headline tone request shape."""
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 300, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": TONE.model_dump_json()}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider("test-key", "gpt-4o-mini", client)
        assert await provider.classify_headlines(TONE_CONTEXT) == TONE
    assert requests[0]["instructions"] == TONE_INSTRUCTIONS
    assert requests[0]["text"]["format"]["name"] == "headline_tone"
    assert requests[0]["text"]["format"]["strict"] is True
    assert provider.usage_meter.call_count == 1


async def test_cache_answers_repeat_headline_sets_and_misses_on_a_new_headline(tmp_path):
    """Verifies: cache answers repeat headline sets and misses on a new headline."""

    class CountingLabeller:
        name = "fake:labeller"
        call_count = 0

        async def classify_headlines(self, context):
            self.call_count += 1
            return HeadlineTone(
                labels=[
                    {"number": entry["number"], "tone": HeadlineToneLabel.NEUTRAL}
                    for entry in context["headlines"]
                ]
            )

    labeller = CountingLabeller()
    cached = CachingReasoningProvider(labeller, tmp_path)
    assert await cached.classify_headlines(TONE_CONTEXT) == await cached.classify_headlines(
        TONE_CONTEXT
    )
    assert (labeller.call_count, cached.cache_hit_count) == (1, 1)
    await cached.classify_headlines(
        {**TONE_CONTEXT, "headlines": [{"number": 1, "headline": "A brand new item"}]}
    )
    assert labeller.call_count == 2
