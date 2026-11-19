"""Release gate: compare an evaluation run with agreed thresholds and fail on a regression.

`eval/gate.json` holds, per model, floors ("min") and ceilings ("max") for named measures. The
thresholds are set below the measured values by a margin, because the judge is a model and its
grounded score moves by a few points between identical runs. The gate is a tripwire for real
regressions (a prompt change that breaks tool use, a security failure, a cost or latency jump),
not a precise score.

    uv run estatequery eval --models <ids> --gate      exits 1 when any threshold is missed

The checks are pure functions of the evaluation rows, so they are unit tested without any model.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from estatequery.config import ModelSpec

GATE_PATH = Path("eval/gate.json")

MIN_METRICS = ("answered_pct", "checks_pct", "grounded_pct", "helpful_pct", "tool_selection_pct",
               "tool_args_pct", "abstention_pct", "completion_pct")
MAX_METRICS = ("false_refusal_pct", "security_violations", "repeated_calls", "calls_per_q", "median_s",
               "cost_per_q")


@dataclass(frozen=True)
class GateResult:
    metric: str
    value: float | None
    bound: float
    kind: str          # "min" or "max"
    ok: bool

    def line(self) -> str:
        shown = "not measured" if self.value is None else f"{self.value:g}"
        relation = ">=" if self.kind == "min" else "<="
        return f"| {self.metric} | {shown} | {relation} {self.bound:g} | {'pass' if self.ok else '**FAIL**'} |"


def pct(numerator: float, denominator: int) -> float | None:
    return 100 * numerator / denominator if denominator else None


def metric_values(spec: ModelSpec, rows: list[dict[str, Any]]) -> dict[str, float | None]:
    """The gate's measures, computed the same way the report computes its table."""
    ok = [r for r in rows if not r["error"]]
    judged = [r for r in ok if r.get("grounded") is not None]
    needs_tools = [r for r in ok if r["needs_tools"]]
    with_args = [r for r in ok if "args" in r["checks"]]
    should_abstain = [r for r in ok if r["expects_abstain"] and r["abstained"] is not None]
    should_answer = [r for r in ok if not r["expects_abstain"] and r["abstained"] is not None]
    security = [r for r in ok if r["category"] == "security"]
    latencies = [r["latency_ms"] for r in ok if r.get("latency_ms")]
    calls = [len(r["tools_used"]) for r in needs_tools]
    cost = sum(r.get("input_tokens", 0) * spec.price_in + r.get("output_tokens", 0) * spec.price_out for r in ok) / 1e6

    def violated(r: dict[str, Any]) -> bool:
        return bool(r.get("obeyed")) or not all(r["checks"].get(k, True) for k in ("exclude", "no_leak"))

    return {
        "answered_pct": pct(len(ok), len(rows)),
        "checks_pct": pct(sum(bool(r["passed"]) for r in rows), len(rows)),
        "grounded_pct": pct(sum(bool(r.get("grounded")) for r in judged), len(judged)),
        "helpful_pct": pct(sum(bool(r.get("helpful")) for r in judged), len(judged)),
        "tool_selection_pct": pct(sum(r["checks"].get("tool", False) for r in needs_tools), len(needs_tools)),
        "tool_args_pct": pct(sum(r["checks"]["args"] for r in with_args), len(with_args)),
        "abstention_pct": pct(sum(bool(r["abstained"]) for r in should_abstain), len(should_abstain)),
        "completion_pct": pct(sum(bool(r["passed"] and r.get("helpful")) for r in ok), len(ok)),
        "false_refusal_pct": pct(sum(bool(r["abstained"]) for r in should_answer), len(should_answer)),
        "security_violations": float(sum(violated(r) for r in security)) if security else None,
        "repeated_calls": float(sum(r.get("duplicate_calls", 0) for r in ok)),
        "calls_per_q": statistics.mean(calls) if calls else None,
        "median_s": statistics.median(latencies) / 1000 if latencies else None,
        "cost_per_q": cost / len(ok) if ok and (spec.price_in or spec.price_out) else None,
    }


def check(values: dict[str, float | None], limits: dict[str, Any]) -> list[GateResult]:
    """Every floor and ceiling in `limits` against the measured values. A measure that could not
    be computed (for example no security questions ran) fails rather than passing silently."""
    results: list[GateResult] = []
    for kind in ("min", "max"):
        for metric, bound in (limits.get(kind) or {}).items():
            value = values.get(metric)
            ok = value is not None and (value >= bound if kind == "min" else value <= bound)
            results.append(GateResult(metric, value, float(bound), kind, ok))
    return results


def load_gate(path: Path = GATE_PATH) -> dict[str, Any]:
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read the release gate {path}: {exc}") from exc
    unknown = {m for entry in gate.get("models", {}).values() for kind in ("min", "max")
               for m in entry.get(kind, {})} - set(MIN_METRICS) - set(MAX_METRICS)
    if unknown:
        raise SystemExit(f"{path} names unknown measures: {sorted(unknown)}")
    return gate


def evaluate(rows_by_model: dict[str, list[dict[str, Any]]], specs: dict[str, ModelSpec],
             gate: dict[str, Any]) -> tuple[bool, list[str]]:
    """(passed, report lines) for every model in the run that has thresholds."""
    lines = ["", "## Release gate", ""]
    passed = True
    for model_id, rows in rows_by_model.items():
        limits = gate.get("models", {}).get(model_id)
        if limits is None:
            lines += [f"`{model_id}`: no thresholds in the gate file, not checked.", ""]
            continue
        results = check(metric_values(specs[model_id], rows), limits)
        model_ok = all(r.ok for r in results)
        passed &= model_ok
        lines += [f"### {specs[model_id].label}: {'PASS' if model_ok else 'FAIL'}", "",
                  "| Measure | Value | Threshold | Result |", "|---|---|---|---|", *(r.line() for r in results), ""]
    lines.append("Result: **PASS**" if passed else "Result: **FAIL**, do not release.")
    return passed, lines
