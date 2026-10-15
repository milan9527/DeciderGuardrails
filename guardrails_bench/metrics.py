from __future__ import annotations

import math
from collections import defaultdict


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def distribution(values: list[float]) -> dict:
    return {
        "n": len(values), "mean": ratio(sum(values), len(values)),
        "p50": percentile(values, 0.50), "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99), "min": min(values, default=None),
        "max": max(values, default=None),
    }


def wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    divisor = 1 + z * z / total
    center = (p + z * z / (2 * total)) / divisor
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / divisor
    return [max(0, center - half), min(1, center + half)]


def confusion(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(gold and predicted for gold, predicted in pairs)
    tn = sum(not gold and not predicted for gold, predicted in pairs)
    fp = sum(not gold and predicted for gold, predicted in pairs)
    fn = sum(gold and not predicted for gold, predicted in pairs)
    return {
        "n": len(pairs), "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "accuracy": ratio(tp + tn, len(pairs)),
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "false_positive_rate": ratio(fp, fp + tn),
        "false_negative_rate": ratio(fn, fn + tp),
        "accuracy_ci95": wilson(tp + tn, len(pairs)),
        "recall_ci95": wilson(tp, tp + fn),
    }


def quality(rows: list[dict], threshold: float, error_action: str = "block") -> dict:
    valid = [r for r in rows if r["ok"]]
    measured = confusion([(r["expected"] == "block", r["score"] >= threshold) for r in valid])
    measured.update({
        "total": len(rows), "errors": len(rows) - len(valid),
        "coverage": ratio(len(valid), len(rows)),
        "operational": confusion([
            (r["expected"] == "block",
             r["score"] >= threshold if r["ok"] else error_action == "block")
            for r in rows
        ]),
    })
    return measured


def calibration(rows: list[dict]) -> dict:
    valid = [r for r in rows if r["ok"]]
    if not valid:
        return {"n": 0, "brier": None, "ece": None, "bins": []}
    bins = []
    for index in range(10):
        bucket = [r for r in valid if min(int(r["score"] * 10), 9) == index]
        if bucket:
            bins.append({
                "lower": index / 10, "upper": (index + 1) / 10, "n": len(bucket),
                "mean_probability": sum(r["score"] for r in bucket) / len(bucket),
                "block_fraction": sum(r["expected"] == "block" for r in bucket) / len(bucket),
            })
    return {
        "n": len(valid),
        "brier": sum((r["score"] - (r["expected"] == "block")) ** 2 for r in valid) / len(valid),
        "ece": sum(b["n"] * abs(b["mean_probability"] - b["block_fraction"]) for b in bins) / len(valid),
        "bins": bins,
    }


def summarize_quality(rows: list[dict], threshold: float, error_action: str) -> dict:
    groups = {}
    for field in ("category", "language", "split", "source", "length_bucket"):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[field]].append(row)
        groups[field] = {
            key: quality(values, threshold, error_action)
            for key, values in sorted(grouped.items())
        }
    check_rows = defaultdict(list)
    for row in rows:
        if row["ok"]:
            for check, score in row["scores"].items():
                if check != "guardrail":
                    check_rows[check].append((check in row["violations"], score >= threshold))
    modes = {r["mode"] for r in rows}
    return {
        "overall": quality(rows, threshold, error_action),
        "groups": groups,
        "per_check": {key: confusion(values) for key, values in sorted(check_rows.items())},
        "threshold_sweep": [
            {"threshold": t / 100, **quality(rows, t / 100, error_action)}
            for t in (10, 25, 50, 75, 90, 95)
        ] if modes != {"managed"} else [],
        "calibration": calibration(rows) if modes == {"compact"} else None,
        "latency_ms": distribution([r["latency_ms"] for r in rows if r["ok"]]),
        "all_request_latency_ms": distribution([r["latency_ms"] for r in rows]),
        "server_ms": distribution([r["server_ms"] for r in rows if r["ok"] and r["server_ms"] is not None]),
        "models": sorted({r["model"] for r in rows if r["model"]}),
        "usage": {
            key: sum(r["usage"].get(key, 0) for r in rows if r["ok"])
            for key in ("input_tokens", "output_tokens")
        },
    }
