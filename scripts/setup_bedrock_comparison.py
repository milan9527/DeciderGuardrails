"""Create isolated, versioned Bedrock policies; never modify existing guardrails."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.core import digest
from guardrails_bench.runner import write_json


def wait_ready(client, identifier, version="DRAFT"):
    for _ in range(60):
        result = client.get_guardrail(guardrailIdentifier=identifier, guardrailVersion=version)
        if result["status"] == "READY":
            return {k: v for k, v in result.items() if k != "ResponseMetadata"}
        if result["status"] == "FAILED":
            raise RuntimeError("Guardrail creation failed: " + str(result.get("statusReasons")))
        time.sleep(2)
    raise RuntimeError("Guardrail did not become ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--account", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.account:
        raise ValueError("AWS account mismatch")
    template = json.loads((ROOT / "benchmarks/bedrock_policy.json").read_text())
    path = args.output / "bedrock-guardrails.json"
    args.output.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text()) if path.exists() else {
        "region": args.region, "account": account, "policy_sha256": digest(template),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prefix": "decider-compare-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "routes": {}, "request_configs": {}, "guardrails": {},
    }
    if (state["account"], state["region"], state["policy_sha256"]) != (account, args.region, digest(template)):
        raise ValueError("Existing comparison state does not match account/region/policy")
    client = session.client("bedrock")
    for route, filters in (("base", []), ("grounding", ["GROUNDING"]), ("relevance", ["RELEVANCE"])):
        if state["routes"].get(route, {}).get("version"):
            continue
        config = {**template, "name": state["prefix"] + "-" + route,
                  "tags": [{"key": "Project", "value": "DeciderGuardrails"},
                           {"key": "Purpose", "value": "isolated-comparison"}]}
        if filters:
            config["contextualGroundingPolicyConfig"] = {
                "filtersConfig": [{"type": name, "threshold": 0.7} for name in filters],
            }
        if route not in state["routes"]:
            created = client.create_guardrail(**config)
            state["routes"][route] = {"guardrail_id": created["guardrailId"]}
            state["request_configs"][route] = config
            write_json(path, state)
            print("Created:", route, created["guardrailId"], flush=True)
        identifier = state["routes"][route]["guardrail_id"]
        wait_ready(client, identifier)
        version = client.create_guardrail_version(
            guardrailIdentifier=identifier, description="Frozen before comparison test evaluation",
        )["version"]
        state["routes"][route]["version"] = version
        snapshot = wait_ready(client, identifier, version)
        state["guardrails"][route] = json.loads(json.dumps(snapshot, default=str))
        write_json(path, state)
        print("Frozen:", route, identifier, "version", version, flush=True)
    print("Configuration:", path, flush=True)


if __name__ == "__main__":
    main()
