"""Evaluation harness: run a fixed question set through the real agent loop for one or
more models and score every answer two ways.

1. Deterministic checks, per question (no model involved):
     expect_tools      at least one of these tools was called
     expect_no_tools   no tool was called (off-topic questions)
     include_any       the answer contains at least one of these strings
     exclude           the answer contains none of these strings
     sql               every value this query returns from the database appears in the answer
2. A Claude judge that reads the question, the tool results the model saw and the answer,
   and decides whether the answer is grounded in that evidence and whether it is helpful.

Scores are attached to each run's Langfuse trace, and a summary table is written to
eval/results.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import statistics
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from estatequery import observability
from estatequery.agent.loop import run_turn
from estatequery.agent.prompts import system_prompt
from estatequery.agent.tools import TOOL_SPECS, connect
from estatequery.config import MODELS_BY_ID, ModelSpec, get_settings

JUDGE_MODEL = "claude-sonnet-5-5"
QUESTIONS_PATH = Path("eval/questions.jsonl")
RESULTS_MD = Path("eval/results.md")
RESULTS_JSON = Path("eval/results.json")
CONCURRENCY = 3
TOOL_SUMMARY = json.dumps(
    [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]["properties"]} for t in TOOL_SPECS],
    ensure_ascii=False,
)

JUDGE_INSTRUCTIONS = """You are grading one answer from a real-estate assistant. The assistant may only state facts that appear in the tool results it received, shown to you as evidence.

grounded: true when every factual claim in the answer (prices, counts, names, locations, amenities, dates) is supported by the evidence, the standing context or the tool descriptions, or when the answer correctly says the data does not contain what was asked. Declining an off-topic question is grounded. Any invented or contradicted fact makes it false. General framing sentences and the standard source line are not claims.

helpful: true when the answer addresses what the user asked as well as the evidence allows: it gives the requested facts, or clearly explains that the data has no match, or declines an off-topic or advice request politely. An answer that dodges a question the evidence could answer is not helpful.

Grade only against the evidence shown, not against your own knowledge of the companies."""


class Verdict(BaseModel):
    grounded: bool
    helpful: bool
    reason: str = Field(description="One sentence naming the decisive evidence or the unsupported claim.")


def normalise(text: str) -> str:
    return re.sub(r"(?<=\d)[,   ](?=\d{3})", "", text.lower())


def sql_values(conn: sqlite3.Connection, query: str) -> list[str]:
    values: list[str] = []
    for row in conn.execute(query).fetchall():
        for value in row:
            if value is None:
                continue
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            values.append(str(value).lower())
    return values


def deterministic_checks(question: dict[str, Any], answer: str, tools_used: list[str],
                         conn: sqlite3.Connection) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    text = normalise(answer)
    if question.get("expect_tools"):
        checks["tool"] = any(t in tools_used for t in question["expect_tools"])
    if question.get("expect_no_tools"):
        checks["no_tool"] = not tools_used
    if question.get("include_any"):
        checks["include"] = any(normalise(s) in text for s in question["include_any"])
    if question.get("exclude"):
        checks["exclude"] = not any(normalise(s) in text for s in question["exclude"])
    if question.get("sql"):
        expected = sql_values(conn, question["sql"])
        checks["sql"] = bool(expected) and all(v in text for v in expected)
    return checks


async def judge(client: anthropic.AsyncAnthropic, question: str, evidence: list[dict[str, Any]],
                answer: str) -> Verdict | None:
    shown = json.dumps(evidence, ensure_ascii=False)[:60000]
    # The assistant's standing instructions state dataset facts (sample size, snapshot date,
    # cities) and its tool descriptions name the countries covered. Those are legitimate
    # sources for an answer, so the judge has to see them too.
    content = (f"Standing context the assistant was given:\n{system_prompt()}\n\n"
               f"Tool descriptions the assistant was given:\n{TOOL_SUMMARY}\n\n"
               f"Question:\n{question}\n\nEvidence (tool calls and results):\n{shown or '[] (no tools were called)'}"
               f"\n\nAnswer to grade:\n{answer}")
    try:
        response = await client.messages.parse(
            model=JUDGE_MODEL,
            max_tokens=2000,
            system=JUDGE_INSTRUCTIONS,
            messages=[{"role": "user", "content": content}],
            output_format=Verdict,
            output_config={"effort": "medium"},
        )
    except anthropic.APIStatusError as exc:
        print(f"    judge failed ({exc.status_code})")
        return None
    return response.parsed_output if response.stop_reason == "end_turn" else None


async def run_one(spec: ModelSpec, question: dict[str, Any], judge_client: anthropic.AsyncAnthropic,
                  conn: sqlite3.Connection, run_name: str) -> dict[str, Any]:
    answer, tools_used, evidence, done, error = "", [], [], {}, None
    async for event in run_turn(spec=spec, history=[], user_message=question["question"],
                                session_id=f"eval-{run_name}", tags=["eval", run_name],
                                emit_tool_results=True):
        if event["type"] == "tool_end":
            evidence.append({"tool": event["tool"], "args": event.get("args"), "result": event.get("result")})
        elif event["type"] == "done":
            done, answer, tools_used = event, event["answer"], event["tools_used"]
        elif event["type"] == "error":
            error = event["message"]

    row: dict[str, Any] = {
        "id": question["id"], "category": question["category"], "model": spec.id,
        "answer": answer, "error": error, "tools_used": tools_used,
        "latency_ms": done.get("latency_ms"), "input_tokens": done.get("input_tokens", 0),
        "output_tokens": done.get("output_tokens", 0), "trace_id": done.get("trace_id"),
    }
    if error:
        row.update(checks={}, passed=False, grounded=None, helpful=None, reason=error)
        return row

    checks = deterministic_checks(question, answer, tools_used, conn)
    verdict = await judge(judge_client, question["question"], evidence, answer)
    row.update(
        checks=checks,
        passed=all(checks.values()),
        grounded=verdict.grounded if verdict else None,
        helpful=verdict.helpful if verdict else None,
        reason=verdict.reason if verdict else "judge unavailable",
    )
    if row["trace_id"]:
        observability.score(row["trace_id"], "eval-checks-passed", float(row["passed"]),
                            ", ".join(f"{k}={v}" for k, v in checks.items()))
        if verdict:
            observability.score(row["trace_id"], "eval-grounded", float(verdict.grounded), verdict.reason)
            observability.score(row["trace_id"], "eval-helpful", float(verdict.helpful))
    return row


def rate(rows: list[dict[str, Any]], key: str) -> str:
    values = [r[key] for r in rows if r[key] is not None]
    return f"{100 * sum(values) / len(values):.0f}%" if values else "n/a"


def summarise(spec: ModelSpec, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r["error"]]
    latencies = [r["latency_ms"] for r in ok if r["latency_ms"]]
    cost = sum(r["input_tokens"] * spec.price_in + r["output_tokens"] * spec.price_out for r in ok) / 1e6
    return {
        "model": spec.label,
        "provider": spec.note or spec.provider,
        "answered": f"{len(ok)}/{len(rows)}",
        "checks": rate(rows, "passed"),
        "grounded": rate(ok, "grounded"),
        "helpful": rate(ok, "helpful"),
        "median_s": f"{statistics.median(latencies) / 1000:.1f}" if latencies else "n/a",
        "cost_per_q": f"${cost / len(ok):.4f}" if ok and (spec.price_in or spec.price_out) else ("free" if ok else "n/a"),
    }


def write_report(summaries: list[dict[str, Any]], rows: list[dict[str, Any]], n_questions: int) -> None:
    lines = [
        "# Evaluation results",
        "",
        f"Run on {date.today().isoformat()} with {n_questions} questions per model. "
        f"Judge: `{JUDGE_MODEL}`. Regenerate with `uv run estatequery eval`.",
        "",
        "| Model | Via | Answered | Checks passed | Grounded | Helpful | Median latency (s) | Cost per question |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(f"| {s['model']} | {s['provider']} | {s['answered']} | {s['checks']} | {s['grounded']} "
                     f"| {s['helpful']} | {s['median_s']} | {s['cost_per_q']} |")
    lines += [
        "",
        "- **Checks passed**: deterministic checks (right tool called, expected values from the database "
        "present in the answer, forbidden content absent).",
        "- **Grounded / Helpful**: judged by Claude against the tool results the model actually received.",
        "",
        "## Failures",
        "",
    ]
    failures = [r for r in rows if r["error"] or not r["passed"] or r["grounded"] is False or r["helpful"] is False]
    if not failures:
        lines.append("None.")
    for r in failures:
        failed = [k for k, v in r.get("checks", {}).items() if not v]
        what = r["error"] or "; ".join(filter(None, [
            f"failed checks: {', '.join(failed)}" if failed else "",
            "not grounded" if r["grounded"] is False else "",
            "not helpful" if r["helpful"] is False else "",
        ]))
        lines.append(f"- `{r['id']}` on {MODELS_BY_ID[r['model']].label}: {what}. {r.get('reason') or ''}".rstrip())
    RESULTS_MD.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RESULTS_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


async def run(model_ids: list[str], limit: int | None) -> None:
    settings = get_settings()
    questions = [json.loads(line) for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        questions = questions[:limit]
    judge_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)
    conn = connect()
    run_name = date.today().strftime("%Y%m%d")
    all_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    try:
        for model_id in model_ids:
            spec = MODELS_BY_ID[model_id]
            print(f"\n== {spec.label} ({len(questions)} questions)", flush=True)
            semaphore = asyncio.Semaphore(CONCURRENCY)

            async def guarded(question: dict[str, Any], spec: ModelSpec = spec) -> dict[str, Any]:
                async with semaphore:
                    row = await run_one(spec, question, judge_client, conn, run_name)
                mark = "ok " if row["passed"] and row["grounded"] and row["helpful"] else "FAIL"
                print(f"  [{mark}] {row['id']}: {row.get('reason') or ''}"[:200], flush=True)
                return row

            rows = await asyncio.gather(*(guarded(q) for q in questions))
            all_rows += rows
            summaries.append(summarise(spec, rows))
    finally:
        conn.close()
        observability.flush()
    write_report(summaries, all_rows, len(questions))
    print(f"\nwrote {RESULTS_MD} and {RESULTS_JSON}")
    for s in summaries:
        print(" ", s)


def main(argv: list[str] | None = None) -> None:
    default_models = ["anthropic:claude-sonnet-5-5"]
    parser = argparse.ArgumentParser(description="Evaluate the agent on the question set.")
    parser.add_argument("--models", default=",".join(default_models), help="comma-separated model ids")
    parser.add_argument("--limit", type=int, default=None, help="only the first N questions")
    args = parser.parse_args(argv)
    model_ids = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [m for m in model_ids if m not in MODELS_BY_ID]
    if unknown:
        raise SystemExit(f"unknown model ids: {unknown}. Known: {sorted(MODELS_BY_ID)}")
    asyncio.run(run(model_ids, args.limit))


if __name__ == "__main__":
    main()
