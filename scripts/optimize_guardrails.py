"""Freeze a dev-selected profile, validate it, and measure matched vLLM latency."""
import argparse
import asyncio
import json
import random
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.core import DEFAULT_ENDPOINT, digest, load_cases, load_policy
from guardrails_bench.metrics import distribution
from guardrails_bench.optimization import (
    PROMPT_VERSION, FocusedProvider, calibrate, decide, decision_metrics, uniform_thresholds,
)
from guardrails_bench.providers import DeciderProvider, load_key
from guardrails_bench.runner import benchmark, create_run, run_batch, warmup, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


def ablations(baseline, focused, thresholds, policy):
    uniform = uniform_thresholds(policy)
    return {
        "baseline": decision_metrics(baseline, uniform, rules=False),
        "rules_only": decision_metrics(baseline, uniform),
        "focused": decision_metrics(focused, uniform),
        "calibrated": decision_metrics(focused, thresholds),
    }


async def collect(out, cases, stage, factory):
    """Alternate provider order in fixed-size blocks to reduce endpoint drift."""
    path = out / f"{stage}.jsonl"
    if path.exists():
        raise ValueError(f"Refusing to append another experiment to {path}")
    jobs = list(cases)
    random.Random(73).shuffle(jobs)
    providers = {mode: factory(mode, 4) for mode in ("detailed", "focused")}
    rows = {mode: [] for mode in providers}
    try:
        for provider in providers.values():
            await warmup(provider, jobs, 2, out)
        for start in range(0, len(jobs), 24):
            modes = list(providers)
            if (start // 24) % 2:
                modes.reverse()
            for mode in modes:
                batch, _ = await run_batch(
                    providers[mode], jobs[start:start + 24], 4, path,
                    extra={"stage": stage, "block_index": start // 24},
                )
                rows[mode].extend(batch)
            print(f"{stage}: {min(start + 24, len(jobs))}/{len(jobs)} paired cases", flush=True)
    finally:
        for provider in providers.values():
            await provider.close()
    return rows


def validate_snapshots(out, datasets, policy):
    protocol = json.loads((out / "protocol.json").read_text())
    if protocol["policy_sha256"] != digest(policy):
        raise ValueError("Policy changed since protocol freeze")
    for name, cases in datasets.items():
        if protocol["datasets"][name]["sha256"] != digest([asdict(c) for c in cases]):
            raise ValueError(f"{name} changed since protocol freeze")


async def run(args):
    policy = load_policy()
    datasets = {
        "dev": load_cases(None, policy, "dev") + load_cases(
            ROOT / "benchmarks/optimization-dev.jsonl", policy, "dev"),
        "regression": load_cases(None, policy, "test"),
        "holdout": load_cases(ROOT / "benchmarks/optimization-holdout.jsonl", policy, "test"),
    }
    out = args.output
    key = load_key(ROOT / "DeciderKey.txt")

    def factory(mode, concurrency):
        kwargs = dict(concurrency=concurrency, timeout=60, retries=0)
        if mode == "focused":
            return FocusedProvider(DEFAULT_ENDPOINT, key, policy, **kwargs)
        return DeciderProvider(DEFAULT_ENDPOINT, key, policy, "detailed", **kwargs)

    if args.stage in ("all", "dev"):
        out.mkdir(parents=True, exist_ok=True)
        if (out / "protocol.json").exists():
            raise ValueError("Use a new output directory for a new optimization experiment")
        write_json(out / "protocol.json", {
            "created_at": now(), "endpoint": DEFAULT_ENDPOINT, "seed": 73,
            "concurrency": 4, "retries": 0, "warmups_per_mode_per_stage": 2,
            "prompt_version": PROMPT_VERSION, "policy_sha256": digest(policy),
            "datasets": {name: {"n": len(cases), "sha256": digest([asdict(c) for c in cases])}
                         for name, cases in datasets.items()},
            "calibration": "Per-check F2 on dev; FP <= same check at 0.5; grid 0.10..0.90 step 0.05",
            "selection": "Prefer calibrated over focused only if dev FP does not rise and "
                         "dev (recall, -FP) strictly improves. Otherwise use focused at 0.5.",
            "limitations": "Authored bilingual synthetic data; old test is regression. "
                          "New holdout fixed before calls, but not externally sourced or independent.",
            "performance": {"concurrencies": [1, 4], "lengths": [256, 8192],
                            "requests_per_cell": args.requests, "warmups": 2},
        })
        write_json(out / "policy.json", policy)
        for name, cases in datasets.items():
            write_json(out / f"{name}-dataset.json", [asdict(c) for c in cases])
        rows = await collect(out, datasets["dev"], "dev", factory)
        thresholds, audit = calibrate(rows["focused"], policy)
        metrics = ablations(rows["detailed"], rows["focused"], thresholds, policy)
        focused, calibrated = metrics["focused"], metrics["calibrated"]
        use_calibrated = calibrated["fp"] <= focused["fp"] and (
            calibrated["recall"], -calibrated["fp"]) > (focused["recall"], -focused["fp"])
        selected = "calibrated" if use_calibrated else "focused"
        if not use_calibrated:
            thresholds = uniform_thresholds(policy)
        profile = {
            "schema_version": 1, "frozen_at": now(), "prompt_version": PROMPT_VERSION,
            "policy_sha256": digest(policy), "dev_sha256": digest([asdict(c) for c in datasets["dev"]]),
            "selected": selected, "thresholds": thresholds,
            "error_action": "block", "literal_terms": ["PROJECT_ORCHID", "绝密代号"],
        }
        write_json(out / "calibration.json", audit)
        write_json(out / "dev-summary.json", metrics)
        write_json(out / "profile.json", profile)
        print("Frozen profile:", json.dumps(profile, ensure_ascii=False), flush=True)
        for name, q in metrics.items():
            print("dev", name, "accuracy", q["accuracy"], "recall", q["recall"],
                  "FP/FN", q["fp"], q["fn"], flush=True)
    validate_snapshots(out, datasets, policy)
    profile = json.loads((out / "profile.json").read_text())
    if args.stage in ("all", "validation"):
        if any((out / f"{name}.jsonl").exists() for name in ("regression", "holdout")):
            raise ValueError("Validation already started; preserve it and use a fresh experiment")
        write_json(out / "validation-freeze.json", {"started_at": now(), "profile_sha256": digest(profile)})
        summary = {}
        for stage in ("regression", "holdout"):
            rows = await collect(out, datasets[stage], stage, factory)
            q = ablations(rows["detailed"], rows["focused"], profile["thresholds"], policy)
            q["selected"] = profile["selected"]
            q["models"] = sorted({r["model"] for batch in rows.values() for r in batch if r["model"]})
            q["latency_ms"] = {mode: distribution([r["latency_ms"] for r in batch if r["ok"]])
                               for mode, batch in rows.items()}
            q["by_category"] = {
                category: ablations(
                    [r for r in rows["detailed"] if r["category"] == category],
                    [r for r in rows["focused"] if r["category"] == category],
                    profile["thresholds"], policy,
                ) for category in policy["checks"]
            }
            q["by_language"] = {
                language: ablations(
                    [r for r in rows["detailed"] if r["language"] == language],
                    [r for r in rows["focused"] if r["language"] == language],
                    profile["thresholds"], policy,
                ) for language in ("en", "zh")
            }
            summary[stage] = q
            with (out / f"{stage}-decisions.jsonl").open("w") as stream:
                for row in rows["focused"]:
                    decision = decide(row, profile["thresholds"])
                    stream.write(json.dumps(
                        {"id": row["id"], "expected": row["expected"], "category": row["category"],
                         "scores": row["scores"], **decision}, ensure_ascii=False
                    ) + "\n")
            for name in ("baseline", "rules_only", "focused", "calibrated"):
                m = q[name]
                print(stage, name, "accuracy", m["accuracy"], "recall", m["recall"],
                      "FP/FN", m["fp"], m["fn"], "errors", m["errors"], flush=True)
            write_json(out / "validation-summary.json", summary)
        if digest(json.loads((out / "profile.json").read_text())) != digest(profile):
            raise ValueError("Profile changed during validation")
    if args.stage in ("all", "bench"):
        cases = [c for c in datasets["regression"] if c.source == "INPUT"]
        protocol = json.loads((out / "protocol.json").read_text())
        requests = protocol["performance"]["requests_per_cell"]
        run_path = create_run(out, "bench", protocol["performance"], policy, cases)
        write_json(out / "benchmark-run.json", {"path": str(run_path), "profile_sha256": digest(profile)})
        await benchmark(run_path, cases, ["detailed", "focused"], factory, [1, 4], [256, 8192],
                        requests, 2, 73, "unique")
    print("Artifacts:", out, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["all", "dev", "validation", "bench"], default="all")
    parser.add_argument("--requests", type=int, default=30)
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
