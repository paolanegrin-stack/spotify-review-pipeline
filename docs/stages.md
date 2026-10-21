# Verify, group and rank

All Python commands use the project virtual environment. Imports, ranking,
dry-runs, and the planted-error comparison make no API calls.

```powershell
.venv\Scripts\python -m src.verify --run-id smoke_test --sample-size 20 --dry-run
.venv\Scripts\python -m src.group --run-id smoke_test --dry-run
.venv\Scripts\python -m src.rank --run-id smoke_test
```

Verifier sampling uses config.VERIFY_SAMPLE_SEED and a sorted population of
completed records under the active enrichment label_config. Requested size is
clamped to the available completed population and disclosed. Pilot default is
20; specify --sample-size 2000 for a later full run. A live verifier run saves
verify/sample.json with the seed, requested/actual sizes, source population
fingerprint and selected IDs. Only text and local IDs reach the verifier;
enrichment predictions remain local for comparison. Verification does not
overwrite enrichment labels, statuses, or attempt counts.

verify/verifier_predictions.jsonl and verify/disagreement_report.md are exported
after each saved batch. Reports disclose missing predictions and calculate
agreement only over compared records. The report includes all five field
comparisons and severity mean absolute error.

After real verifier predictions exist, the following uses a synthetic in-memory
copy and the saved verifier prediction, with no new call or business-table write:

```powershell
.venv\Scripts\python -m src.verify --run-id smoke_test --planted-error-test
```

No verifier labels or issue descriptions were invented during development.
Dry-runs do not create them. Unit tests use explicitly synthetic mock responses.

Group membership is exactly one ISS-TOPIC per completed complaint/cancellation.
The evidence pack is the first at most 30 members by review_id, deterministically
sorted. One request names one issue. Model representatives must come from that
pack; the model cannot modify membership or counts. issues.json contains only
validated, saved issue names/descriptions and code-computed member counts.
Progress messages disclose partial naming. membership.csv is code-only.

Rank reads membership.csv and the completed SQLite records. If membership.csv
does not yet exist, it creates the same deterministic membership without calling
the naming model. Existing membership must match the complete code-defined set;
stale, incomplete, duplicate or foreign pairs are rejected. Ranking uses integer
severity sums, six-decimal Decimal half-up means, and issue-ID tie breaking.

Live verify/group execution requires an explicit positive --max-spend-usd;
default is zero. The cap applies to total spend/reservations across all roles
in the run, including earlier enrichment. Every attempt uses the existing
SQLite ledger and calls.jsonl with role verify or group. Retries are bounded;
permanent schema errors stop without retry, and invalid model outputs are
logged as failed and rejected. Missing usage stays visibly unknown.

stage_cache stores each validated batch atomically in SQLite, keyed by the exact
request (including model, effort, schema, prompt and inputs) and source hashes.
On restart those results are re-exported without new calls. JSON/CSV/Markdown
exports use main-thread atomic replacement with bounded PermissionError retries;
SQLite is authoritative. Call and run logs use append/flush/close under one
process-wide lock. In-flight attempts persist in SQLite; restart appends any
missing events as failed with unknown usage, without rewriting existing lines.
All live roles share .enrich.lock to prevent simultaneous runs on one database.
Dry-runs read the database without migrations or writes and print final summaries.
