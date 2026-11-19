# Evaluation results

Run on 2026-11-19 with 25 questions per model. Judge: `claude-sonnet-5-5`. Regenerate with `uv run estatequery eval`.

| Model | Via | Answered | Checks passed | Grounded | Helpful | Median latency (s) | Cost per question |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 5.5 | Anthropic API | 25/25 | 100% | 92% | 100% | 3.9 | $0.0164 |

- **Checks passed**: deterministic checks (right tool called, expected values from the database present in the answer, forbidden content absent).
- **Grounded / Helpful**: judged by Claude against the tool results the model actually received.

## Failures

- `dg-oman-projects` on Claude Sonnet 5.5: not grounded. The claim that all 14 projects are in Muscat is contradicted by the evidence, since Azure Oceanfront Villas lists its city as "AIDA" rather than Muscat; the other details match the tool result.}
- `wl-no-match` on Claude Sonnet 5.5: not grounded. The no-match statement and the cities and property types (apartments, land, rooms) are supported by the tool result, but the claim that under 10,000 SAR is far below what a villa for sale normally costs comes from background knowledge, not the evidence.</br>
