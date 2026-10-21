# Provisional rates

rates.csv records user-supplied rates of $0.10 input, $0.01 cached input and
$0.50 output per million tokens, with the official pricing-page URL and
checked_date 2026-10-20. The note is **to confirm against the official page**;
the date and rates have not been independently verified online in this task.

No pilot has been executed. Dry-run needs no credentials and makes no calls:

```powershell
.venv\Scripts\python -m src.enrich --run-id pilot_dry --dry-run
```

Live execution requires an explicit positive --max-spend-usd (default 0).
Reservations use an intentionally conservative UTF-8 byte count plus overhead
as a byte-based input-token upper bound, and the full configured output cap. This is
an estimate, not a provider billing guarantee. Unknown charges keep their full
reservation across restarts. If the cached-input rate is missing, any call
reporting cached tokens also retains its full reservation and marks cost
unknown. Real token counts come only from API response usage fields; the
dry-run byte bound is never written as measured usage. No missing usage or
cached rate is silently counted as zero.
The ledger is shared by all workers and survives restart; budget totals cover
all configurations in the run. SDK retries are disabled; explicit retries are
individually reserved and logged. Reconcile uncertain charges before scaling.

Only one enrichment process may own a run at a time. A leftover .enrich.lock
after a forced termination requires inspection before removal. SQLite call
events are the durable log; calls.jsonl is an atomically regenerated view.
Requests, schemas, responses and local-ID maps are saved in call events.
The dry-run output is only a request preview, not measured usage or labels.

SDK integration was inspected locally in installed openai 3.35.1:
resources/responses/responses.py accepts `reasoning` and `text`;
types/shared_params/reasoning.py accepts `effort="none"`;
types/responses/response_format_text_json_schema_config_param.py defines
`type="json_schema"`, `name`, `schema`, and `strict=True` inside `text.format`.
No online documentation or model-availability check was performed.
The full request preview includes the exact system instructions, local-ID user
message and schema. --limit counts distinct texts; aliases can complete more
original review IDs. Results and direct cache provenance live in
enrichment_results, joined to records for source identity and status.
