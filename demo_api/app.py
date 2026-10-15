from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from guardrails_bench.core import Case, DEFAULT_ENDPOINT, active_checks, digest, load_policy
from guardrails_bench.providers import DeciderProvider, load_key

from .scenarios import CHECK_LABELS, catalog
from .auth import CSRF_HEADER, CSRF_VALUE, Sessions, validate_credentials, verify_password

logger = logging.getLogger("guardrails.demo")


@dataclass(frozen=True)
class Settings:
    username: str
    password_hash: str
    cookie_secure: bool = True
    session_ttl: int = 8 * 60 * 60
    login_requests_per_minute: int = 10
    endpoint: str = DEFAULT_ENDPOINT
    key_file: Path = Path("DeciderKey.txt")
    max_concurrency: int = 2
    requests_per_minute: int = 90
    timeout: float = 25

    @classmethod
    def from_env(cls):
        try:
            auth = json.loads(os.environ.get("DEMO_AUTH_CONFIG", "{}"))
            username, password_hash = auth["username"], auth["password_hash"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("DEMO_AUTH_CONFIG must contain username and password_hash") from None
        return cls(
            username=username, password_hash=password_hash,
            cookie_secure=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
            endpoint=os.environ.get("DECIDER_ENDPOINT", DEFAULT_ENDPOINT),
            key_file=Path(os.environ.get("DECIDER_KEY_FILE", "DeciderKey.txt")),
            max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "2")),
            requests_per_minute=int(os.environ.get("REQUESTS_PER_MINUTE", "90")),
            timeout=float(os.environ.get("DECIDER_TIMEOUT", "25")),
        )

    def validate(self):
        validate_credentials(self.username, self.password_hash)
        if self.session_ttl <= 0 or self.login_requests_per_minute < 1:
            raise ValueError("Session duration and login rate must be positive")
        if self.max_concurrency < 1 or self.requests_per_minute < 1:
            raise ValueError("Concurrency and rate limits must be positive")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 28:
            raise ValueError("DECIDER_TIMEOUT must be between 0 and 28 seconds")

    @property
    def cookie_name(self):
        return "__Host-decider-session" if self.cookie_secure else "decider-session"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=12000)
    source: Literal["INPUT", "OUTPUT"] = "INPUT"
    reference: str | None = Field(default=None, max_length=8000)
    query: str | None = Field(default=None, max_length=3000)
    mode: Literal["compact", "detailed"] = "detailed"
    threshold: float = Field(default=0.5, gt=0, lt=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_context(self):
        if sum(len(x or "") for x in (self.content, self.reference, self.query)) > 18000:
            raise ValueError("Combined text cannot exceed 18000 characters")
        if self.source == "INPUT" and (self.reference or self.query):
            raise ValueError("Reference and query apply only to OUTPUT")
        return self


class BoundedBody:
    """Bound actual streamed bytes before JSON parsing, including chunked requests."""

    def __init__(self, app, max_bytes=65536):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        chunks, length = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            length += len(chunk)
            if length > self.max_bytes:
                return await JSONResponse(
                    {"detail": "Request body exceeds 64 KiB", "enforcement_action": "block"},
                    status_code=413,
                )(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None, provider_factory=None):
    policy = load_policy()

    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        config.validate()
        app.state.settings = config
        app.state.semaphore = asyncio.Semaphore(config.max_concurrency)
        app.state.request_times = deque()
        app.state.login_times = deque()
        app.state.sessions = Sessions(config.session_ttl)
        app.state.providers = {}
        # The credential never appears in config/API responses/logs.
        key = load_key(config.key_file) if provider_factory is None else ""
        try:
            for mode in ("compact", "detailed"):
                app.state.providers[mode] = (
                    provider_factory(mode) if provider_factory
                    else DeciderProvider(config.endpoint, key, policy, mode,
                                         concurrency=config.max_concurrency,
                                         timeout=config.timeout, retries=0)
                )
            yield
        finally:
            for provider in app.state.providers.values():
                await provider.close()

    app = FastAPI(title="Decider Guardrails Demo", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BoundedBody)

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def csrf_protected(request: Request):
        # Browsers cannot send this header cross-origin without a successful CORS
        # preflight. This app grants no cross-origin access; SameSite is additional.
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(status_code=403, detail="无效的请求来源，请刷新页面重试。")

    async def authenticated(request: Request):
        state = request.app.state
        token = request.cookies.get(state.settings.cookie_name, "")
        session = state.sessions.get(token)
        if not session:
            raise HTTPException(status_code=401, detail="请先登录，或重新登录已过期的会话。")
        return session

    @app.get("/healthz")
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "decider-guardrails"}

    @app.get("/api/config", dependencies=[Depends(authenticated)])
    async def config():
        return {
            "policy_version": policy["version"], "policy_sha256": digest(policy),
            "checks": [{"id": key, "name": CHECK_LABELS.get(key, key), "policy": value}
                       for key, value in policy["checks"].items()],
            "modes": ["compact", "detailed"], "default_threshold": 0.5,
            "max_content_chars": 12000, "auth_required": True,
            "notice": "这是模型判定演示。预期标签用于对照，实际结果可能误报或漏报。",
        }

    @app.get("/api/scenarios", dependencies=[Depends(authenticated)])
    async def scenarios():
        return {"scenarios": catalog(), "source": "fixed_synthetic_fixtures"}

    @app.post("/api/login", dependencies=[Depends(csrf_protected)])
    async def login(body: LoginRequest, request: Request, response: Response):
        state = request.app.state
        config = state.settings
        now = time.monotonic()
        while state.login_times and state.login_times[0] <= now - 60:
            state.login_times.popleft()
        if len(state.login_times) >= config.login_requests_per_minute:
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请一分钟后再试。",
                                headers={"Retry-After": "60"})
        state.login_times.append(now)
        password_ok = await asyncio.to_thread(verify_password, body.password, config.password_hash)
        username_ok = hmac.compare_digest(body.username.encode(), config.username.encode())
        if not password_ok or not username_ok:
            raise HTTPException(status_code=401, detail="用户名或密码错误。")
        state.sessions.revoke(request.cookies.get(config.cookie_name, ""))
        token = state.sessions.create(config.username)
        response.set_cookie(config.cookie_name, token, max_age=config.session_ttl,
                            httponly=True, secure=config.cookie_secure, samesite="strict", path="/")
        return {"authenticated": True, "username": config.username}

    @app.get("/api/session")
    async def session(current=Depends(authenticated)):
        return {"authenticated": True, "username": current.username}

    @app.post("/api/logout", dependencies=[Depends(csrf_protected)])
    async def logout(request: Request, response: Response):
        state = request.app.state
        config = state.settings
        state.sessions.revoke(request.cookies.get(config.cookie_name, ""))
        response.delete_cookie(config.cookie_name, httponly=True, secure=config.cookie_secure,
                               samesite="strict", path="/")
        return {"authenticated": False}

    @app.post("/api/evaluate", dependencies=[Depends(authenticated), Depends(csrf_protected)])
    async def evaluate(body: EvaluateRequest, request: Request):
        state = request.app.state
        now = time.monotonic()
        while state.request_times and state.request_times[0] <= now - 60:
            state.request_times.popleft()
        if len(state.request_times) >= state.settings.requests_per_minute:
            raise HTTPException(status_code=429, detail="调用较频繁，请稍后再试。",
                                headers={"Retry-After": "60"})
        if state.semaphore.locked():
            raise HTTPException(status_code=429, detail="当前演示正在处理其他请求，请稍后重试。",
                                headers={"Retry-After": "2"})
        # No await between checking capacity and acquiring an available permit.
        await state.semaphore.acquire()
        state.request_times.append(now)
        request_id = uuid.uuid4().hex
        case = Case(id="interactive", category="pii", language="mixed",
                    content=body.content, expected="allow",
                    source=body.source, reference=body.reference or None, query=body.query or None)
        try:
            try:
                result = await asyncio.wait_for(
                    state.providers[body.mode].evaluate(case),
                    timeout=state.settings.timeout + 1,
                )
            except (TimeoutError, asyncio.TimeoutError):
                return JSONResponse({
                    "request_id": request_id, "status": "error", "enforcement_action": "block",
                    "error": "上游响应超时；本次未获得模型判断。", "code": "UPSTREAM_TIMEOUT",
                }, status_code=504)
            if not result.ok:
                logger.warning("request_id=%s upstream_failure=%s", request_id,
                               result.error.split(":")[0] if result.error else "unknown")
                return JSONResponse({
                    "request_id": request_id, "status": "error", "enforcement_action": "block",
                    "error": "模型服务暂不可用；本次未获得模型判断。",
                    "code": "UPSTREAM_UNAVAILABLE",
                }, status_code=502)
            checks = active_checks(case, policy)
            decision = "block" if result.score >= body.threshold else "allow"
            logger.info("request_id=%s mode=%s decision=%s latency_ms=%.2f",
                        request_id, body.mode, decision, result.latency_ms)
            return {
                "request_id": request_id, "status": "ok", "decision": decision,
                "enforcement_action": decision, "mode": body.mode, "threshold": body.threshold,
                "score": result.score, "latency_ms": result.latency_ms,
                "server_ms": result.server_ms, "model": result.model, "usage": result.usage,
                "policy_version": policy["version"], "policy_sha256": digest(policy),
                "active_checks": list(checks),
                "checks": [{
                    "id": key, "name": CHECK_LABELS.get(key, "综合策略"),
                    "score": score, "triggered": score >= body.threshold,
                    "confidence": result.confidences.get(key),
                    "policy": checks.get(key, "检查候选内容是否违反任一适用策略。"),
                } for key, score in result.scores.items()],
                "score_note": ("各项违规概率的最大值，不代表校准后的整体违规概率。"
                               if body.mode == "detailed" else "模型返回的 block 选项概率。"),
            }
        finally:
            state.semaphore.release()

    return app


app = create_app()
