from __future__ import annotations

import asyncio
import json
import platform
import random
import sys
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .core import Case, digest
from .metrics import distribution, ratio


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def create_run(output: Path, kind: str, config: dict, policy: dict, cases: list[Case]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    run = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{kind}-{uuid.uuid4().hex[:6]}")
    run.mkdir()
    write_json(run / "manifest.json", {
        "schema_version": 1, "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": __version__, "python": sys.version.split()[0],
        "platform": platform.platform(), "config": config,
        "policy_sha256": digest(policy),
        "dataset_sha256": digest([asdict(c) for c in cases]),
        "case_count": len(cases), "status": "running",
    })
    write_json(run / "policy.json", policy)
    # Snapshot includes labels for reproducibility; only build_request creates model input.
    write_json(run / "dataset.json", [asdict(c) for c in cases])
    return run


def row_for(case: Case, result, provider, phase: str, **extra) -> dict:
    row = {
        **asdict(case), **asdict(result), "provider": provider.name, "mode": provider.mode,
        "phase": phase, "content_chars": len(case.content),
        "length_bucket": "long" if len(case.content) >= 2000 else "short",
        "timestamp": datetime.now(timezone.utc).isoformat(), **extra,
    }
    return row


async def run_batch(provider, jobs: list[Case], concurrency: int, journal: Path,
                    phase: str = "measured", extra: dict | None = None) -> tuple[list[dict], float]:
    """Bounded closed-loop workers: network time starts after acquiring a worker."""
    queue = asyncio.Queue()
    for index, case in enumerate(jobs):
        queue.put_nowait((index, case))
    rows = []
    start = time.perf_counter()
    with journal.open("a", encoding="utf-8") as stream:
        async def worker():
            while True:
                try:
                    index, case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                result = await provider.evaluate(case)
                row = row_for(case, result, provider, phase, sequence=index, **(extra or {}))
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                rows.append(row)
                queue.task_done()
        await asyncio.gather(*(worker() for _ in range(min(concurrency, len(jobs)))))
    return sorted(rows, key=lambda r: r["sequence"]), time.perf_counter() - start


async def warmup(provider, cases: list[Case], count: int, run: Path):
    if not count:
        return
    rows, _ = await run_batch(provider, [cases[i % len(cases)] for i in range(count)],
                              1, run / "warmup.jsonl", phase="warmup")
    if not any(row["ok"] for row in rows):
        raise RuntimeError(f"All warmup requests failed: {rows[0]['error']}; see {run / 'warmup.jsonl'}")


def finish_run(run: Path, status: str, **extra):
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(status=status, completed_at=datetime.now(timezone.utc).isoformat(), **extra)
    write_json(run / "manifest.json", manifest)


async def evaluate(run: Path, cases: list[Case], modes: list[str], factory: Callable,
                   concurrency: int, warmups: int, seed: int):
    jobs = list(cases)
    random.Random(seed).shuffle(jobs)
    timings = {}
    for mode in modes:
        provider = factory(mode, concurrency)
        try:
            await warmup(provider, jobs, warmups, run)
            rows, elapsed = await run_batch(provider, jobs, concurrency, run / "results.jsonl")
            timings[mode] = {
                "elapsed_seconds": elapsed, "requests_per_second": len(rows) / elapsed,
                "successful_requests_per_second": sum(r["ok"] for r in rows) / elapsed,
            }
            print(f"{mode}: {len(rows)} samples, {sum(r['ok'] for r in rows)} valid, "
                  f"{elapsed:.2f}s, {timings[mode]['successful_requests_per_second']:.2f} success/s",
                  flush=True)
        finally:
            await provider.close()
    finish_run(run, "complete", timing=timings)


def performance_jobs(cases: list[Case], requests: int, target_chars: int, seed: int,
                     cache: str) -> list[Case]:
    # Long fixtures are evaluated for quality separately. Control state size here.
    pool = [c for c in cases if len(c.content) <= target_chars] or cases
    rng = random.Random(seed)
    shuffled = list(pool)
    rng.shuffle(shuffled)
    jobs = []
    filler = "Ordinary background: the library is open on weekdays. "
    for index in range(requests):
        case = shuffled[index % len(shuffled)]
        overhead = len(case.content) + len(case.reference or "") + len(case.query or "")
        nonce = f"Benchmark request {rng.getrandbits(96):024x}. " if cache == "unique" else ""
        padding_length = max(0, target_chars - overhead - len(nonce))
        padding = (filler * (padding_length // len(filler) + 1))[:padding_length]
        jobs.append(replace(case, background=nonce + padding))
    return jobs


async def benchmark(run: Path, cases: list[Case], modes: list[str], factory: Callable,
                    concurrencies: list[int], lengths: list[int], requests: int,
                    warmups: int, seed: int, cache: str):
    # Randomized cell order reduces confounding from host drift. Saved seed reproduces it.
    cells = [(mode, c, length) for mode in modes for c in concurrencies for length in lengths]
    random.Random(seed).shuffle(cells)
    summaries = []
    for cell_index, (mode, concurrency, length) in enumerate(cells):
        provider = factory(mode, concurrency)
        jobs = performance_jobs(cases, requests, length, seed, cache)
        cell_id = f"{mode}-c{concurrency}-chars{length}"
        try:
            warmup_cases = [
                replace(c, background="Warmup " + (c.background or ""))
                for c in jobs
            ]
            await warmup(provider, warmup_cases, warmups, run)
            rows, elapsed = await run_batch(
                provider, jobs, concurrency, run / "results.jsonl",
                extra={"cell": cell_id, "concurrency": concurrency, "target_chars": length},
            )
            good = [r for r in rows if r["ok"]]
            summary = {
                "cell": cell_id, "mode": mode, "concurrency": concurrency,
                "target_chars": length, "requests": len(rows), "successes": len(good),
                "errors": len(rows) - len(good), "error_rate": 1 - len(good) / len(rows),
                "elapsed_seconds": elapsed, "requests_per_second": len(rows) / elapsed,
                "successful_requests_per_second": len(good) / elapsed,
                "latency_ms": distribution([r["latency_ms"] for r in good]),
                "all_request_latency_ms": distribution([r["latency_ms"] for r in rows]),
                "server_ms": distribution([r["server_ms"] for r in good if r["server_ms"] is not None]),
                "input_tokens": distribution([r["usage"]["input_tokens"] for r in good
                                               if "input_tokens" in r["usage"]]),
                "input_tokens_per_second": ratio(
                    sum(r["usage"].get("input_tokens", 0) for r in good), elapsed
                ),
                "http_attempts": sum(r["attempts"] for r in rows),
                "cache": cache,
            }
            summaries.append(summary)
            write_json(run / "benchmark.json", summaries)
            print(f"[{cell_index + 1}/{len(cells)}] {cell_id}: "
                  f"{len(good)}/{len(rows)} valid, {len(good)/elapsed:.2f} success/s, "
                  f"p95={summary['latency_ms']['p95']}ms", flush=True)
        finally:
            await provider.close()
    finish_run(run, "complete")
