# Evaluation results

Run on 2026-11-19 with 25 questions per model. Judge: `claude-sonnet-5-5`. Regenerate with `uv run estatequery eval`.

| Model | Via | Answered | Checks passed | Grounded | Helpful | Median latency (s) | Cost per question |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 5.5 | Anthropic | 25/25 | 100% | 96% | 100% | 4.6 | $0.0171 |

- **Checks passed**: deterministic checks (right tool called, expected values from the database present in the answer, forbidden content absent).
- **Grounded / Helpful**: judged by Claude against the tool results the model actually received.

## Failures

- `dg-oman-projects` on Claude Sonnet 5.5: not grounded. The claim 'Most are in Muscat' is a summary not stated outright: 12 list Muscat, 2 (Azure Oceanfront Villas) show only 'AIDA' as the city, so it is roughly supported; but 'several are within the AIDA development' and the grouping of Sunrise Haven under 'AIDA and related villas' is not supported, since its city is just Muscat with no AIDA link in the evidence.the Marriott and Trump grouping is fine.
