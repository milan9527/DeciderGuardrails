import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from guardrails_bench.core import DEFAULT_ENDPOINT, Case, load_cases, load_policy
from guardrails_bench.providers import BedrockProvider, DeciderProvider, load_key


def test_credentials_from_env_and_export_file(tmp_path, monkeypatch):
    path = tmp_path / "key.txt"
    path.write_text("export DECIDER_API_KEY='file-secret'\n")
    monkeypatch.delenv("DECIDER_API_KEY", raising=False)
    assert load_key(path) == "file-secret"
    monkeypatch.setenv("DECIDER_API_KEY", "env-secret")
    assert load_key(path) == "env-secret"


@pytest.mark.parametrize("endpoint", [
    "http://example.com/v1/systemone",
    "https://user:secret@example.com/v1/systemone",
    "https://example.com/v1/systemone?api_key=secret",
])
def test_remote_key_transport_rejects_insecure_or_embedded_auth(endpoint):
    with pytest.raises(ValueError):
        DeciderProvider(endpoint, "secret", load_policy(), "compact")


def test_retry_is_explicit_and_errors_never_echo_credentials():
    requests = []
    policy = load_policy()
    case = load_cases(None, policy)[0]

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-secret"
        if len(requests) == 1:
            return httpx.Response(503, text="upstream echoed Bearer test-secret")
        body = json.loads(request.content)
        return httpx.Response(200, json={"answers": {
            name: {"type": "choice", "choice": "block",
                   "probabilities": {"allow": 0.2, "block": 0.8}, "confidence": 0.6}
            for name in body["questions"]
        }})

    async def run(retries):
        provider = DeciderProvider(DEFAULT_ENDPOINT, "test-secret", policy, "compact",
                                   retries=retries, transport=httpx.MockTransport(handler))
        try:
            return await provider.evaluate(case)
        finally:
            await provider.close()

    result = asyncio.run(run(0))
    assert not result.ok and result.error == "HTTP 503"
    assert "test-secret" not in result.error
    requests.clear()
    result = asyncio.run(run(1))
    assert result.ok and result.attempts == 2
    assert result.latency_ms >= 250


def test_invalid_json_is_a_recorded_error():
    async def run():
        provider = DeciderProvider(
            DEFAULT_ENDPOINT, "secret", load_policy(), "compact",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")),
        )
        try:
            return await provider.evaluate(load_cases(None, load_policy())[0])
        finally:
            await provider.close()
    result = asyncio.run(run())
    assert not result.ok
    assert result.error.startswith("Invalid response")


def test_bedrock_receives_grounding_roles_and_masking_is_intervention():
    calls = []

    class FakeClient:
        def apply_guardrail(self, **kwargs):
            calls.append(kwargs)
            return {"action": "GUARDRAIL_INTERVENED", "usage": {},
                    "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}}

    provider = BedrockProvider.__new__(BedrockProvider)
    provider.client = FakeClient()
    provider.guardrail_id, provider.version = "existing-id", "1"
    case = Case("g", "grounding", "en", "Ninety days.", "block", ("grounding",),
                source="OUTPUT", reference="Returns within 30 days.", query="Return window?")
    result = provider._evaluate(case)
    assert result.ok and result.score == 1
    assert [c["text"]["qualifiers"][0] for c in calls[0]["content"]] == [
        "grounding_source", "query", "guard_content"
    ]
    assert "ResponseMetadata" not in result.raw
    provider._evaluate(replace(case, background="Performance padding."))
    assert calls[1]["content"][-1]["text"]["text"] == "Performance padding.\nNinety days."
