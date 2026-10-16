"""Measure Decider and frozen Bedrock policies on identical cases in one run."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.core import DEFAULT_ENDPOINT, load_cases, load_policy
from guardrails_bench.providers import BedrockProvider, DeciderProvider, load_key
from guardrails_bench.report import generate_report
from guardrails_bench.runner import benchmark, create_run, evaluate, finish_run, write_json


async def measure(args, state):
    policy = load_policy()
    cases = load_cases(None, policy, "test")
    key = load_key(ROOT / "DeciderKey.txt")
    route = state["routes"]["base"]
    modes = ["managed", "compact", "detailed"]

    def factory(mode, concurrency):
        if mode == "managed":
            return BedrockProvider(route["guardrail_id"], route["version"], state["region"],
                                   concurrency, 30, 0, routes=state["routes"])
        return DeciderProvider(DEFAULT_ENDPOINT, key, policy, mode, concurrency, 30, 0)

    output = args.config.parent
    pointer = output / "comparison-runs.json"
    runs = json.loads(pointer.read_text()) if pointer.exists() else {}
    config = {
        "provider": "comparison", "modes": modes, "threshold": 0.5,
        "error_action": "block", "region": state["region"], "endpoint": DEFAULT_ENDPOINT,
        "retries": 0, "seed": 42, "warmup": 2,
        "bedrock_routes": state["routes"], "bedrock_policy_sha256": state["policy_sha256"],
        "context_placeholders": {
            "grounding_only_query": "Summarize the information in the reference.",
            "relevance_only_reference": "No reference is supplied. Only evaluate relevance to the query.",
            "selection": "Only OUTPUT/reference/query presence, never category or gold labels.",
        },
    }
    for kind in (["eval", "bench"] if args.stage == "both" else [args.stage]):
        selected = cases if kind == "eval" else [c for c in cases if c.source == "INPUT"]
        run_config = {**config, "concurrency": 4 if kind == "eval" else [1, 4, 8]}
        if kind == "bench":
            run_config.update(lengths=[256, 2048, 8192], requests=args.requests, cache="unique",
                              scope="INPUT-only performance; contextual latency is reported separately from quality cases")
        run = create_run(output, kind, run_config, policy, selected)
        write_json(run / "bedrock-guardrails.json", state)
        runs[kind] = str(run)
        write_json(pointer, runs)
        print("Run:", run, flush=True)
        try:
            if kind == "eval":
                await evaluate(run, selected, modes, factory, 4, 2, 42)
            else:
                await benchmark(run, selected, modes, factory, [1, 4, 8], [256, 2048, 8192],
                                args.requests, 2, 42, "unique")
            summary = generate_report(run)
            for name, item in summary.get("quality", {}).items():
                q = item["overall"]
                print(name, "accuracy", q["accuracy"], "FP", q["fp"], "FN", q["fn"],
                      "errors", q["errors"], flush=True)
        except BaseException as exc:
            finish_run(run, "failed", failure_type=type(exc).__name__)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=["eval", "bench", "both"], default="both")
    parser.add_argument("--requests", type=int, default=30)
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be positive")
    state = json.loads(args.config.read_text())
    if any(not r.get("version") or r["version"] == "DRAFT" for r in state["routes"].values()):
        parser.error("All Bedrock routes must use frozen numeric versions")
    asyncio.run(measure(args, state))


if __name__ == "__main__":
    main()
