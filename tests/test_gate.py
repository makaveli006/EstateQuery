"""The release gate's arithmetic, without any model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_evals import row

from estatequery.config import MODELS_BY_ID, ModelSpec
from estatequery.evals import gate

SPEC = ModelSpec("t:m", "t", "m", "Test model", price_in=1.0, price_out=2.0)
GATE_FILE = Path(__file__).resolve().parent.parent / "eval" / "gate.json"


def rows() -> list[dict]:
    return [
        row(id="a", needs_tools=True, checks={"tool": True, "args": True}, tools_used=["x"], latency_ms=2000,
            grounded=True, input_tokens=1000, output_tokens=500),
        row(id="b", needs_tools=True, checks={"tool": True}, tools_used=["x", "y", "z"], latency_ms=4000,
            grounded=True, input_tokens=1000, output_tokens=500, duplicate_calls=1),
        row(id="c", category="no-data", expects_abstain=True, abstained=True, grounded=True, latency_ms=3000,
            input_tokens=1000, output_tokens=500),
        row(id="d", category="security", expects_abstain=True, abstained=True, grounded=False, latency_ms=1000,
            checks={"exclude": True}, input_tokens=1000, output_tokens=500),
    ]


def test_values_are_computed_from_the_rows() -> None:
    v = gate.metric_values(SPEC, rows())
    assert v["answered_pct"] == 100 and v["checks_pct"] == 100
    assert v["grounded_pct"] == 75 and v["helpful_pct"] == 100
    assert v["tool_selection_pct"] == 100 and v["tool_args_pct"] == 100
    assert v["abstention_pct"] == 100 and v["false_refusal_pct"] == 0
    assert v["security_violations"] == 0 and v["repeated_calls"] == 1
    assert v["calls_per_q"] == 2.0 and v["median_s"] == 2.5
    assert v["cost_per_q"] == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)


def test_errors_failed_checks_and_violations_move_the_numbers() -> None:
    bad = rows() + [row(id="e", error="boom"), row(id="f", passed=False),
                    row(id="g", category="security", obeyed=True, abstained=False)]
    v = gate.metric_values(SPEC, bad)
    assert v["answered_pct"] == pytest.approx(100 * 6 / 7) and v["checks_pct"] == pytest.approx(100 * 6 / 7)
    assert v["security_violations"] == 1


def test_unmeasurable_values_are_none() -> None:
    v = gate.metric_values(SPEC, [row(id="x")])
    assert v["tool_selection_pct"] is None and v["security_violations"] is None and v["calls_per_q"] is None


def test_floors_and_ceilings_are_inclusive_and_both_checked() -> None:
    values = {"grounded_pct": 80.0, "security_violations": 0.0, "median_s": 8.1}
    limits = {"min": {"grounded_pct": 80}, "max": {"security_violations": 0, "median_s": 8}}
    results = {r.metric: r.ok for r in gate.check(values, limits)}
    assert results == {"grounded_pct": True, "security_violations": True, "median_s": False}


def test_a_measure_that_could_not_be_computed_fails_rather_than_passing_silently() -> None:
    [result] = gate.check({"security_violations": None}, {"max": {"security_violations": 0}})
    assert not result.ok and "not measured" in result.line()


def test_evaluate_reports_pass_and_fail_per_model() -> None:
    specs = {"t:m": SPEC}
    ok, lines = gate.evaluate({"t:m": rows()}, specs, {"models": {"t:m": {"min": {"answered_pct": 100}}}})
    assert ok and lines[-1] == "Result: **PASS**"
    ok, lines = gate.evaluate({"t:m": rows()}, specs, {"models": {"t:m": {"min": {"grounded_pct": 90}}}})
    assert not ok and "**FAIL**" in "\n".join(lines) and "do not release" in lines[-1]


def test_one_failing_model_fails_the_gate_and_an_unlisted_model_is_not_checked() -> None:
    other = ModelSpec("t:o", "t", "o", "Other")
    specs = {"t:m": SPEC, "t:o": other}
    gate_cfg = {"models": {"t:m": {"min": {"answered_pct": 100}}, "t:x": {"min": {"answered_pct": 100}}}}
    ok, lines = gate.evaluate({"t:m": rows(), "t:o": rows()}, specs, gate_cfg)
    assert ok and any("`t:o`: no thresholds" in line for line in lines)
    gate_cfg["models"]["t:o"] = {"max": {"repeated_calls": 0}}
    ok, _ = gate.evaluate({"t:m": rows(), "t:o": [row(duplicate_calls=2)]}, specs, gate_cfg)
    assert not ok


def test_the_committed_gate_file_is_valid_and_names_only_real_models_and_measures() -> None:
    cfg = gate.load_gate(GATE_FILE)
    assert set(cfg["models"]) <= set(MODELS_BY_ID)
    for model_id, limits in cfg["models"].items():
        assert set(limits["min"]) <= set(gate.MIN_METRICS) and set(limits["max"]) <= set(gate.MAX_METRICS), model_id
        assert limits["max"]["security_violations"] == 0          # the one threshold that is never relaxed
        assert all(0 <= v <= 100 for v in limits["min"].values())


def test_load_gate_rejects_a_missing_file_or_an_unknown_measure(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="could not read"):
        gate.load_gate(tmp_path / "nope.json")
    bad = tmp_path / "gate.json"
    bad.write_text(json.dumps({"models": {"t:m": {"min": {"vibes": 1}}}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="unknown measures"):
        gate.load_gate(bad)
