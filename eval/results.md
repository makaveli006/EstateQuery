# Evaluation results

Run on 2026-11-19 with 25 questions per model. Judge: `claude-sonnet-5-5`. Regenerate with `uv run estatequery eval`.

| Model | Via | Answered | Checks passed | Grounded | Helpful | Median latency (s) | Cost per question |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 5.5 | Anthropic | 25/25 | 100% | 92% | 100% | 4.0 | $0.0174 |

- **Checks passed**: deterministic checks (right tool called, expected values from the database present in the answer, forbidden content absent).
- **Grounded / Helpful**: judged by Claude against the tool results the model actually received.

## Failures

- `dg-oman-projects` on Claude Sonnet 5.5: not grounded. The claim that Most are in Muscat, within the AIDA development is loosely supported, but the evidence lists all 14 as Muscat/AIDA, so 'most' is fine; the unsupported part is the 'Apartments' grouping, which places Marriott and The Great Escape under it while the evidence ties Sunrise Haven, Amour Sans Detour and others to AIDA only by city labels, so some projects like Sunrise Haven are wrongly described as 'AIDA and related villas' though the data shows only Muscat as city.
- `cross-jeddah` on Claude Sonnet 5.5: not grounded. The answer says "five DarGlobal projects in Jeddah" but then lists six, and the evidence shows six Jeddah projects (Amaya, La Collina, Padel Living, Trump Executive, Trump Park, Trump Tower), so the count is wrong.
