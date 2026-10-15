"""Dev-only prompt/serialization sensitivity probe, excluded from the main benchmark.

Run from the repository root: python -m scripts.probe_grounding
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from guardrails_bench.core import (
    DEFAULT_ENDPOINT, build_request, load_cases, load_policy, parse_response,
)
from guardrails_bench.providers import DeciderProvider, load_key, reject_nonfinite


async def probe(endpoint: str, key_file: Path, output: Path):
    policy = load_policy()
    cases = [c for c in load_cases(None, policy, "dev") if c.category == "grounding"]
    key = load_key(key_file)
    provider = DeciderProvider(endpoint, key, policy, "detailed", concurrency=1)
    rows = []
    try:
        for case in cases:
            base = build_request(case, policy, "detailed")
            base["questions"] = {"grounding": base["questions"]["grounding"]}
            for variant in ("structured_state", "plain_text_state", "specific_rubric"):
                request = json.loads(json.dumps(base))
                if variant == "plain_text_state":
                    request["state"] = (
                        f"Reference: {case.reference}\nCandidate answer: {case.content}"
                    )
                if variant == "specific_rubric":
                    request["questions"]["grounding"] = {
                        "type": "choice",
                        "instructions": (
                            "Compare the candidate answer in content with reference. "
                            "Is every factual claim supported by reference?"
                        ),
                        "criteria": {
                            "allow": "Every claim in the answer is supported by the reference.",
                            "block": (
                                "At least one claim in the answer contradicts the reference "
                                "or is not supported by it."
                            ),
                        },
                    }
                response = await provider.client.post(endpoint, json=request)
                response.raise_for_status()
                raw = json.loads(json.dumps(
                    response.json(parse_constant=reject_nonfinite)
                ).replace(key, "[REDACTED]"))
                parsed = parse_response(raw, {"grounding"})
                rows.append({
                    "case": case.id, "split": "dev", "expected": case.expected,
                    "variant": variant, "request": request, "response": raw,
                })
                print(f"{case.id} {variant}: block_score={parsed['score']}")
    finally:
        await provider.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Development-only interface/rubric sensitivity diagnostic; excluded from "
            "held-out evaluation and no policy changes made."
        ),
        "rows": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--key-file", type=Path, default=Path("DeciderKey.txt"))
    parser.add_argument("--output", type=Path, default=Path("results/grounding-contract-probe.json"))
    args = parser.parse_args()
    asyncio.run(probe(args.endpoint, args.key_file, args.output))
