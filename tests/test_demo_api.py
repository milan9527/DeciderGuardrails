import asyncio

from fastapi.testclient import TestClient

from demo_api.app import Settings, create_app
from demo_api.auth import CSRF_HEADER, CSRF_VALUE, hash_password
from demo_api.scenarios import catalog
from guardrails_bench.core import Result

PASSWORD = "test-password-long-enough"
PASSWORD_HASH = hash_password(PASSWORD)


class FakeProvider:
    def __init__(self, mode, result=None):
        self.mode, self.calls = mode, []
        self.result = result or Result(
            True, 123.4, score=0.8, scores={"pii": 0.8, "hate": 0.02},
            confidences={"pii": 0.6}, model="test-model", server_ms=110,
            usage={"input_tokens": 100},
        )

    async def evaluate(self, case):
        self.calls.append(case)
        return self.result

    async def close(self):
        pass


def client_with(provider=None, **settings):
    fake = provider or FakeProvider("detailed")
    return TestClient(create_app(Settings("admin", PASSWORD_HASH, **settings), lambda mode: fake), base_url="https://testserver"), fake


def auth(client):
    headers = {CSRF_HEADER: CSRF_VALUE}
    if not client.cookies.get("__Host-decider-session"):
        response = client.post("/api/login", headers=headers,
                               json={"username": "admin", "password": PASSWORD})
        assert response.status_code == 200
    return headers


def test_catalog_contains_real_balanced_bilingual_examples():
    scenarios = catalog()
    assert len(scenarios) == 8
    for scenario in scenarios:
        assert len(scenario["cases"]) == 4
        assert {c["language"] for c in scenario["cases"]} == {"en", "zh"}
        assert sum(c["expected"] == "block" for c in scenario["cases"]) == 2


def test_unauthorized_cannot_read_workspace_or_call_model():
    client, fake = client_with()
    with client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/scenarios").status_code == 401
        assert client.get("/api/session").status_code == 401
        assert client.post("/api/evaluate", json={"content": "hello"}).status_code == 401
        assert client.post("/api/evaluate", headers={"Authorization": "Bearer old-demo-token"},
                           json={"content": "hello"}).status_code == 401
        assert not fake.calls
        auth(client)
        config = client.get("/api/config")
        assert config.status_code == 200 and "endpoint" not in config.json()
        assert PASSWORD not in config.text and PASSWORD_HASH not in config.text
        assert client.get("/api/scenarios").status_code == 200


def test_login_errors_do_not_identify_accounts_or_create_sessions():
    client, fake = client_with()
    with client:
        headers = {CSRF_HEADER: CSRF_VALUE}
        wrong_user = client.post("/api/login", headers=headers,
                                 json={"username": "unknown", "password": PASSWORD})
        wrong_password = client.post("/api/login", headers=headers,
                                     json={"username": "admin", "password": "wrong"})
        assert wrong_user.status_code == wrong_password.status_code == 401
        assert wrong_user.json() == wrong_password.json()
        assert "set-cookie" not in wrong_password.headers
        assert not fake.calls


def test_secure_cookie_login_and_logout_revoke_captured_session():
    client, _ = client_with()
    with client:
        response = client.post("/api/login", headers={CSRF_HEADER: CSRF_VALUE},
                               json={"username": "admin", "password": PASSWORD})
        assert response.json() == {"authenticated": True, "username": "admin"}
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert "Max-Age=28800" in cookie and "Path=/" in cookie and "Domain=" not in cookie
        captured = client.cookies.get("__Host-decider-session")
        assert PASSWORD not in cookie and captured not in response.text
        assert client.get("/api/session").json()["username"] == "admin"
        assert client.post("/api/logout", headers={CSRF_HEADER: CSRF_VALUE}).status_code == 200
        assert client.get("/api/session").status_code == 401
        replay = {"Cookie": "__Host-decider-session=" + captured, CSRF_HEADER: CSRF_VALUE}
        assert client.post("/api/evaluate", headers=replay, json={"content": "hi"}).status_code == 401


def test_expired_and_tampered_sessions_are_rejected():
    client, fake = client_with()
    with client:
        auth(client)
        session = next(iter(client.app.state.sessions.entries.values()))
        session.expires_at = 0
        assert client.get("/api/session").status_code == 401
        assert not client.app.state.sessions.entries
        assert client.get("/api/session", headers={"Cookie": "__Host-decider-session=forged"}).status_code == 401
        assert not fake.calls


def test_csrf_header_required_and_cross_origin_preflight_not_granted():
    client, fake = client_with()
    with client:
        credentials = {"username": "admin", "password": PASSWORD}
        assert client.post("/api/login", json=credentials).status_code == 403
        auth(client)
        assert client.post("/api/evaluate", json={"content": "hi"}).status_code == 403
        assert client.post("/api/logout").status_code == 403
        preflight = client.options("/api/evaluate", headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": CSRF_HEADER,
        })
        assert "access-control-allow-origin" not in preflight.headers
        assert client.get("/api/session").status_code == 200
        assert not fake.calls


def test_login_attempts_are_rate_limited_before_password_verification():
    client, fake = client_with(login_requests_per_minute=1)
    with client:
        headers = {CSRF_HEADER: CSRF_VALUE}
        assert client.post("/api/login", headers=headers,
                           json={"username": "admin", "password": "wrong"}).status_code == 401
        response = client.post("/api/login", headers=headers,
                               json={"username": "admin", "password": PASSWORD})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert not fake.calls


def test_probabilities_drive_decision_and_gold_fields_are_rejected():
    client, fake = client_with()
    with client:
        response = client.post("/api/evaluate", headers=auth(client),
                               json={"content": "hello", "threshold": 0.85})
        assert response.status_code == 200
        assert response.json()["decision"] == "allow"
        assert response.json()["score"] == 0.8
        assert len(response.json()["active_checks"]) == 10
        assert response.headers["cache-control"] == "no-store"
        invalid = client.post("/api/evaluate", headers=auth(client),
                              json={"content": "hello", "expected": "allow"})
        assert invalid.status_code == 422
        assert len(fake.calls) == 1


def test_grounding_context_applies_only_to_outputs():
    client, fake = client_with()
    with client:
        bad = client.post("/api/evaluate", headers=auth(client), json={
            "content": "90 days", "reference": "30 days", "source": "INPUT",
        })
        assert bad.status_code == 422
        good = client.post("/api/evaluate", headers=auth(client), json={
            "content": "90 days", "reference": "30 days", "query": "Returns?", "source": "OUTPUT",
        })
        assert good.status_code == 200
        assert len(good.json()["active_checks"]) == 12
        assert fake.calls[0].reference == "30 days"


def test_upstream_failure_is_not_a_valid_block_prediction():
    fake = FakeProvider("detailed", Result(False, 10, error="HTTP 503"))
    client, _ = client_with(fake)
    with client:
        response = client.post("/api/evaluate", headers=auth(client), json={"content": "Hello"})
        assert response.status_code == 502
        assert response.json()["status"] == "error"
        assert response.json()["enforcement_action"] == "block"
        assert "decision" not in response.json()


def test_rate_limit_and_body_limit():
    client, fake = client_with(requests_per_minute=1)
    with client:
        assert client.post("/api/evaluate", headers=auth(client),
                           json={"content": "Hello"}).status_code == 200
        limited = client.post("/api/evaluate", headers=auth(client), json={"content": "Hi"})
        assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
        too_large = client.post("/api/evaluate", headers=auth(client),
                                content=b'{"content":"' + b"x" * 70000 + b'"}')
        assert too_large.status_code == 413
        assert len(fake.calls) == 1


def test_queue_does_not_accept_more_than_configured_capacity():
    client, fake = client_with(max_concurrency=1)
    with client:
        # Exercise the same exhausted permit state as a concurrent upstream call.
        client.portal.call(client.app.state.semaphore.acquire)
        response = client.post("/api/evaluate", headers=auth(client), json={"content": "Hello"})
        assert response.status_code == 429
        assert not fake.calls
        client.app.state.semaphore.release()


def test_api_timeout_returns_504_and_releases_capacity():
    class SlowProvider(FakeProvider):
        async def evaluate(self, case):
            await asyncio.sleep(2)
    client, _ = client_with(SlowProvider("detailed"), timeout=0.01)
    with client:
        response = client.post("/api/evaluate", headers=auth(client), json={"content": "Hello"})
        assert response.status_code == 504
        assert response.json()["code"] == "UPSTREAM_TIMEOUT"
        assert not client.app.state.semaphore.locked()
