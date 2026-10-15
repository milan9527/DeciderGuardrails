import pytest

from guardrails_bench.metrics import calibration, confusion, distribution, quality, wilson


def test_hand_computed_confusion_and_error_coverage():
    rows = [
        {"ok": True, "expected": "block", "score": 0.9},
        {"ok": True, "expected": "allow", "score": 0.2},
        {"ok": True, "expected": "allow", "score": 0.8},
        {"ok": True, "expected": "block", "score": 0.3},
        {"ok": False, "expected": "allow", "score": None},
    ]
    q = quality(rows, 0.5)
    assert (q["tp"], q["tn"], q["fp"], q["fn"]) == (1, 1, 1, 1)
    assert q["coverage"] == 0.8
    assert q["accuracy"] == 0.5
    assert q["operational"]["accuracy"] == 0.4
    assert q["operational"]["fp"] == 2
    assert quality(rows, 0.5, "allow")["operational"]["tn"] == 2


def test_empty_denominators_are_null_not_perfect_scores():
    empty = confusion([])
    assert empty["recall"] is None
    assert empty["accuracy"] is None
    assert quality([{"ok": False, "expected": "block"}], 0.5)["accuracy"] is None
    assert distribution([])["p95"] is None


def test_calibration_uses_probability_not_confidence():
    c = calibration([
        {"ok": True, "expected": "block", "score": 0.8},
        {"ok": True, "expected": "allow", "score": 0.2},
    ])
    assert c["brier"] == pytest.approx(0.04)
    assert c["ece"] == pytest.approx(0.2)
    assert calibration([{"ok": True, "expected": "block", "score": 1}])["bins"][0]["upper"] == 1


def test_wilson_and_percentiles():
    assert wilson(10, 10)[0] == pytest.approx(0.7224672)
    assert distribution([100, 200, 300])["p95"] == 290
