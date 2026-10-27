# Expanded assessment evidence — 2026-10-27

This archive contains the preceding round-14 independent assessment of the same runtime, data and case files as the submission. Documentation and reports were updated afterwards. See ../manifest.json for portable file hashes and ../../../TEST_REPORT.md for interpretation.

- `evidence/`: 120 real-model repetitions + 17 controlled components. C03.1_r0 is the excluded connectivity probe.
- `supplemental/` and `pinned/supplemental/`: controlled boundary evidence, including failures; these are not natural-model pass rates.
- `test_cases.json`, `adjudication.json`, `scored_results.json`: frozen custom rubric, decisions and score. This is not an official take-home score.
- `frontend-check/results.json`: 11 browser checks with an isolated backend fixture, not live-model end-to-end checks.
- `harness/`: original assessment source for audit. It expects the original `assessment/runs/<run>/` layout, `assessment/test_cases.json`, separate fixtures/output folders and, for browser tests, Playwright/Chrome. These are archival sources, not the README quick-start command. The supported portable rerun is the 15-case `evals/runner.py`.

Historical absolute workspace paths, previous-round comparisons and raw SHA256 values in the archived metadata describe the original local run. Portable submission integrity uses normalized-LF SHA256 values in ../manifest.json instead. Not every original auxiliary artifact is included; full dialogue evidence, rating decisions and failures are retained. No API keys or .env contents are included. Any temporary passwords are synthetic fixture outputs.
