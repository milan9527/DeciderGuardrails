"""Build a paired comparison summary from completed, saved measurements."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardrails_bench.metrics import summarize_quality
from guardrails_bench.report import read_rows
from guardrails_bench.runner import write_json

NAMES = {"bedrock/managed": "Bedrock Standard", "decider/compact": "Decider compact",
         "decider/detailed": "Decider detailed"}
ORDER = list(NAMES)


def pct(n):
    return "—" if n is None else f"{100*n:.1f}%"


def number(n):
    return "—" if n is None else f"{n:.1f}"


def quality_table(groups):
    lines = ["| 服务 | 正确/有效 | Accuracy | Recall | FP / FN | API 错误 | p50 / p95 ms |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name in ORDER:
        item = groups[name]
        q, latency = item["overall"], item["latency_ms"]
        lines.append(f"| {NAMES[name]} | {q['tp']+q['tn']}/{q['n']} | {pct(q['accuracy'])} | "
                     f"{pct(q['recall'])} | {q['fp']} / {q['fn']} | {q['errors']} | "
                     f"{number(latency['p50'])} / {number(latency['p95'])} |")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--markdown", type=Path, default=ROOT / "BEDROCK_COMPARISON.md")
    args = parser.parse_args()
    paths = json.loads((args.root / "comparison-runs.json").read_text())
    quality_run, bench_run = Path(paths["eval"]), Path(paths["bench"])
    manifests = {kind: json.loads((Path(path) / "manifest.json").read_text()) for kind, path in paths.items()}
    assert all(m["status"] == "complete" for m in manifests.values())
    rows, perf_rows = read_rows(quality_run), read_rows(bench_run)
    grouped = {name: [r for r in rows if f"{r['provider']}/{r['mode']}" == name] for name in ORDER}
    expected_ids = {r["id"] for r in grouped[ORDER[0]]}
    assert len(expected_ids) == 120
    assert all(len(g) == 120 and {r["id"] for r in g} == expected_ids for g in grouped.values())
    # All providers receive identical benchmark jobs within each matrix condition.
    signatures = {}
    for row in perf_rows:
        key = (row["concurrency"], row["target_chars"], row["sequence"])
        signature = tuple(row.get(k) for k in ("content", "background", "source", "reference", "query"))
        assert signatures.setdefault(key, signature) == signature
    scopes = {
        "all_120": lambda r: True,
        "input_only": lambda r: r["source"] == "INPUT",
        "officially_supported_input": lambda r: r["source"] == "INPUT"
        and not (r["language"] == "zh" and r["category"] == "word_filter"),
        "output_context_adapter": lambda r: r["source"] == "OUTPUT",
    }
    quality = {scope: {name: summarize_quality([r for r in group if include(r)], 0.5, "block")
                       for name, group in grouped.items()} for scope, include in scopes.items()}
    cells = json.loads((bench_run / "benchmark.json").read_text())
    measured_bedrock = [r for r in rows + perf_rows if r["provider"] == "bedrock"]
    warmups = [
        json.loads(line)
        for run in [quality_run, bench_run]
        for line in (run / "warmup.jsonl").read_text().splitlines() if line.strip()
    ]
    units = Counter()
    for row in measured_bedrock:
        units.update({k: v for k, v in row["usage"].items() if isinstance(v, (int, float))})
    report = {
        "runs": paths, "quality": quality, "benchmark": cells,
        "bedrock_measured_usage": dict(units),
        "errors": dict(Counter(r["error"] for r in rows + perf_rows if not r["ok"])),
        "counts": {"quality": len(rows), "performance": len(perf_rows), "warmup": len(warmups),
                   "measured_errors": sum(not r["ok"] for r in rows + perf_rows),
                   "warmup_errors": sum(not r["ok"] for r in warmups)},
        "timing_note": "HTTP/SDK roundtrip on one client; network and service queueing included.",
    }
    write_json(args.root / "comparison-summary.json", report)
    main_n = quality["officially_supported_input"][ORDER[0]]["overall"]["total"]
    input_n = quality["input_only"][ORDER[0]]["overall"]["total"]
    lines = [
        "# Decider 与 Bedrock Guardrails 实测", "",
        f"测量开始：`{manifests['eval']['created_at']}`；结束：`{manifests['bench']['completed_at']}`。",
        "同一台客户端、同一时段，Bedrock 请求区域 `us-east-1`；Decider 使用现有 vLLM API。"
        "Bedrock Standard 通过 `us.guardrail.v1:0` 在美国区域内路由。",
        f"效果测试并发 4，120 条样本 × 3 种服务模式，共 {len(rows)} 次测量；"
        f"性能测量 {len(perf_rows)} 次；另有 {len(warmups)} 次预热。正式测量的 API 错误数为 "
        f"{report['counts']['measured_errors']}，预热错误数为 {report['counts']['warmup_errors']}。", "",
        "## 可直接匹配输入字段的效果对比", "",
        f"主表采用 {main_n} 条 INPUT 样本：来自原有 test 集，排除 Bedrock 官方未覆盖的中文禁词样本。"
        "三方看到相同候选文本，没有新增参考资料或查询占位内容。标签仍按本项目策略定义；"
        "Bedrock 内置类别和 HIGH 强度与 Decider 自定义判定并非严格等价策略。", "",
        *quality_table(quality["officially_supported_input"]), "",
        f"全部 {input_n} 条 INPUT（额外包含中文禁词的实际表现）：", "",
        *quality_table(quality["input_only"]), "",
        "## 全量 120 条测试样本", "",
        "此表还包含 OUTPUT 上下文适配，以及官方未覆盖的中文禁词、事实核验和相关性样本。"
        "它反映本项目样本的实用表现，不能称为完全相同 API 契约、语言支持范围或生产总体准确率。", "",
        *quality_table(quality["all_120"]), "",
        "### 分场景准确率", "",
        "| 场景 | 样本数 | Bedrock Standard | Decider compact | Decider detailed |",
        "|---|---:|---:|---:|---:|",
    ]
    for category in sorted({r["category"] for r in rows}):
        values = [quality["all_120"][name]["groups"]["category"][category] for name in ORDER]
        lines.append(f"| {category} | {values[0]['total']} | "
                     + " | ".join(pct(v["accuracy"]) for v in values) + " |")
    lines += ["", "### 按语言（全量集）", "",
              "| 语言 | 样本数 | Bedrock Standard | Decider compact | Decider detailed |",
              "|---|---:|---:|---:|---:|"]
    for language in ["en", "zh"]:
        values = [quality["all_120"][name]["groups"]["language"][language] for name in ORDER]
        lines.append(f"| {language} | {values[0]['total']} | "
                     + " | ".join(pct(v["accuracy"]) for v in values) + " |")
    lines += ["", "## 性能矩阵", "",
              f"每格 {manifests['bench']['config']['requests']} 次测量、2 次预热，"
              f"三种并发 × 三种长度 × 三种服务模式，共 {len(perf_rows)} 次测量。"
              "矩阵顺序按 seed=42 打乱；性能池仅选 INPUT，避免混入 Bedrock OUTPUT 的 5,000 字符限制。"
              "同一矩阵条件下三方的候选文本、背景填充和标记完全一致。",
              "以下是成功请求/秒与成功请求 p95；接口错误另列，不算成功。",
              "延迟是 HTTP/SDK 往返，包含网络和排队；吞吐是 closed-loop 客户端吞吐，不是 GPU 性能或服务容量上限。", "",
              "| 字符目标 | 并发 | Bedrock req/s · p95 ms | compact req/s · p95 ms | detailed req/s · p95 ms | 三方错误数 |",
              "|---|---:|---:|---:|---:|---:|"]
    for length in [256, 2048, 8192]:
        for concurrency in [1, 4, 8]:
            selected = [next(c for c in cells if c["mode"] == mode and c["target_chars"] == length
                             and c["concurrency"] == concurrency) for mode in ["managed", "compact", "detailed"]]
            lines.append(f"| {length} | {concurrency} | "
                         + " | ".join(f"{c['successful_requests_per_second']:.2f} · {number(c['latency_ms']['p95'])}"
                                      for c in selected)
                         + " | " + " / ".join(str(c["errors"]) for c in selected) + " |")
    lines += ["", "## 配置、差异与边界", "",
              "- Decider：`compact` / `detailed`，block 分数阈值 0.5；冻结原有策略与标签，不按 test 结果调参。",
              "- Bedrock：内容过滤与禁用主题均为 Standard；六类内容过滤为 HIGH，PROMPT_ATTACK 只检查 INPUT。"
              "投资建议和自伤方法分别定义禁用主题；禁词与项目一致；PII 选定类型使用 BLOCK，另加中国身份证格式正则。",
              "- Bedrock 的三份配置均冻结为数值版本；只按 source/reference/query 字段选择 base、grounding 或 relevance，"
              "不按类别或正确答案选择。事实核验和相关性阈值均为 0.7，与 Decider 的 0.5 不是同一概率尺度。",
              "- Bedrock 上下文检查即使只启用一种过滤器，也要求 reference、query、candidate 三种字段。"
              "GROUNDING-only 的缺失 query 补固定概括请求；RELEVANCE-only 的缺失 reference 补固定无参考资料声明。"
              "占位字段不执行其他过滤器，但仍可能影响托管模型，所以 OUTPUT 16 条单独视为适配诊断。",
              "- AWS 官方列明 Standard 内容过滤/禁用主题与 PII 支持中文，但词过滤和 contextual grounding 的官方语言范围仅英、法、西。"
              "中文后两类返回 HTTP 200 也不等于官方支持。",
              "- Bedrock 采用原生过滤器，词匹配、PII 实体范围与 Decider 文本策略有边界差异；"
              "本实验没有训练、校准或复现 AWS 内部策略。",
              "- 默认不重试；错误单列。每格样本量有限、仅一次运行；p95/p99 不适合作为生产 SLO。"
              "unique 标记在一格内不同，但同长度跨格复用以保持输入配对，无法排除跨格缓存。",
              "- Bedrock 返回的 usage 已保存在 JSON，包括各策略计量单位；未把单位猜算成账单或与 vLLM GPU 成本直接比较。",
              "- 所有结果来自固定合成样本。API 返回的拦截不证明实际执行了生产阻断；本工具只评测分类/干预判定。", "",
              "## Bedrock 全量集误判", "",
              "| ID | 预期 | 实际 | 命中过滤器 |", "|---|---|---|---|"]
    for row in grouped["bedrock/managed"]:
        predicted = ("block" if row["score"] >= 0.5 else "allow") if row["ok"] else "ERROR"
        if predicted == row["expected"]:
            continue
        triggered = []
        for assessment in (row["raw"] or {}).get("assessments", []):
            for policy, details in assessment.items():
                if not isinstance(details, dict):
                    continue
                for entries in details.values():
                    if isinstance(entries, list):
                        triggered.extend(str(item.get("type") or item.get("name") or item.get("match") or policy)
                                         for item in entries if isinstance(item, dict)
                                         and item.get("action") not in {None, "NONE"})
        lines.append(f"| {row['id']} | {row['expected']} | {predicted} | {', '.join(triggered) or '无'} |")
    lines += ["", "## 复核文件", "",
              f"- 固定策略：`benchmarks/bedrock_policy.json`；Bedrock 配置与版本快照：`{args.root}/bedrock-guardrails.json`。",
              f"- 数据 SHA256：`{manifests['eval']['dataset_sha256']}`。",
              f"- 效果逐条记录与 HTML：`{quality_run}/results.jsonl`、`{quality_run}/report.html`。",
              f"- 性能逐条记录与 HTML：`{bench_run}/results.jsonl`、`{bench_run}/report.html`。",
              f"- 分范围指标与策略计量：`{args.root}/comparison-summary.json`。",
              "- AWS 文档：[层级](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-tiers.html)、"
              "[语言支持](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-supported-languages.html)、"
              "[上下文请求契约](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html)、"
              "[跨区域配置](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-cross-region-support.html)。",
              "", "本次新增代码与报告仅保存在本地，未推送 GitHub。"]
    args.markdown.write_text("\n".join(lines) + "\n")
    print("Summary:", args.root / "comparison-summary.json")
    print("Report:", args.markdown)


if __name__ == "__main__":
    main()
