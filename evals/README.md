# Evaluation protocol

- `dev.json`: 12 technical + 4 paper + 4 general briefs. `heldout.json`: 6 + 2 + 2. Do not tune prompts against heldout results.
- These are research briefs and human rubrics, not an answer-key dataset. Human factual correctness and paper explanation scores start as `null`.
- `research evaluate` defaults to explicit synthetic fixture mode. Fixture results verify workflow plumbing only. They never establish model quality, speedup or research usefulness.
- Real controlled experiments require `--live --corpus-manifest artifacts/corpus.json` and a configured DeepSeek key. Freeze relevant original papers/docs with `research freeze` first, inspect parser warnings, and use the same manifest for every arm. Uploaded source IDs are tenant-specific.
- B0: one iterative Researcher with the whole brief and the same total tool/model/token/dollar limits. B1: task decomposition and multiple Researchers without global gap search. B2: adds Reviewer-driven global gap search. All arms use the same final writing/review/citation checks.
- Ablate B2 `--concurrency 1` vs `3`, and `--context full` vs `evidence`. Full text is included in stable source order until the shared input budget is reached; omitted source IDs and estimated sizes are recorded, never silently discarded.
- Use 3 repetitions per heldout case for the full campaign. The initial $10 campaign cap includes all actual retries and unresolved reservations; a larger study may remain unexecuted within this budget.
- Record versions, corpus hashes, price table, model settings, actual usage, wall time, failures and unresolved questions. Score external assertions across report paragraphs, tables and paper fields, including facts missing from the Claim table. Reference presence is not citation precision.
- Human sheet per report: total sampled factual assertions; supported/contradicted/unknown; citation entailment; coverage against rubric; principles/implementation/experiment fidelity (1–5); limitation attribution; research idea testability (1–5). Preserve denominator, reviewer identity/date and disagreements.
- Live web research is a separate ecological test, not a controlled architecture comparison: changing pages and search ranking prevent attributing all differences to the agents.
- Run evaluation with the standalone worker stopped and no other queued runs. Use the dedicated project database.

Example (API key supplied via environment, never committed):

```sh
research evaluate --split dev --variant B2 --limit 2 --output artifacts/fixture.jsonl
research freeze evals/local-source-urls.txt --output artifacts/corpus.json
research evaluate --live --corpus-manifest artifacts/corpus.json --split dev --variant B0 --limit 1
research evaluate --live --corpus-manifest artifacts/corpus.json --split dev --variant B2 --limit 1
```


## Reproducing the fixture sweep

With `.local/test-tenants.jsonl` seeded, PostgreSQL/MinIO running and the standalone worker stopped, run `.venv/bin/python scripts/evaluate_fixtures.py` from the repository root. It executes B2 on all 20 dev briefs plus one case each for B0, B1, B2 serial and B2 full context. Output is written to a timestamped `artifacts/fixture-evaluation/` directory. Heldout is not used by this smoke suite.

The final 2026-09-11 sweep completed all 24 cases with `quality_status=unchecked`, zero report-gate errors and no paid calls. This verifies the harness, not model quality. `report_gate_errors` counts all automated publication defects; `citation_locator_errors` counts only original-text version/offset/hash verification failures. Older live metrics used the latter name for all gate errors; consult the verification report when comparing records.
