from dataclasses import asdict, replace

import pytest

from guardrails_bench.core import (
    Case, active_checks, build_request, load_cases, load_policy, parse_response,
)


def answer(block=0.7):
    return {
        "type": "choice", "choice": "block" if block >= 0.5 else "allow",
        "probabilities": {"block": block, "allow": 1 - block},
        "confidence": abs(2 * block - 1),
    }


def test_dataset_balance_and_split():
    cases = load_cases(None, load_policy())
    assert len(cases) == 168
    assert sum(c.split == "test" for c in cases) == 120
    assert sum(c.language == "zh" for c in cases) == 84
    assert len({c.id for c in cases}) == len(cases)


def test_gold_annotations_never_affect_request():
    policy = load_policy()
    case = load_cases(None, policy)[0]
    relabeled = replace(case, id="secret-gold-id", category="pii", expected="allow",
                        violations=(), split="test", rationale="secret-gold-reason", tags=("secret-tag",))
    for mode in ("compact", "detailed"):
        assert build_request(case, policy, mode) == build_request(relabeled, policy, mode)
    assert len(build_request(case, policy, "detailed")["questions"]) == 10


def test_context_checks_require_output_and_context():
    policy = load_policy()
    case = load_cases(None, policy)[0]
    assert "grounding" not in active_checks(replace(case, reference="X"), policy)
    with_context = replace(case, source="OUTPUT", reference="X", query="Y")
    assert len(active_checks(with_context, policy)) == 12
    assert "reference" in build_request(with_context, policy, "compact")["state"]


def test_use_block_probability_not_confidence_or_selected_choice():
    payload = {"answers": {"a": answer(0.7), "b": answer(0.1)}, "latency_ms": 10}
    result = parse_response(payload, {"a", "b"})
    assert result["score"] == 0.7
    assert result["confidences"]["b"] == 0.8
    assert result["scores"]["b"] == 0.1


@pytest.mark.parametrize("mutate", [
    lambda p: p["answers"].clear(),
    lambda p: p["answers"]["a"].update(type="noul"),
    lambda p: p["answers"]["a"].update(choice="unknown"),
    lambda p: p["answers"]["a"]["probabilities"].update(block=float("nan")),
    lambda p: p["answers"]["a"]["probabilities"].update(block=True),
    lambda p: p["answers"]["a"]["probabilities"].update(block=1.5),
    lambda p: p["answers"]["a"]["probabilities"].update(allow=0.9),
    lambda p: p["answers"]["a"].update(choice="allow"),
    lambda p: p.update(latency_ms=-1),
    lambda p: p.update(usage={"input_tokens": "100"}),
    lambda p: p.update(model={"unexpected": "object"}),
])
def test_malformed_responses_fail_closed_instead_of_default_allow(mutate):
    payload = {"answers": {"a": answer()}}
    mutate(payload)
    with pytest.raises(ValueError):
        parse_response(payload, {"a"})


def test_dataset_rejects_inconsistent_gold():
    policy = load_policy()
    row = asdict(load_cases(None, policy)[0])
    row["violations"] = []
    with pytest.raises(ValueError, match="violations"):
        Case.from_dict(row, set(policy["checks"]))
