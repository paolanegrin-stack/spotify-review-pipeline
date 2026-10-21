# Spotify Reviews Multi-Agent Pipeline

## 1. Summary

**Business question:** Where should the next quarter of product effort go among access, usability, playback, and billing/support?

**Final scoped run.** Prioritize **ISS-USABILITY**: complaints about limited control over playback and playlists have the strongest observed priority score among the compared issues. Evidence: “12226 complaints [C1]” and “priority score 32097 [C4]”. A representative review describes unexpected tracks in a liked-songs playlist [000c1149-e4a1-4bc0-8fb9-c17434dc67fc].

Claim IDs refer to the final [claims table](grading/claims.csv). [Final memo](docs/final_memo.md) · [Grading export](grading/) · [Live dashboard](https://spotify-review-pipeline-blue.vercel.app/) · [Dashboard setup](docs/dashboard.md)

The saved memo predates out-of-scope quarantine: its coverage references describe that earlier snapshot. Current accounting below supersedes those counts; the memo is copied unchanged and its issue-level claims are unchanged.

## 2. Scope and status

`runs/full_v1/ingestion_report.json` reports **660,622 original IDs**, **97,400,616 bytes**, and all ingestion/profile checks passed. The declared classification scope is a deterministic sample of **100,000 nonempty reviews**: order by SHA-256 of UTF-8 `seed + ':' + review_id`, ascending, using manifest seed `berkeley-fall-2026-assignment-5-v1`; select the first IDs. All **50 golden IDs** are included. [Scope method](docs/scope.md).

This is the assignment minimum declared by the operator; the checked-in [grading contract](data/GRADING_CONTRACT.md) instead requires classifying every nonempty review. **This is not full-corpus classification.**

Read-only database inspection at this snapshot shows:

| Account | Saved count / status |
|---|---:|
| Scoped classified/completed | 99,923 |
| Scoped quarantined after failures/retries | 77 |
| Empty-text quarantines outside scope | 13 |
| Out-of-scope quarantines (`not_classified_out_of_declared_scope`) | 560,609 |
| All quarantined IDs | 560,699 |
| Pending IDs | 0 |

The scoped quarantines retain their actual saved reasons: **50** `api_error:Request timed out.`, **26** `validation_failed:Quote must be a short exact source substring`, and **1** `validation_failed:Invalid JSON, refusal, or incomplete output`. The requested normalization to `enrich_failed_after_retries` changed no rows because no scoped pending records remained. There are no saved records with that normalized reason at this snapshot.

Out-of-scope quarantine is complete, without labels or model calls. All **660,622** IDs are accounted for. The last `run_summary.json` predates this code-only update; current counts come from SQLite and are preserved in [grading/run.json](grading/run.json).

## 3. Architecture

| Stage | Code or model | Model / reasoning effort | Prompt version |
|---|---|---|---|
| prepare | Code: CSV, hashes, SQLite | — | — |
| enrich | Model; code validates labels and evidence | gpt-6-luna / none | enrich_v1 |
| verify | Independent model labeling; code compares | gpt-6-luna / none | verify_v1 |
| group | Code membership; model naming/descriptions | gpt-6-luna / none | group_v1 |
| rank | Code: severity sum, Decimal means, deterministic ordering | — | — |
| memo | Model recommendation; code checks citations/numbers | gpt-6-luna / low | memo_v2 |

Enrichment now batches **50** reviews; verification batches **25**. Operational concurrency progression was **1 → 2 → 60 → 300** workers; preserved reports measure the pilot at **1** worker (**156.229045** enrich seconds), the checkpoint at **2** (**146.849784** enrich seconds), and resumed checkpoint at **300** (**208.095266** enrich seconds). The intermediate worker setting is operator history: no separate worker-specific measurement survives in the saved reports, and the 10k calculator labels both attempts with the latest worker setting. These are different scopes/batches, not a controlled scaling experiment. A shared limiter caps **1,800,000 TPM / 4,000 RPM**; a shared spend ledger reserves before dispatch. The final saved spend cap is **USD 5.00**. API retries are bounded at **3** attempts, invalid enrichment items retry once, and memo validation allows **2** retries. Exact-text results are cached under the same label configuration and propagated only within scope. SQLite batches save atomically; logs append under one process-wide lock; whole-file exports use main-thread replacement with permission-error retries. Resume skips saved completed results.

Dashboard flow: **saved run outputs → COPY loader → Neon Postgres → Vercel Python API → static dashboard**. The API is read-only and uses parameterized SQL; raw CSVs and secrets are not deployed.

## 4. How to run

On a fresh checkout, create a Python virtual environment, install dependencies, copy [.env.example](.env.example) to `.env`, and fill `OPENAI_API_KEY` and `DATABASE_URL`. Put the unchanged course CSV in `data/`; raw CSVs, `.env` and `runs/` are ignored by Git.

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
# Prepare and declare the scope only for a fresh run, never over an active run:
.\.venv\Scripts\python -m src.prepare --input data/spotify_reviews_18months.csv --run-id full_v1
.\.venv\Scripts\python -m src.scope --run-id full_v1
# Explicit execution permits paid calls. Reuse this command to resume:
.\.venv\Scripts\python -m src.pipeline --input data/spotify_reviews_18months.csv --run-id full_v1 --scope-file runs/full_v1/scope_ids.txt --workers 300 --verify-sample 2000 --max-spend-usd 5.00 --execute
```

Omitting `--execute` uses model-stage dry-runs. After the run is idle and scoped work has finished, the explicit code-only accounting and ranking commands are:

```powershell
.\.venv\Scripts\python -m src.scope --run-id full_v1 --quarantine-out-of-scope
.\.venv\Scripts\python -m src.rank --run-id full_v1
# Offline replay of measured checkpoints; makes no API/model calls:
.\.venv\Scripts\python -m cost.calculator --runs ckpt_10k --workers 300
# After final outputs exist, create the schema and load saved outputs:
.\.venv\Scripts\python -m src.load_db --schema-only
.\.venv\Scripts\python -m src.load_db --run-id full_v1
```

The calculator currently supports the course development checkpoints, not direct `full_v1` replay. Deploy using [docs/dashboard.md](docs/dashboard.md): Vercel Framework Preset Other, repository root, checked-in `vercel.json`, and `DATABASE_URL` in Vercel environment settings. [Pipeline details](docs/pipeline.md).

## 5. Results

### Final scoped-run ranking

Copied from `runs/full_v1/ranking.csv`; committed [ranking.csv](grading/ranking.csv) is identical. Membership includes completed complaints and cancellations only. Priority score is severity sum, not revenue impact or observed churn.

| Rank | Issue | Complaint/cancellation count | Severity sum | Mean severity | Priority score |
|---|---|---:|---:|---:|---:|
| 1 | ISS-USABILITY | 12226 | 32097 | 2.625307 | 32097 |
| 2 | ISS-OTHER | 12378 | 24653 | 1.991679 | 24653 |
| 3 | ISS-BILLING | 7555 | 20963 | 2.774719 | 20963 |
| 4 | ISS-PLAYBACK | 6103 | 20063 | 3.287400 | 20063 |
| 5 | ISS-CATALOG | 2663 | 7410 | 2.782576 | 7410 |
| 6 | ISS-ACCESS | 1508 | 5761 | 3.820292 | 5761 |
| 7 | ISS-DOWNLOADS | 954 | 2897 | 3.036688 | 2897 |
| 8 | ISS-SUPPORT | 96 | 264 | 2.750000 | 264 |

### Final independent verification

Copied from `runs/full_v1/verify/disagreement_report.md`:

```text
# Independent verifier disagreements

Seed: 20261020; requested: 2000; selected: 2000;
compared: 2000; missing predictions: 0.
Agreement is measured only over saved predictions; missing cases are explicitly excluded.

| Field | Agreement |
|---|---|
| topic | 0.909500 |
| intent | 0.961000 |
| severity | 0.925000 |
| sentiment | 0.824500 |
| needs_review | 0.907000 |

Severity mean absolute error: 0.079500
```

### Golden-set evaluation

All **50** student-labeled examples have saved predictions. Topic accuracy **24.00%**, macro F1 **0.176557**; intent accuracy **80.00%**, macro F1 **0.736551**; exact severity accuracy **62.00%**, severity MAE **0.600000**; joint topic/intent/severity accuracy **16.00%**. Topic agreement is poor and warrants human review. These are student-label diagnostics, not an instructor-approved benchmark or a grade. Missing predictions remain in accuracy/F1 denominators; MAE uses valid predictions only.

**Human-labeling error analysis:** the human used `access` for 14 golden reviews; the model used `other` for 13 of them. The principal recurring error is human assignment of general praise to `access`, contrary to the contract; 11 reviewed cases follow this pattern. Excluding all human-`access`/model-`other` pairs gives **12/37 = 32.43%**, a post-hoc diagnostic only, not a replacement for official topic agreement (**12/50 = 24.00%**). The excluded group also contains generic criticism and a specific-ad-praise case where both labels are wrong. The golden set measures human rule application as much as model accuracy; a blind second-human relabel under the contract is recommended. [Detailed error analysis](evals/golden_eval.md).

[Metrics and per-case predictions](evals/golden_metrics.json) · [Confusion counts](evals/golden_confusion.csv) · [Human labels](evals/golden_50_human_labels.csv)

[Offline injection test](evals/injection_test.json) passed prompt/data separation and simulated-output validation checks, with no model calls; it does **not** establish live-model injection resistance. The [synthetic planted-error test](evals/planted_error_test.json) flagged a deliberately wrong topic against a saved independent verifier prediction without changing business tables.

Measured cost reports: [100-review cold/warm](cost/report.md), [500-review checkpoint](cost/ckpt_500_report.md), [10k checkpoint](cost/ckpt_10k_report.md). Historical full-corpus projections are estimates, not measured final-run totals.

## 6. Cost and time

USD values below are known API subtotals computed from saved usage, not a guarantee of the complete bill. Calls without usage remain **unknown**, not zero. Prices and provenance are in [cost/rates.csv](cost/rates.csv); local compute and hosting costs are not measured.

| Run / saved measurement | Known API USD | Calls with unknown usage | Wall-clock seconds |
|---|---:|---:|---:|
| 100-review cold | 0.00541343 | 1 | 194.620243 |
| 100-review warm, same run/cache | 0 | 0 | 0.126043 |
| 500-review checkpoint, original invocation | 0.01654767 | 0 | 207.143858 |
| 10k, interrupted attempt | 0.07066771 | 1 | 118.32245 |
| 10k, resumed attempt (report calls it warm) | 0.14777290 | 0 | 300.193342 |
| 10k lifetime known subtotal | 0.21844061 | 1 | See separate attempts above |
| full_v1, lifetime known cost / completed invocation time | 1.97843941 | 76 | 1265.823879 |

The warm 100-review replay made no new enrichment calls. The 10k resumed attempt is **not** a warm-cache benchmark: it continued incomplete work. The original 500-review memo failed its checks. The final run is marked **completed**. Its **1265.823879** seconds cover the completed invocation; the prior saved interrupted invocation took **2059.334633** seconds. Complete elapsed time across all launches is unavailable because the initial forced interruption did not save a full invocation summary. Lifetime cost includes all saved model stages, plus **76** calls with unknown charges. Spend/reservations are **USD 2.55352471**, which is not a measured bill. Unknown usage remains unknown.

## 7. Resume demo

Saved snapshots report `checkpoint_before.json`: **99,923** completed at `2026-10-21T06:36:45.570239+00:00`, and `checkpoint_after.json`: **99,923** completed at `2026-10-21T06:43:10.675034+00:00`. The latest resume stage logged no new enrichment calls and no additional completions.

The first interruption was a **forced double Ctrl+C**, followed by resume from saved state (operator-provided history). **No screen recording is available.** The current snapshots have equal completion counts, so they do not prove an increase in completed records across this particular pair. They must not be presented as a complete interruption-demo submission. The code supports graceful first-Ctrl+C draining and forced exit on a second Ctrl+C, but that capability is distinct from recorded evidence.

## 8. Audit status and known gaps

Part B is complete as an offline evaluation/export, not as a claim that every course requirement passes. The unmodified course checker returned **review_required**. [Full self-check report](evals/course_self_check.json).

- Coverage: **660,622** unique source IDs, **0** missing, **0** duplicates, **99,923** valid completed records, **560,699** valid quarantines, **0** invalid/unresolved exports, and **21,901** valid exact-text cache reuses. No source-hash, schema, membership, ranking, claims, or missing-role errors were reported.
- `unfinished_classification`: the checked-in full-corpus contract still expects every nonempty review; **560,686** nonempty IDs are not completed. The declared minimum scope does not make this checker requirement disappear.
- `invalid_usage`: **152** missing input/output usage-field flags across **76** calls. Unknown charges remain unknown rather than being fabricated as zero.
- `resume_snapshot_mismatch` and `resume_call_evidence`: before/after snapshots have equal completion counts. `reprocessed_checkpoint` flags **2,374** resume call entries referencing IDs present in the saved before snapshot. This snapshot contains the final completion set, so it is inconsistent with the earlier call timeline and cannot demonstrate the claimed interruption boundary. Original snapshots/logs were preserved, not rewritten to pass.
- No screen recording is available. A new live demonstration was not attempted because this task permits no paid calls.
- The injection test is an offline structural/simulated-response test; live-model robustness remains unmeasured.
- Golden topic accuracy is low; human/model disagreements need review. Human labels were not silently revised, and no instructor-approved benchmark was claimed.
- The memo and last run summary predate out-of-scope quarantine: memo totals of **90** quarantined and **560,609** unresolved describe the earlier snapshot, not current accounting. Current exported status counts are correct. The memo and original summary were not rewritten as if generated later.
- Complete elapsed time across all launches is unavailable; live dashboard availability and loaded-data freshness were not checked.

### Reproduce Part B without model calls

After the run is idle:

```powershell
.\.venv\Scripts\python -m src.audit --run-id full_v1
.\.venv\Scripts\python data/check_submission.py profile --full data/spotify_reviews_18months.csv --out grading/ingestion.json
.\.venv\Scripts\python data/check_submission.py reference --full data/spotify_reviews_18months.csv --analysis data/spotify_reviews_18months.csv --out runs/audit_full_v1/reference.json
.\.venv\Scripts\python data/check_submission.py check --reference runs/audit_full_v1/reference.json --submission grading --out evals/course_self_check.json
.\.venv\Scripts\python -m unittest discover -s tests -q
```

The reference remains outside the grading folder and is ignored by Git. Golden evaluation is separate from the course checker because student labels are not an instructor-approved benchmark. `src.audit` exports final durable SQLite call events, including failed/unknown-usage attempts, without changing the original run or emitting new calls.

## 9. Limitations and data source

Reviews are self-selected, historical observations, not a representative customer survey. Expressed cancellation intent is not observed churn. There is no revenue or subscription-plan data, and classification and quoted evidence may be wrong. Exact-text reuse can propagate an incorrect label. Quarantined records and excluded scope reduce coverage. Verifier agreement measures consistency, not accuracy. Priority scores aggregate expressed severity; they do not establish causality, feasibility or expected business value. Numeric/citation checks do not establish that a model-generated recommendation is sound.

Source: BwandoWando, [3.4 Million Spotify Google Store Reviews](https://www.kaggle.com/datasets/bwandowando/3-4-million-spotify-google-store-reviews), fixed course extract described in [data/manifest.json](data/manifest.json). Saved review timestamps span **2022-05-17 00:01:07** through **2023-11-15 23:16:10**. Preserve original source strings and review IDs; see [contract notes](docs/contract_notes.md).

`data/spotify_reviews_18months.csv` SHA-256:

```text
1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6
```

Raw input and the local reference remain untracked. The compact [grading export](grading/) includes every original ID and saved execution evidence, with large JSONL files gzip-compressed. Human labels and grading artifacts are excluded from Vercel deployment. The dashboard exposes saved outputs, not raw CSV download access.
