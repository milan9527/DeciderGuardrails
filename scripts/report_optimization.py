"""Rebuild tables and paired error audit from a completed optimization experiment."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.core import digest
from guardrails_bench.optimization import BASELINE_PROMPT_VERSION, decide, uniform_thresholds
from guardrails_bench.runner import write_json


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percent(value):
    return "—" if value is None else f"{100 * value:.1f}%"


def report(out, export_dir=None):
    profile = json.loads((out / "profile.json").read_text())
    freeze = json.loads((out / "validation-freeze.json").read_text())
    if freeze["profile_sha256"] != digest(profile):
        raise ValueError("Profile no longer matches validation freeze")
    policy = json.loads((out / "policy.json").read_text())
    dev = json.loads((out / "dev-summary.json").read_text())
    validation = json.loads((out / "validation-summary.json").read_text())
    pointer = json.loads((out / "benchmark-run.json").read_text())
    benchmark_path = Path(pointer["path"])
    benchmark = json.loads((benchmark_path / "benchmark.json").read_text())
    bench_manifest = json.loads((benchmark_path / "manifest.json").read_text())
    if bench_manifest["status"] != "complete" or len(benchmark) != 8:
        raise ValueError("Performance matrix is incomplete")
    uniform = uniform_thresholds(policy)
    errors, paired = {}, {}
    ablation_audit = {}
    lines = [
        "# Decider 优化实测表",
        "",
        f"冻结配置：`{profile['selected']}` / `{profile['prompt_version']}`；"
        f"冻结时间：{profile['frozen_at']}。",
        "",
        "规则阶段复用原版响应；阈值阶段复用 focused 响应。仅原版和 focused 发起实际请求。",
        "",
        "| 数据 | 配置 | n | 准确率 | 精确率 | 召回率 | FPR | FP | FN | 请求错误 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stage, q in [("dev", dev), *validation.items()]:
        for name in ("baseline", "rules_only", "focused", "calibrated"):
            m = q[name]
            lines.append(
                f"| {stage} | {name} | {m['n']} | {percent(m['accuracy'])} | "
                f"{percent(m['precision'])} | {percent(m['recall'])} | "
                f"{percent(m['false_positive_rate'])} | {m['fp']} | {m['fn']} | {m['errors']} |"
            )
    for stage in validation:
        rows = read_rows(out / f"{stage}.jsonl")
        modes = {mode: {r["id"]: r for r in rows if r["mode"] == mode}
                 for mode in ("detailed", "focused")}
        if set(modes["detailed"]) != set(modes["focused"]):
            raise ValueError("Unpaired validation data")
        errors[stage] = []
        fixed, regressed = [], []
        for case_id, row in modes["focused"].items():
            base = modes["detailed"][case_id]
            decision = decide(row, profile["thresholds"])
            baseline = decide(base, uniform, rules=False)
            correct = decision["action"] == row["expected"]
            previously_correct = baseline["action"] == base["expected"]
            if not row["ok"] or not base["ok"]:
                continue
            if correct and not previously_correct:
                fixed.append(case_id)
            if previously_correct and not correct:
                regressed.append(case_id)
            if not correct:
                errors[stage].append({
                    "id": case_id, "category": row["category"], "language": row["language"],
                    "expected": row["expected"], "content": row["content"],
                    "reference": row["reference"], "query": row["query"],
                    "scores": row["scores"], **decision,
                })
        paired[stage] = {"fixed": fixed, "regressed": regressed}
        ablation_audit[stage] = {}
        for variant in ("rules_only", "focused", "calibrated"):
            candidate_rows = modes["detailed"] if variant == "rules_only" else modes["focused"]
            thresholds = profile["thresholds"] if variant == "calibrated" else uniform
            variant_fixed, variant_regressed, variant_errors = [], [], []
            for case_id, row in candidate_rows.items():
                base = modes["detailed"][case_id]
                if not row["ok"] or not base["ok"]:
                    continue
                decision = decide(row, thresholds)
                correct = decision["action"] == row["expected"]
                before = decide(base, uniform, rules=False)["action"] == base["expected"]
                if correct and not before:
                    variant_fixed.append(case_id)
                if before and not correct:
                    variant_regressed.append(case_id)
                if not correct:
                    variant_errors.append({
                        "id": case_id, "category": row["category"], "expected": row["expected"],
                        "scores": row["scores"], **decision,
                    })
            ablation_audit[stage][variant] = {
                "fixed": variant_fixed, "regressed": variant_regressed, "errors": variant_errors,
            }
        lines += ["", f"## {stage} 分类：原版 → 冻结配置", "",
                  "| 类别 | 准确率 | 召回率 | FP | FN |",
                  "|---|---:|---:|---:|---:|"]
        for category, q in validation[stage]["by_category"].items():
            b, a = q["baseline"], q["calibrated"]
            lines.append(
                f"| {category} | {percent(b['accuracy'])} → {percent(a['accuracy'])} | "
                f"{percent(b['recall'])} → {percent(a['recall'])} | "
                f"{b['fp']} → {a['fp']} | {b['fn']} → {a['fn']} |"
            )
        lines += ["", f"修复 {len(fixed)} 条，新增错误 {len(regressed)} 条。",
                  "", "| 剩余错误 ID | 预期 | 判定 | 触发项 |", "|---|---|---|---|"]
        for row in errors[stage]:
            lines.append(f"| {row['id']} | {row['expected']} | {row['action']} | "
                         f"{', '.join(row['triggered_checks']) or '—'} |")
    lines += ["", "## 匹配条件性能", "",
              "每格请求数见表，INPUT，单 HTTP 请求；客户端 HTTP 延迟，未计入本地规则/阈值处理。",
              "", "| 字符目标 | 并发 | 模板 | 成功/请求 | p50 ms | p95 ms | req/s |",
              "|---:|---:|---|---:|---:|---:|---:|"]
    for cell in sorted(benchmark, key=lambda c: (c["target_chars"], c["concurrency"], c["mode"])):
        latency = cell["latency_ms"]
        lines.append(
            f"| {cell['target_chars']} | {cell['concurrency']} | {cell['mode']} | "
            f"{cell['successes']}/{cell['requests']} | {latency['p50']:.1f} | "
            f"{latency['p95']:.1f} | {cell['successful_requests_per_second']:.2f} |"
        )
    measured = read_rows(out / "dev.jsonl") + read_rows(out / "regression.jsonl") + \
        read_rows(out / "holdout.jsonl") + read_rows(benchmark_path / "results.jsonl")
    warmups = read_rows(out / "warmup.jsonl") + read_rows(benchmark_path / "warmup.jsonl")
    totals = {
        "measured_requests": len(measured), "measured_errors": sum(not r["ok"] for r in measured),
        "warmup_requests": len(warmups), "warmup_errors": sum(not r["ok"] for r in warmups),
        "http_attempts": sum(r["attempts"] for r in measured + warmups),
        "models": sorted({r["model"] for r in measured if r["model"]}),
    }
    summary = dict(profile=profile, dev=dev, validation=validation, benchmark=benchmark,
                   totals=totals, paired=paired, errors=errors, ablation_audit=ablation_audit)
    write_json(out / "summary.json", summary)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if export_dir:
        export_dir.mkdir(parents=True, exist_ok=True)
        write_json(export_dir / "decider-calibrated-profile.json", profile)
        write_json(export_dir / "decider-focused-profile.json", {
            **profile, "selected": "focused", "thresholds": uniform,
            "selection_note": "Predefined 0.5 ablation; recall-oriented option after validation review. "
                              "This is not the dev-selected calibrated profile.",
        })
        write_json(export_dir / "decider-rules-profile.json", {
            **profile, "selected": "rules_only", "prompt_version": BASELINE_PROMPT_VERSION,
            "thresholds": uniform,
            "selection_note": "Predefined literal-rule replacement using original detailed responses; "
                              "semantic thresholds unchanged at 0.5.",
        })
        # Compact, versionable evidence; full synthetic texts/raw responses stay in results/.
        write_json(export_dir / "optimization-summary.json", {
            "protocol": json.loads((out / "protocol.json").read_text()),
            **{k: v for k, v in summary.items() if k != "errors"},
        })
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    print(out / "report.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, help="Export profiles and versionable metrics")
    args = parser.parse_args()
    report(args.run, args.export_dir)
