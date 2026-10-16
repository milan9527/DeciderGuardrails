"""Apply a frozen Decider profile to stdin, without persisting candidate content."""
import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.core import DEFAULT_ENDPOINT, load_policy
from guardrails_bench.optimization import OptimizedGuardrail
from guardrails_bench.providers import load_key


async def run(args, content):
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    guardrail = OptimizedGuardrail(
        args.endpoint, load_key(args.key_file), load_policy(), profile, concurrency=1, retries=0,
    )
    try:
        started = time.perf_counter()
        decision = await guardrail.evaluate(
            content, source=args.source,
            reference=args.reference.read_text(encoding="utf-8") if args.reference else None,
            query=args.query,
        )
        output = asdict(decision)
        output["total_latency_ms"] = (time.perf_counter() - started) * 1000
        # Raw server output is available in evaluation journals; avoid echoing it here.
        output["inference"].pop("raw")
        output["thresholds"] = profile["thresholds"]
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if decision.error_action else 0
    finally:
        await guardrail.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True,
                        help="Explicit rules-only, focused, or experimental calibrated profile")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--key-file", type=Path, default=ROOT / "DeciderKey.txt")
    parser.add_argument("--source", choices=["INPUT", "OUTPUT"], default="INPUT")
    parser.add_argument("--reference", type=Path, help="UTF-8 evidence file for OUTPUT")
    parser.add_argument("--query", help="User query for OUTPUT relevance")
    args = parser.parse_args()
    content = sys.stdin.read()
    if not content.strip():
        parser.error("Read nonempty candidate content from stdin")
    if args.source == "INPUT" and (args.reference or args.query):
        parser.error("reference/query require --source OUTPUT")
    raise SystemExit(asyncio.run(run(args, content)))


if __name__ == "__main__":
    main()
