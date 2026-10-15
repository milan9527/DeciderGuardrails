from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .core import Case, Result, build_request, parse_response


def reject_nonfinite(value: str):
    raise ValueError("Non-finite JSON number")


def load_key(path: Path) -> str:
    value = os.environ.get("DECIDER_API_KEY", "").strip()
    if not value:
        value = path.read_text(encoding="utf-8").strip()
        if re.match(r"^(?:export\s+)?DECIDER_API_KEY\s*=", value):
            value = value.split("=", 1)[1].strip()
        value = value.strip("\"'")
    if not value or any(c.isspace() for c in value):
        raise ValueError("DECIDER_API_KEY / key file must contain a single nonempty key")
    return value


class DeciderProvider:
    name = "decider"

    def __init__(
        self, endpoint: str, key: str, policy: dict, mode: str,
        concurrency: int = 4, timeout: float = 30, retries: int = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Endpoint must be an HTTP(S) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Endpoint cannot contain credentials, query or fragment")
        if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS for remote authenticated endpoints")
        self.policy, self.mode, self.retries = policy, mode, retries
        self.key, self.endpoint = key, endpoint
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            follow_redirects=False, transport=transport,
        )

    async def evaluate(self, case: Case) -> Result:
        request = build_request(case, self.policy, self.mode)
        started = time.perf_counter()
        status, error, raw = None, None, None
        for attempt in range(1, self.retries + 2):
            status = None
            retryable = False
            try:
                response = await self.client.post(self.endpoint, json=request)
                status = response.status_code
                if status != 200:
                    # Never echo upstream errors: they may contain headers or credentials.
                    error = f"HTTP {status}"
                    retryable = status in {429, 500, 502, 503, 504}
                else:
                    raw = json.loads(json.dumps(
                        response.json(parse_constant=reject_nonfinite)
                    ).replace(self.key, "[REDACTED]"))
                    parsed = parse_response(raw, set(request["questions"]))
                    return Result(
                        ok=True, latency_ms=(time.perf_counter() - started) * 1000,
                        attempts=attempt, status_code=status, raw=raw, **parsed,
                    )
            except httpx.TransportError as exc:
                error, retryable = type(exc).__name__, True
            except (ValueError, TypeError, KeyError) as exc:
                error = "Invalid response: " + str(exc).replace(self.key, "[REDACTED]")[:200]
            if not retryable or attempt > self.retries:
                break
            await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 4))
        return Result(
            ok=False, latency_ms=(time.perf_counter() - started) * 1000,
            attempts=attempt, status_code=status, error=error,
        )

    async def close(self):
        await self.client.aclose()


class BedrockProvider:
    """Existing guardrail only. No creation or modification of AWS resources."""

    name = "bedrock"
    mode = "managed"

    def __init__(self, guardrail_id: str, version: str, region: str,
                 concurrency: int = 4, timeout: float = 30, retries: int = 0):
        import boto3
        from botocore.config import Config

        self.guardrail_id, self.version = guardrail_id, version
        self.client = boto3.client(
            "bedrock-runtime", region_name=region,
            config=Config(
                connect_timeout=timeout, read_timeout=timeout,
                max_pool_connections=concurrency,
                retries={"mode": "standard", "total_max_attempts": retries + 1},
            ),
        )

    def _evaluate(self, case: Case) -> Result:
        started = time.perf_counter()
        content = []
        if case.source == "OUTPUT" and case.reference:
            content.append({"text": {"text": case.reference, "qualifiers": ["grounding_source"]}})
        if case.source == "OUTPUT" and case.query:
            content.append({"text": {"text": case.query, "qualifiers": ["query"]}})
        candidate = {
            "text": (case.background + "\n" if case.background is not None else "") + case.content
        }
        if len(content):
            candidate["qualifiers"] = ["guard_content"]
        content.append({"text": candidate})
        try:
            raw = self.client.apply_guardrail(
                guardrailIdentifier=self.guardrail_id, guardrailVersion=self.version,
                source=case.source, content=content, outputScope="FULL",
            )
            action = raw.get("action")
            if action not in {"NONE", "GUARDRAIL_INTERVENED"}:
                raise ValueError("Unknown Bedrock guardrail action")
            metadata = raw.get("ResponseMetadata", {})
            # An intervention includes masking, not only a complete rejection.
            return Result(
                ok=True, latency_ms=(time.perf_counter() - started) * 1000,
                attempts=metadata.get("RetryAttempts", 0) + 1,
                status_code=metadata.get("HTTPStatusCode"),
                score=float(action == "GUARDRAIL_INTERVENED"),
                model=f"bedrock-guardrail:{self.guardrail_id}:{self.version}",
                usage=raw.get("usage", {}),
                raw={k: v for k, v in raw.items() if k != "ResponseMetadata"},
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            metadata = response.get("ResponseMetadata", {})
            code = response.get("Error", {}).get("Code", type(exc).__name__)
            return Result(
                ok=False, latency_ms=(time.perf_counter() - started) * 1000,
                error=str(code), status_code=metadata.get("HTTPStatusCode"),
                attempts=metadata.get("RetryAttempts", 0) + 1,
            )

    async def evaluate(self, case: Case) -> Result:
        return await asyncio.to_thread(self._evaluate, case)

    async def close(self):
        self.client.close()
