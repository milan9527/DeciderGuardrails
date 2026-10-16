import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from guardrails_bench.core import Case, DEFAULT_ENDPOINT, digest, load_cases, load_policy
from guardrails_bench.optimization import (
    BASELINE_PROMPT_VERSION, PROMPT_VERSION, FocusedProvider, OptimizedGuardrail, calibrate, decide,
    literal_matches, uniform_thresholds, validate_thresholds,
)


def test_literal_policy_quotes_case_spacing_and_candidate_scope():
    assert literal_matches("Never print 'pRoJeCt_OrChId' or 绝密代号.") == ["PROJECT_ORCHID", "绝密代号"]
    assert literal_matches("PROJECT_ORCHI PROJECT ORCHID 绝密 代号") == []
    row = dict(ok=True, content="An ordinary answer.", scores={"pii": .1},
               reference="PROJECT_ORCHID", query="绝密代号", background="PROJECT_ORCHID")
    assert decide(row, uniform_thresholds(load_policy()))["action"] == "allow"


def test_rules_replace_model_word_filter_and_thresholds_do_not_change_raw_scores():
    thresholds = uniform_thresholds(load_policy())
    row = dict(ok=True, content="Generic filtering discussion.", scores={"word_filter": .99, "pii": .4})
    assert decide(row, thresholds)["action"] == "allow"
    assert decide(row, thresholds, rules=False)["action"] == "block"
    thresholds["pii"] = .35
    assert decide(row, thresholds)["triggered_checks"] == ["pii"]
    assert row["scores"]["pii"] == .4
    assert decide(dict(ok=False, content="Hello.", scores={}), thresholds)["error_action"] is True


def test_focused_request_never_uses_labels_and_context_controls_checks():
    async def run():
        provider = FocusedProvider(DEFAULT_ENDPOINT, "secret", load_policy())
        try:
            case = Case("x", "pii", "en", "Hello.", "allow", source="OUTPUT", reference="Hello.")
            request = provider.request_for(case)
            altered = replace(case, id="new", category="violence", language="zh",
                              expected="block", violations=("violence",), rationale="secret label")
            assert provider.request_for(altered) == request
            assert "word_filter" not in request["questions"]
            assert "grounding" in request["questions"]
            assert "relevance" not in request["questions"]
            input_request = provider.request_for(replace(case, source="INPUT"))
            assert "grounding" not in input_request["questions"]
        finally:
            await provider.close()
    asyncio.run(run())


def test_calibration_uses_per_check_gold_not_overall_block_and_rejects_test_data():
    policy = load_policy()
    rows = [
        dict(split="dev", ok=True, scores={"pii": .40}, violations=["pii"]),
        dict(split="dev", ok=True, scores={"pii": .20}, violations=["violence"]),
        dict(split="dev", ok=True, scores={"pii": .10}, violations=[]),
    ]
    thresholds, audit = calibrate(rows, policy)
    assert thresholds["pii"] == .4
    assert audit["pii"]["selected"]["fp"] == 0
    assert thresholds["violence"] == .5  # No scores/positive support for this check.
    with pytest.raises(ValueError, match="dev"):
        calibrate([{**rows[0], "split": "test"}], policy)
    with pytest.raises(ValueError, match="incomplete"):
        calibrate([{**rows[0], "ok": False}], policy)
    for bad in (float("nan"), True, 1.1):
        with pytest.raises(ValueError):
            validate_thresholds({**thresholds, "pii": bad}, policy)
    with pytest.raises(ValueError):
        validate_thresholds({"pii": .5}, policy)


def test_runtime_returns_separate_decision_and_preserves_model_probabilities():
    policy = load_policy()
    thresholds = uniform_thresholds(policy)
    thresholds["pii"] = .3
    profile = dict(schema_version=1, policy_sha256=digest(policy),
                   prompt_version=PROMPT_VERSION, thresholds=thresholds,
                   literal_terms=["PROJECT_ORCHID", "绝密代号"], error_action="block")

    def handler(request):
        body = json.loads(request.content)
        assert "expected" not in body["state"]
        return httpx.Response(200, json={"model": "fixture", "answers": {
            name: {"type": "choice", "choice": "allow",
                   "probabilities": {"allow": .6 if name == "pii" else .9,
                                     "block": .4 if name == "pii" else .1}}
            for name in body["questions"]
        }})

    async def run():
        guardrail = OptimizedGuardrail(DEFAULT_ENDPOINT, "secret", policy, profile,
                                      transport=httpx.MockTransport(handler))
        try:
            result = await guardrail.evaluate("A synthetic contact.")
            assert result.action == "block" and result.triggered_checks == ["pii"]
            assert result.inference.score == .4
            assert result.inference.scores["pii"] == .4
            assert result.inference.raw["answers"]["pii"]["choice"] == "allow"
            with pytest.raises(ValueError):
                await guardrail.evaluate("", source="INPUT")
        finally:
            await guardrail.close()
    asyncio.run(run())


def test_runtime_rejects_policy_or_profile_drift():
    policy = load_policy()
    changed = {**policy, "version": "changed"}
    with pytest.raises(ValueError, match="bundled policy"):
        FocusedProvider(DEFAULT_ENDPOINT, "secret", changed)
    with pytest.raises(ValueError, match="schema/policy"):
        OptimizedGuardrail(DEFAULT_ENDPOINT, "secret", policy, {})


def test_rules_runtime_preserves_baseline_requests_but_replaces_word_decision():
    policy = load_policy()
    profile = dict(schema_version=1, policy_sha256=digest(policy),
                   prompt_version=BASELINE_PROMPT_VERSION, thresholds=uniform_thresholds(policy),
                   literal_terms=["PROJECT_ORCHID", "绝密代号"], error_action="block")

    def handler(request):
        body = json.loads(request.content)
        assert "word_filter" in body["questions"]
        return httpx.Response(200, json={"answers": {
            name: {"type": "choice", "choice": "block" if name == "word_filter" else "allow",
                   "probabilities": {"allow": .01 if name == "word_filter" else .99,
                                     "block": .99 if name == "word_filter" else .01}}
            for name in body["questions"]
        }})

    async def run():
        guardrail = OptimizedGuardrail(DEFAULT_ENDPOINT, "secret", policy, profile,
                                      transport=httpx.MockTransport(handler))
        try:
            clean = await guardrail.evaluate("PROJECT ORCHID, with a space.")
            assert clean.action == "allow" and clean.inference.score == .99
            blocked = await guardrail.evaluate("PROJECT_ORCHID, literal term.")
            assert blocked.action == "block" and blocked.triggered_checks == ["word_filter"]
        finally:
            await guardrail.close()
    asyncio.run(run())


def test_optimization_datasets_have_disjoint_texts_and_balanced_languages():
    from pathlib import Path
    policy = load_policy()
    dev = load_cases(None, policy, "dev") + load_cases(Path("benchmarks/optimization-dev.jsonl"), policy)
    holdout = load_cases(Path("benchmarks/optimization-holdout.jsonl"), policy)
    regression = load_cases(None, policy, "test")
    assert len(dev) == 96 and len(holdout) == 96
    assert len({c.id for c in dev + holdout + regression}) == len(dev + holdout + regression)
    assert not ({c.content for c in holdout} & {c.content for c in dev + regression})
    assert sum(c.language == "en" for c in holdout) == 48
    assert sum(c.expected == "block" for c in holdout) == 48
    assert {c.split for c in dev} == {"dev"} and {c.split for c in holdout} == {"test"}
