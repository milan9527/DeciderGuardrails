"""Verify a deployed or local demo, then optionally run the fixed scenario suite."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.metrics import confusion, distribution
from demo_api.auth import CSRF_HEADER, CSRF_VALUE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--credentials-file", type=Path, default=ROOT / ".deployment/demo-login.json")
    parser.add_argument("--scenarios", action="store_true")
    parser.add_argument("--modes", nargs="+", choices=["compact", "detailed"], default=["compact", "detailed"])
    parser.add_argument("--output", type=Path, default=ROOT / ".deployment/scenario-results.json")
    args = parser.parse_args()
    state_path = ROOT / ".deployment/deployment.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    base = (args.base_url or state.get("WebsiteUrl", "")).rstrip("/")
    url = urlsplit(base)
    if not url.hostname or (url.scheme != "https" and url.hostname not in {"127.0.0.1", "localhost"}):
        raise ValueError("Use HTTPS or localhost for --base-url")
    login = json.loads(args.credentials_file.read_text())
    headers = {CSRF_HEADER: CSRF_VALUE}
    checks = []
    report = {"created_at": datetime.now(timezone.utc).isoformat(),
              "base_url": base, "checks": checks, "rows": [], "summary": {}}
    with httpx.Client(base_url=base, timeout=40, follow_redirects=False) as client:
        for attempt in range(12):
            try:
                response = client.get("/api/health")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(5)
        else:
            raise RuntimeError("API did not become healthy")
        assert response.json()["status"] == "ok"
        checks.append("API health")
        assert client.post("/api/evaluate", json={"content": "hello"}).status_code == 401
        checks.append("Unauthenticated inference denied")
        for path in ("/api/config", "/api/scenarios"):
            assert client.get(path).status_code == 401
        checks.append("Workspace data requires login")
        authenticated = client.post("/api/login", headers=headers, json=login)
        assert authenticated.status_code == 200
        assert "HttpOnly" in authenticated.headers["set-cookie"]
        assert client.get("/api/session").json()["username"] == login["username"]
        checks.append("Username/password login with HttpOnly session")
        assert client.post("/api/evaluate", json={"content": "hello"}).status_code == 403
        checks.append("Authenticated writes require CSRF header")
        missing = client.get("/api/not-a-route")
        assert missing.status_code == 404 and "application/json" in missing.headers["content-type"]
        checks.append("API 404 remains JSON, no SPA masking")
        response = client.post("/api/evaluate", headers=headers,
                               json={"content": "hello", "expected": "allow"})
        assert response.status_code == 422
        checks.append("Client-supplied gold labels rejected")
        catalog = client.get("/api/scenarios").json()["scenarios"]
        assert len(catalog) == 8 and sum(len(s["cases"]) for s in catalog) == 32
        checks.append("Eight scenarios / 32 examples available")
        if url.hostname not in {"127.0.0.1", "localhost"}:
            frontend = client.get("/")
            assert frontend.status_code == 200 and 'id="root"' in frontend.text
            assert "content-security-policy" in frontend.headers
            assert login["password"] not in frontend.text
            checks.append("CloudFront static frontend + security headers")
            if state.get("WebsiteUrl") == base:
                s3 = client.get(f"https://{state['FrontendBucket']}.s3.{state['region']}.amazonaws.com/index.html")
                assert s3.status_code == 403
                checks.append("Anonymous direct S3 access denied")
        if args.scenarios:
            for mode in args.modes:
                rows = []
                for scene in catalog:
                    for case in scene["cases"]:
                        body = {k: case[k] for k in ("content", "source")}
                        body.update(mode=mode, threshold=0.5)
                        for key in ("reference", "query"):
                            if case.get(key):
                                body[key] = case[key]
                        started = time.perf_counter()
                        response = client.post("/api/evaluate", json=body, headers=headers)
                        api_latency_ms = (time.perf_counter() - started) * 1000
                        row = {"scenario": scene["id"], "case_id": case["id"],
                               "expected": case["expected"], "mode": mode,
                               "http_status": response.status_code,
                               "api_latency_ms": api_latency_ms, "result": response.json()}
                        rows.append(row)
                        report["rows"].append(row)
                        print(mode, case["id"], response.status_code,
                              row["result"].get("decision", "ERROR"), flush=True)
                valid = [r for r in rows if r["http_status"] == 200]
                report["summary"][mode] = {
                    **confusion([(r["expected"] == "block", r["result"]["decision"] == "block")
                                 for r in valid]),
                    "errors": len(rows) - len(valid), "total": len(rows),
                    "latency_ms": distribution([r["result"]["latency_ms"] for r in valid]),
                    "api_latency_ms": distribution([r["api_latency_ms"] for r in valid]),
                }
        captured_cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)
        assert client.post("/api/logout", headers=headers).status_code == 200
        assert client.get("/api/session", headers={"Cookie": captured_cookies}).status_code == 401
        checks.append("Logout revokes the captured session")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("Infrastructure/API checks passed:", ", ".join(checks))
    print("Scenario report:", args.output)
    if any(s["errors"] for s in report["summary"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
