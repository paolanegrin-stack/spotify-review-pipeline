# Offline cost calculator

Default offline replay after the cold and warm invocations:

```powershell
.venv\Scripts\python -m cost.calculator --runs pilot_cold
```

Separate run folders are also supported: `--runs pilot_cold pilot_warm`.
Optional estimate settings: `--budget-usd 40 --workers 20`. Workers here are a
modeled speedup, not a request to start workers or make API calls.

Explicit paid pilot execution (run these yourself, after confirming rates):

```powershell
if (Test-Path -LiteralPath runs/pilot_cold) { throw 'Use a fresh pilot_cold directory for the cold pilot.' }
.venv\Scripts\python -m src.pipeline --input data/cost_100.csv --run-id pilot_cold --workers 1 --verify-sample 20 --max-spend-usd 0.50 --execute
if ($LASTEXITCODE -ne 0) { throw 'Cold pilot incomplete; inspect its logs before continuing.' }
Copy-Item -LiteralPath runs/pilot_cold/run_summary.json -Destination runs/pilot_cold/run_summary_cold.json

# Warm invocation: same run ID and settings, preserving every result cache.
.venv\Scripts\python -m src.pipeline --input data/cost_100.csv --run-id pilot_cold --workers 1 --verify-sample 20 --max-spend-usd 0.50 --execute
if ($LASTEXITCODE -ne 0) { throw 'Warm pilot incomplete; inspect its logs.' }
Copy-Item -LiteralPath runs/pilot_cold/run_summary.json -Destination runs/pilot_cold/run_summary_warm.json
```

The calculator separates cold/warm invocations using run_log.jsonl invocation
IDs and each call's timestamp. The second invocation must show zero new enrich
calls; the report prints PASS or FAIL. Original initial/resume call phases are
preserved; exports add pilot_phase=cold/warm and run_id. Summary snapshots retain
each invocation's measured total time and counts. Without a matching snapshot,
the calculator reports the logged stage span and current database counts.

Outputs: report.md, usage.csv, pilot_records.jsonl (one final record per pilot
ID), pilot_calls.jsonl. The supplied 100-review checksum is checked. Pending
classifications prevent a final records export; finish the pilot first.

API cost = ((input - cached_input) × input_rate + cached_input × cached_rate
+ output × output_rate) / 1,000,000. Reasoning is already included in output.
Measured cost is the known subtotal plus a count/list of calls with unknown
usage. Estimates price all projected attempts at the known per-call average;
that does not resolve the historical unknown charge. Infrastructure cost is UNKNOWN.
Rates retain their source URLs/dates in usage.csv; the current values still
carry the note "to confirm against the official page".

Full-run estimates use each stage's measured work: enrichment scales to exact
texts or all nonempty rows, verification to 2,000 reviews, grouping/memo once.
Conservative estimates add 20% API retries. Enrich seconds per call are divided
by the modeled workers. Time does not depend on token prices. No network, API
key or package installation is needed for replay, and importing makes no calls.

The calculator also accepts `--runs ckpt_500` for checkpoint_500.csv. Its exports
use the ckpt_500_ prefix so the real 100-review pilot artifacts are preserved.
Live roles use a 120-second timeout and one shared 2,000 RPM / 1,200,000 TPM
limiter. Token admissions use the input byte bound plus output cap. The shared
SQLite ledger reserves spend before dispatch; progress prints every 30 seconds.
