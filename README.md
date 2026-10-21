# Spotify Reviews Multi-Agent Pipeline

## 1. Summary

**Business question:** Where should the next quarter of product effort go among access, usability, playback, and billing/support?

**10k checkpoint (final-run ranking pending).** Compare against **ISS-PLAYBACK**, with 626 complaints [C13] and priority score 2069 [C16], and **ISS-BILLING**, with 805 complaints [C9] and priority score 2256 [C12]. These are meaningful competing priorities, but their observed priority scores are below usability’s.

These claim IDs refer to `runs/ckpt_10k/claims.csv`, not final-run claims. Final grouping, ranking and recommendation are still pending at this documentation snapshot. [Checkpoint memo copy](docs/final_memo.md).

[Live dashboard](https://spotify-review-pipeline-blue.vercel.app/) · [Dashboard setup](docs/dashboard.md)

## 2. Scope and status

`runs/full_v1/ingestion_report.json` reports **660,622 original IDs**, **97,400,616 bytes**, and all ingestion/profile checks passed. The declared classification scope is a deterministic sample of **100,000 nonempty reviews**: order by SHA-256 of UTF-8 `seed + ':' + review_id`, ascending, using manifest seed `berkeley-fall-2026-assignment-5-v1`; select the first IDs. All **50 golden IDs** are included. [Scope method](docs/scope.md).

This is the assignment minimum declared by the operator; the checked-in [grading contract](data/GRADING_CONTRACT.md) instead requires classifying every nonempty review. **This is not full-corpus classification.**

Read-only database inspection at this snapshot shows:

| Account | Saved count / status |
|---|---:|
| Scoped classified/completed | 99,923 |
| Scoped quarantined after failures/retries | 77 |
| Empty-text quarantines outside scope | 13 |
| Remaining out-of-scope IDs | 560,609 pending |

The scoped quarantines retain their actual saved reasons: **50** `api_error:Request timed out.`, **26** `validation_failed:Quote must be a short exact source substring`, and **1** `validation_failed:Invalid JSON, refusal, or incomplete output`. The requested normalization to `enrich_failed_after_retries` changed no rows because no scoped pending records remained. There are no saved records with that normalized reason at this snapshot.

The intended final accounting is to quarantine the remaining excluded IDs as `not_classified_out_of_declared_scope`, without labels or model calls. **That command has not yet been reflected in the saved database**; they must not be reported as already quarantined.

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

### 10k checkpoint (final-run ranking pending)

These rows are copied from `runs/ckpt_10k/ranking.csv`. Membership includes completed complaints and cancellations; praise, requests and unclear intent are excluded. Priority score is the severity sum, not revenue impact or a churn estimate.

| Rank | Issue | Complaint/cancellation count | Severity sum | Mean severity | Priority score |
|---|---|---:|---:|---:|---:|
| 1 | ISS-USABILITY | 1246 | 3224 | 2.587480 | 3224 |
| 2 | ISS-OTHER | 1217 | 2423 | 1.990961 | 2423 |
| 3 | ISS-BILLING | 805 | 2256 | 2.802484 | 2256 |
| 4 | ISS-PLAYBACK | 626 | 2069 | 3.305112 | 2069 |
| 5 | ISS-CATALOG | 267 | 751 | 2.812734 | 751 |
| 6 | ISS-ACCESS | 156 | 596 | 3.820513 | 596 |
| 7 | ISS-DOWNLOADS | 92 | 280 | 3.043478 | 280 |
| 8 | ISS-SUPPORT | 7 | 21 | 3.000000 | 21 |

### Final-run verifier: partial saved results

The following is copied from `runs/full_v1/verify/disagreement_report.md`. It is an in-progress snapshot, not the final verification result:

```text
# Independent verifier disagreements

Seed: 20261020; requested: 2000; selected: 2000;
compared: 1600; missing predictions: 400.
Agreement is measured only over saved predictions; missing cases are explicitly excluded.

| Field | Agreement |
|---|---|
| topic | 0.902500 |
| intent | 0.960000 |
| severity | 0.924375 |
| sentiment | 0.826250 |
| needs_review | 0.903750 |

Severity mean absolute error: 0.080625
```

Measured cost reports: [100-review cold/warm](cost/report.md), [500-review checkpoint](cost/ckpt_500_report.md), [10k checkpoint](cost/ckpt_10k_report.md). Historical full-corpus projections in those reports are estimates, not measured final-run totals; some scenarios remain UNKNOWN.

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
| full_v1, latest saved lifetime cost / last saved interrupted invocation time | 1.93155401 | 76 | 2059.334633 |

The warm 100-review replay made no new enrichment calls. The 10k resumed attempt is **not** a warm-cache benchmark: it continued incomplete work. The original 500-review memo failed its checks. Final-run cost/time is provisional: its last saved summary is still marked `interrupted`, and verify/group/memo are running or pending; `2059.334633` seconds is not the complete end-to-end runtime. The subsequent saved enrichment stage took **93.501143** seconds with no new calls and no new completions. Unknown calls retain conservative spend reservations; reservations are not measured charges.

## 7. Resume demo

Saved snapshots report `checkpoint_before.json`: **99,923** completed at `2026-10-21T06:36:45.570239+00:00`, and `checkpoint_after.json`: **99,923** completed at `2026-10-21T06:43:10.675034+00:00`. The latest resume stage logged no new enrichment calls and no additional completions.

The first interruption was a **forced double Ctrl+C**, followed by resume from saved state (operator-provided history). **No screen recording is available.** The current snapshots have equal completion counts, so they do not prove an increase in completed records across this particular pair. They must not be presented as a complete interruption-demo submission. The code supports graceful first-Ctrl+C draining and forced exit on a second Ctrl+C, but that capability is distinct from recorded evidence.

## 8. Known gaps

- Final grouping, ranking, memo and complete cost/time summary are pending. Refresh the checkpoint fallback once the final artifacts exist.
- Out-of-scope pending IDs have not yet been quarantined; scoped failures retain their original detailed reasons rather than the requested normalized label.
- Human labels are copied to [evals/golden_50_human_labels.csv](evals/golden_50_human_labels.csv); golden-set evaluation metrics have not been computed.
- A saved injection-test evaluation is not present. Prompt protection is implemented, but it is not empirical injection-test evidence.
- Planted-error comparison code and unit tests exist; a saved final-run planted-error demonstration/report has not been produced.
- `grading/` exports are not produced yet, and `data/check_submission.py` has not been run against a final submission.
- Resume evidence lacks a screen recording and the saved snapshots do not show additional completions.
- Live dashboard availability and its loaded data freshness were not checked during this offline documentation update.

Part B evaluation, export and checker work has deliberately not been started here.

## 9. Limitations and data source

Reviews are self-selected, historical observations, not a representative customer survey. Expressed cancellation intent is not observed churn. There is no revenue or subscription-plan data, and classification and quoted evidence may be wrong. Exact-text reuse can propagate an incorrect label. Quarantined records and excluded scope reduce coverage. Verifier agreement measures consistency, not accuracy. Priority scores aggregate expressed severity; they do not establish causality, feasibility or expected business value. Numeric/citation checks do not establish that a model-generated recommendation is sound.

Source: BwandoWando, [3.4 Million Spotify Google Store Reviews](https://www.kaggle.com/datasets/bwandowando/3-4-million-spotify-google-store-reviews), fixed course extract described in [data/manifest.json](data/manifest.json). Saved review timestamps span **2022-05-17 00:01:07** through **2023-11-15 23:16:10**. Preserve original source strings and review IDs; see [contract notes](docs/contract_notes.md).

`data/spotify_reviews_18months.csv` SHA-256:

```text
1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6
```

Raw input and runtime artifacts remain local; they are not committed to this repository. The dashboard exposes saved outputs, not raw CSV download access.
