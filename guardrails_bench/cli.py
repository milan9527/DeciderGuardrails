from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

from .core import DEFAULT_ENDPOINT, load_cases, load_policy
from .providers import BedrockProvider, DeciderProvider, load_key
from .report import generate_report
from .runner import benchmark, create_run, evaluate, finish_run


def positive(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def nonnegative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return n


def probability(value: str) -> float:
    n = float(value)
    if not 0 < n < 1:
        raise argparse.ArgumentTypeError("must be strictly between 0 and 1")
    return n


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Evaluate Strands Decider as a guardrail.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("eval", "bench"):
        command = commands.add_parser(name, help="Quality evaluation" if name == "eval" else "Performance matrix")
        command.add_argument("--provider", choices=["decider", "bedrock"], default="decider")
        command.add_argument("--endpoint", default=os.environ.get("DECIDER_ENDPOINT", DEFAULT_ENDPOINT))
        command.add_argument("--key-file", type=Path, default=Path("DeciderKey.txt"))
        command.add_argument("--policy", type=Path)
        command.add_argument("--dataset", type=Path)
        command.add_argument("--split", choices=["dev", "test", "all"], default="test")
        command.add_argument("--modes", nargs="+", choices=["compact", "detailed"], default=["compact", "detailed"])
        command.add_argument("--timeout", type=positive, default=30)
        command.add_argument("--retries", type=nonnegative, default=0)
        command.add_argument("--warmup", type=nonnegative, default=2)
        command.add_argument("--seed", type=int, default=42)
        command.add_argument("--output", type=Path, default=Path("results"))
        command.add_argument("--guardrail-id", default=os.environ.get("BEDROCK_GUARDRAIL_ID"))
        command.add_argument("--guardrail-version", default=os.environ.get("BEDROCK_GUARDRAIL_VERSION"))
        command.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
        if name == "eval":
            command.add_argument("--concurrency", type=positive, default=4)
            command.add_argument("--threshold", type=probability, default=0.5)
            command.add_argument("--error-action", choices=["block", "allow"], default="block")
        else:
            command.add_argument("--concurrency", nargs="+", type=positive, default=[1, 4, 8])
            command.add_argument("--lengths", nargs="+", type=positive, default=[256, 2048, 8192])
            command.add_argument("--requests", type=positive, default=30, help="Requests per matrix cell")
            command.add_argument("--cache", choices=["unique", "repeat"], default="unique")
    check = commands.add_parser("validate", help="Validate dataset and policy without calling an API")
    check.add_argument("--policy", type=Path)
    check.add_argument("--dataset", type=Path)
    report = commands.add_parser("report", help="Rebuild an offline report from saved results")
    report.add_argument("run", type=Path)
    report.add_argument("--threshold", type=probability)
    return root


def main():
    args = parser().parse_args()
    run = None
    try:
        if args.command == "report":
            generate_report(args.run, args.threshold)
            print(args.run / "report.html")
            return
        policy = load_policy(args.policy)
        cases = load_cases(args.dataset, policy, getattr(args, "split", "all"))
        if args.command == "validate":
            print(json.dumps({
                "cases": len(cases),
                "by_split": dict(Counter(c.split for c in cases)),
                "by_language": dict(Counter(c.language for c in cases)),
                "by_expected": dict(Counter(c.expected for c in cases)),
                "by_category": dict(Counter(c.category for c in cases)),
            }, ensure_ascii=False, indent=2))
            return
        modes = list(dict.fromkeys(args.modes))
        if args.provider == "decider":
            key = load_key(args.key_file)
            def factory(mode, concurrency):
                return DeciderProvider(args.endpoint, key, policy, mode, concurrency,
                                       args.timeout, args.retries)
        else:
            if not all((args.guardrail_id, args.guardrail_version, args.region)):
                raise ValueError("Bedrock needs --guardrail-id, --guardrail-version and --region")
            modes = ["managed"]
            def factory(mode, concurrency):
                return BedrockProvider(args.guardrail_id, args.guardrail_version, args.region,
                                       concurrency, args.timeout, args.retries)
        config = {
            k: str(v) if isinstance(v, Path) else v
            for k, v in vars(args).items() if k != "key_file"
        }
        config["modes"] = modes
        if args.provider == "bedrock":
            config.pop("endpoint", None)
        run = create_run(args.output, args.command, config, policy, cases)
        print(f"Run directory: {run}", flush=True)
        if args.command == "eval":
            asyncio.run(evaluate(run, cases, modes, factory, args.concurrency, args.warmup, args.seed))
        else:
            asyncio.run(benchmark(
                run, cases, modes, factory, list(dict.fromkeys(args.concurrency)),
                list(dict.fromkeys(args.lengths)), args.requests, args.warmup, args.seed, args.cache,
            ))
        summary = generate_report(run)
        print(f"HTML: {run / 'report.html'}\nMarkdown: {run / 'report.md'}", flush=True)
        if "quality" in summary:
            for name, item in summary["quality"].items():
                q = item["overall"]
                print(f"{name}: accuracy={q['accuracy']}, recall={q['recall']}, "
                      f"FP={q['fp']}, FN={q['fn']}, errors={q['errors']}")
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        if run is not None:
            finish_run(run, "failed", failure_type=type(exc).__name__)
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        if run is not None:
            finish_run(run, "interrupted")
        print("Interrupted; completed request records are saved.", file=sys.stderr)
        raise SystemExit(130) from None
