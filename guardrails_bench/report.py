from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

from .metrics import summarize_quality
from .runner import write_json


def pct(value):
    return "—" if value is None else f"{value:.1%}"


def num(value):
    return "—" if value is None else f"{value:.2f}"


def markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def read_rows(run: Path) -> list[dict]:
    path = run / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def generate_report(run: Path, threshold: float | None = None) -> dict:
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    threshold = threshold if threshold is not None else manifest["config"].get("threshold", 0.5)
    error_action = manifest["config"].get("error_action", "block")
    rows = read_rows(run)
    summary = {"manifest": manifest, "threshold": threshold, "error_action": error_action}
    lines = [
        "# Decider Guardrails 评测报告", "",
        f"- 时间：{manifest['created_at']}",
        f"- 状态：{manifest['status']}；类型：{manifest['kind']}",
        f"- 固定策略 SHA256：`{manifest['policy_sha256']}`",
        f"- 样本 SHA256：`{manifest['dataset_sha256']}`",
        "- 样本是人工编写的合成案例，标签依据本项目显式策略；不是 Bedrock 官方基准或生产认证。",
        "- 延迟包含客户端、网络和网关；server_ms 是接口自报值，无法视为纯 GPU 时间。",
        "- Bedrock 结果仅在配置现有 Guardrail 后产生；不同配置不能直接视为相同策略。",
        "",
    ]
    if manifest["kind"] == "eval":
        grouped = defaultdict(list)
        for row in rows:
            grouped[f"{row['provider']}/{row['mode']}"].append(row)
        summaries = {
            name: summarize_quality(values, threshold, error_action)
            for name, values in grouped.items()
        }
        summary["quality"] = summaries
        lines += [
            f"阈值：`block score >= {threshold}`；接口异常执行 `{error_action}`。"
            "效果指标仅统计合法响应；operational 指标额外计入异常处理策略。",
            "detailed 的 block score 是各检查违规概率的最大值，不是经过校准的总体违规概率。"
            "低分样本仍可能漏报；confidence 不等同于违规概率。",
            "",
            "| 模式 | 有效/总数 | Accuracy | Precision | Recall | F1 | 误报率 | 漏报率 | p50 ms | p95 ms |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, values in summaries.items():
            q, latency = values["overall"], values["latency_ms"]
            lines.append(f"| {name} | {q['n']}/{q['total']} | "
                         + " | ".join(pct(q[k]) for k in
                                      ("accuracy", "precision", "recall", "f1",
                                       "false_positive_rate", "false_negative_rate"))
                         + f" | {num(latency['p50'])} | {num(latency['p95'])} |")
        for name, values in summaries.items():
            lines += ["", f"## {name}", "", f"实际模型：`{', '.join(values['models'])}`", ""]
            q = values["overall"]
            lines.append(f"TP={q['tp']}，TN={q['tn']}，FP={q['fp']}，FN={q['fn']}，异常={q['errors']}。")
            if q["accuracy_ci95"]:
                lines.append("Accuracy 95% Wilson 区间："
                             + "–".join(pct(x) for x in q["accuracy_ci95"])
                             + "。成对和合成样本并非独立生产流量，区间仅供描述。")
            if values["calibration"]:
                lines.append(f"Brier={num(values['calibration']['brier'])}；"
                             f"ECE（10 bins）={num(values['calibration']['ece'])}。")
            for group_name in ("split", "language", "category", "length_bucket"):
                lines += ["", f"### 按 {group_name}", "",
                          "| 分组 | 有效/总数 | Accuracy | Recall | FP | FN | 异常 |",
                          "|---|---:|---:|---:|---:|---:|---:|"]
                for key, item in values["groups"][group_name].items():
                    lines.append(f"| {key} | {item['n']}/{item['total']} | "
                                 f"{pct(item['accuracy'])} | {pct(item['recall'])} | "
                                 f"{item['fp']} | {item['fn']} | {item['errors']} |")
            lines += ["", "### 误判与异常", "",
                      "| ID | 期望 | 实际 | 分数 | 内容（前 160 字） |",
                      "|---|---|---|---:|---|"]
            for row in grouped[name]:
                predicted = ("block" if row["score"] >= threshold else "allow") if row["ok"] else "ERROR"
                if predicted != row["expected"]:
                    lines.append("| " + " | ".join(markdown_cell(v) for v in (
                        row["id"], row["expected"], predicted,
                        num(row["score"]), row["error"] or row["content"][:160],
                    )) + " |")
        lines += ["", "阈值扫描仅用于诊断。若要选阈值，使用 dev；随后冻结策略与阈值，"
                  "在独立 test/业务数据上评估。不得用 test 最佳阈值报告泛化效果。"]
    else:
        path = run / "benchmark.json"
        cells = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        summary["benchmark"] = cells
        lines += [
            "固定并发的 closed-loop 测量；worker 开始请求后计时，不包含客户端等待队列。"
            "预热另存且不计入统计，默认关闭重试，所有错误均计入错误率。",
            "长度是 state 文本字符预算，不是 token；实际服务端 input_tokens 记录在 JSON 中。"
            "unique 模式在 state 前部插入随机标记减少前缀缓存复用，无法保证服务端完全无缓存。",
            "小样本 p95/p99 只供初步观察；这不是开放到达率的饱和压测或延迟 SLO 证明。",
            "",
            "| 模式 | 并发 | state目标字符 | 成功/总数 | 成功 req/s | p50 ms | p95 ms | p99 ms | 错误率 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for cell in sorted(cells, key=lambda c: (c["mode"], c["target_chars"], c["concurrency"])):
            lat = cell["latency_ms"]
            lines.append(f"| {cell['mode']} | {cell['concurrency']} | {cell['target_chars']} | "
                         f"{cell['successes']}/{cell['requests']} | "
                         f"{num(cell['successful_requests_per_second'])} | "
                         f"{num(lat['p50'])} | {num(lat['p95'])} | {num(lat['p99'])} | "
                         f"{pct(cell['error_rate'])} |")
    write_json(run / "summary.json", summary)
    (run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_html(run, summary, rows, "\n".join(lines))
    return summary


def write_html(run: Path, summary: dict, rows: list[dict], markdown: str):
    # Do not embed large raw payloads or long fixtures twice.
    compact_rows = [
        {k: row[k] for k in (
            "id", "provider", "mode", "category", "language", "split", "expected",
            "ok", "score", "error", "latency_ms"
        )} | {"content": row["content"][:500]}
        for row in rows
    ]
    data = json.dumps({"summary": summary, "rows": compact_rows}, ensure_ascii=False).replace("<", "\\u003c")
    template = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decider Guardrails 评测</title>
<style>
:root{font-family:system-ui,sans-serif;color:#e4eaf4;background:#101624}
body{max-width:1300px;margin:auto;padding:32px}h1{font-size:30px;margin:0 0 8px}
p{color:#aab9cf;line-height:1.7}.panel{background:#1a2334;border:1px solid #30425d;padding:20px;border-radius:12px;margin:20px 0}
label{margin-right:24px}select,input{margin:8px;accent-color:#6be3b0}select{padding:5px}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;border-bottom:1px solid #30425d;padding:10px;vertical-align:top}th{color:#9caec8}
.scroll{overflow:auto}.bad{color:#ffa4a4}.good{color:#6be3b0}pre{white-space:pre-wrap;line-height:1.6}
.bar{background:#6be3b0;height:12px;border-radius:3px;display:inline-block;min-width:1px}#bars td{width:25%}
</style>
<h1>Decider Guardrails</h1><p>可重复的效果与性能评测 · 合成中英文案例 · 实际远程接口</p>
<div id="meta" class="panel"></div>
<div id="controls" class="panel">
<label>诊断阈值 <input id="threshold" type="range" min="0.01" max="0.99" step="0.01"><b id="value"></b></label>
<label>分组 <select id="group"><option value="mode">模式</option><option value="category">类别</option><option value="language">语言</option><option value="split">数据划分</option></select></label>
<p>滑块仅重算保存的结果，不调用模型。detailed 使用各检查分数的最大值；异常不混入模型效果指标。测试集扫描不能用于选取上线阈值。</p>
</div>
<div class="panel scroll"><table id="metrics"></table></div>
<div id="bars" class="panel scroll"></div>
<div class="panel scroll"><h2 id="failure-title">误判与接口异常</h2><table id="failures"></table></div>
<details class="panel"><summary>完整报告与测量说明</summary><pre>REPORT_TEXT</pre></details>
<script type="application/json" id="data">DATA_JSON</script>
<script>
const data=JSON.parse(document.getElementById('data').textContent), s=data.summary;
const el=id=>document.getElementById(id), fmt=x=>x==null?'—':(100*x).toFixed(1)+'%';
function table(node,headers,rows){node.replaceChildren();let tr=document.createElement('tr');
for(const h of headers){const th=document.createElement('th');th.textContent=h;tr.append(th)}node.append(tr);
for(const r of rows){tr=document.createElement('tr');for(const v of r){const td=document.createElement('td');td.textContent=v;tr.append(td)}node.append(tr)}}
el('meta').textContent=`${s.manifest.created_at} · ${s.manifest.kind} · 状态 ${s.manifest.status} · 固定样本 ${s.manifest.case_count} · ${s.manifest.config.endpoint||s.manifest.config.region||''}`;
el('threshold').value=s.threshold;
function render(){
const t=Number(el('threshold').value);el('value').textContent=t.toFixed(2);
if(s.benchmark){
el('controls').hidden=true;el('failure-title').textContent='性能请求异常';
table(el('metrics'),['模式','并发','目标字符','成功/总数','成功 req/s','p50 ms','p95 ms','p99 ms'],
s.benchmark.map(c=>[c.mode,c.concurrency,c.target_chars,`${c.successes}/${c.requests}`,c.successful_requests_per_second.toFixed(2),...['p50','p95','p99'].map(k=>c.latency_ms[k]?.toFixed(1)??'—')]));
el('bars').hidden=true;
}else{
const groups=new Map(),field=el('group').value;
for(const r of data.rows){const key=r.provider+'/'+r.mode+(field==='mode'?'':' / '+r[field]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r)}
const result=[];for(const [name,rows] of groups){let tp=0,tn=0,fp=0,fn=0,errors=0;
for(const r of rows){if(!r.ok){errors++;continue}let gold=r.expected==='block',pred=r.score>=t;if(gold&&pred)tp++;else if(gold)fn++;else if(pred)fp++;else tn++}
const div=(a,b)=>b?a/b:null;result.push([name,rows.length-errors+'/'+rows.length,fmt(div(tp+tn,tp+tn+fp+fn)),fmt(div(tp,tp+fp)),fmt(div(tp,tp+fn)),fmt(div(fp,fp+tn)),fmt(div(fn,fn+tp)),fp,fn,errors])}
table(el('metrics'),['分组','有效/总数','Accuracy','Precision','Recall','误报率','漏报率','FP','FN','异常'],result);
el('bars').hidden=true;
}
const failures=data.rows.filter(r=>!r.ok||(!s.benchmark&&(r.score>=t?'block':'allow')!==r.expected));
table(el('failures'),['样本','模式','期望','实际','分数','内容 / 异常'],failures.map(r=>[r.id,r.mode,r.expected,r.ok?(r.score>=t?'block':'allow'):'ERROR',r.score?.toFixed(3)??'—',r.error||r.content]));
}
el('threshold').addEventListener('input',render);el('group').addEventListener('change',render);render();
</script></html>"""
    template = template.replace("REPORT_TEXT", html.escape(markdown)).replace("DATA_JSON", data)
    (run / "report.html").write_text(template, encoding="utf-8")
