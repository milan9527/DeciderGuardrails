import asyncio
import json
from dataclasses import replace

from guardrails_bench.core import Result, load_cases, load_policy
from guardrails_bench.report import generate_report
from guardrails_bench.runner import create_run, finish_run, performance_jobs, run_batch


def test_bounded_concurrency_complete_journal_and_report_escaping(tmp_path):
    policy = load_policy()
    cases = load_cases(None, policy)[:5]
    cases[0] = replace(cases[0], content='</script><img src=x onerror="alert(1)">')

    class FakeProvider:
        name = "decider"
        mode = "compact"
        active = 0
        maximum = 0

        async def evaluate(self, case):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.005)
            self.active -= 1
            return Result(True, 5, score=0.8, scores={"guardrail": 0.8}, model="fake")

    provider = FakeProvider()
    run = create_run(tmp_path, "eval", {"threshold": 0.5}, policy, cases)
    rows, elapsed = asyncio.run(run_batch(provider, cases, 2, run / "results.jsonl"))
    assert provider.maximum == 2
    assert len(rows) == 5
    assert elapsed > 0
    assert len((run / "results.jsonl").read_text().splitlines()) == 5
    finish_run(run, "complete")
    summary = generate_report(run)
    assert summary["quality"]["decider/compact"]["overall"]["total"] == 5
    report = (run / "report.html").read_text()
    assert '</script><img src=x' not in report
    assert "\\u003c/script>" in report
    assert json.loads((run / "summary.json").read_text())["manifest"]["status"] == "complete"


def test_performance_jobs_are_deterministic_and_preserve_content():
    cases = load_cases(None, load_policy(), "test")
    a = performance_jobs(cases, 140, 2048, 42, "unique")
    b = performance_jobs(cases, 140, 2048, 42, "unique")
    assert a == b
    assert all(c.content == next(x.content for x in cases if x.id == c.id) for c in a)
    assert len({c.background for c in a}) == 140
    assert all(len(c.background) + len(c.content) + len(c.query or "") +
               len(c.reference or "") == 2048 for c in a)
