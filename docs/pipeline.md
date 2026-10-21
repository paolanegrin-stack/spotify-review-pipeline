# Single-command pipeline and memo

The default mode never makes API calls, even with a positive spend limit:

```powershell
.venv\Scripts\python -m src.pipeline --input data/cost_100.csv --run-id pipeline_dry
```

Stages run in order: prepare, enrich, verify, group, rank, memo. Model-stage
output is captured in enrich_preview.txt, verify_preview.txt, group_preview.txt
and memo_preview.txt when --execute is absent. Downstream stages explicitly
report unavailable completed evidence on a fresh run; previews are not model
results. Code still generates deterministic membership, ranking and claims.

Only an explicit --execute permits calls. --max-spend-usd is a run-wide cap,
including previous spend and unknown-charge reservations across every role.
Default cap is zero. --workers controls enrichment concurrency; verification,
grouping and memo remain sequential. --verify-sample defaults to the declared
pilot sample size. Re-run the same input/run ID to resume saved batches;
different input bytes require a new run ID.

pipeline_state.json fingerprints exact input, configuration, prompts, relevant
source code, classifications, upstream evidence and exported file hashes.
Unchanged successful stages are reused. Incomplete or stopped stages resume;
dry-run previews are never accepted as live outputs. run_log.jsonl appends one
entry per stage per invocation with timestamps, duration, outcome, reuse,
calls, real usage, estimated cost and unresolved-usage indicators.
run_summary.json distinguishes current-invocation and lifetime spend/usage and
records the configuration, model IDs, prompt versions, label_config and scope.
This is execution evidence, not a grading export or a completed cost calculator.

Memo uses gpt-6-luna with low effort. claims.csv preserves the exact ranking
strings and assigns C1 onward, four metrics per issue in ranking order.
The bounded evidence pack contains at most eight ranked issues, saved names
and descriptions, at most three representative quotes per issue, run totals
and available verifier agreement. It never contains the full source dataset.

Memo generation requires three observed eligible issues to support a priority
and two alternatives among access, usability, playback and billing/support.
Insufficient evidence is disclosed and blocks a live memo rather than inventing
issues. The no-execute preview remains available with an empty evidence pack.

memo_v2 supplies verbatim citation snippets for issue metrics [C#] and run totals
[T#]. Every number must be copied with its entire snippet and citation; other
years, ranks, percentages or counts are rejected. T5 is verifier topic agreement;
unavailable agreement is described qualitatively. IDs are excluded from number scanning.
Unknown claims, reviews and issues are rejected. This deterministic check does
not prove that prose, causal explanations or recommendations are sound.

Failed memo validation is retried twice with the exact error list, using the shared call
log/ledger for each attempt. A third failure leaves memo.md headed FAILED
CHECKS and memo_check.json with passed=false and the problems. Only validated
memos enter the saved request cache. Unknown charges retain reservations.
Memo role/effort and input-artifact hashes are included in its call events.

Standalone preview:

```powershell
.venv\Scripts\python -m src.memo --run-id smoke_test --dry-run
```

A prior ranking.csv is required. Claim generation is code-only even in dry-run.
No paid memo or pipeline execution was performed during Step 13 development.
