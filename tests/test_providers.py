import json

import httpx
import pytest

from trading_desk.llm import OpenAIProvider
from trading_desk.models import Analysis, Classification
from trading_desk.tools.market_data import AlphaVantageProvider


def response_body(analysis):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": analysis.model_dump_json()}],
            }
        ],
    }


async def test_llm_wire_contract_and_parsing():
    expected = Analysis(
        classification=Classification.INSUFFICIENT_DATA,
        summary="No evidence",
        evidence=[],
        risks=[],
        abstain=True,
    )

    def handler(request):
        body = json.loads(request.content)
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert body["store"] is False
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=response_body(expected))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAIProvider("test-key", "test-model", client).reason("technical", {})
    assert result == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": []},
        {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "refusal", "refusal": "Cannot comply"}]}
            ],
        },
    ],
)
async def test_incomplete_refused_or_missing_output_rejected(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ValueError):
            await OpenAIProvider("test", "test", client).reason("technical", {})


async def test_market_rate_limit_retry_budget(monkeypatch):
    calls = []

    async def no_wait(_):
        pass

    monkeypatch.setattr("trading_desk.tools.market_data.asyncio.sleep", no_wait)

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await AlphaVantageProvider("test", client).fetch("AAPL")
    assert len(calls) == 3


async def test_market_quota_payload_is_explicit_error():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"Information": "quota exceeded"})
        )
    ) as client:
        data = await AlphaVantageProvider("test", client).fetch("AAPL")
    assert data.errors
    assert not data.candles


async def test_market_valid_response_sorted_and_parsed():
    payload = {
        "Time Series (Daily)": {
            "2025-04-23": {
                "1. open": "101",
                "2. high": "103",
                "3. low": "100",
                "4. close": "102",
                "5. volume": "10000",
            },
            "2025-04-22": {
                "1. open": "100",
                "2. high": "102",
                "3. low": "99",
                "4. close": "101",
                "5. volume": "9000",
            },
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        data = await AlphaVantageProvider("test", client).fetch("AAPL")
    assert len(data.candles) == 2
    assert data.candles[-1].close == 102
    assert data.candles[0].timestamp < data.candles[-1].timestamp
