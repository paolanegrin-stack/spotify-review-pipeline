# Golden evaluation: human-labeling error analysis

Sources: [unchanged human labels](golden_50_human_labels.csv), saved final-run model labels in [golden_metrics.json](golden_metrics.json), and the [grading contract](../data/GRADING_CONTRACT.md). No labels, model predictions or official metrics were changed, and no model calls were made. Row references below use the CSV's `n` column.

## Principal recurring cause

A principal recurring cause of low topic agreement is **human rule-application error**, not model error: the human labeler used `access` for general praise, although the contract defines `access` as “Login, signup, password or account access” and explicitly assigns general praise to `other`. The model followed that general-praise rule in **11** reviewed cases: rows 3, 5, 8, 14, 15, 23, 26, 35, 36, 39, 40. For example, both “Good” reviews were human-labeled `access` and model-labeled `other`.

“Great app use it every chance I get” was actually human-labeled `usability`, not `access`; its model label `other` reflects the same general-praise rule. This is an additional human labeling error, rather than evidence of an access problem. This recurring category does not establish that every other model prediction is correct or explain every disagreement.

## Exact counts and diagnostic

| Measure | Computed result |
|---|---:|
| Golden reviews | 50 |
| Human topic `access` | 14 |
| Of those, model topic `other` | 13 |
| Official topic agreement, unchanged | 12/50 = 24.00% |
| Diagnostic only: exclude human `access` / model `other` pairs | 12/37 = 32.43% |

The diagnostic exclusion is defined solely as `human.topic == "access" and model.topic == "other"`. It removes **13** mismatches and leaves the same **12** matches. It is a post-hoc subset diagnostic, **not a replacement for official topic agreement or an unbiased estimate of model accuracy**.

Importantly, the excluded pairs are not all generic praise: row 13 is generic criticism (“This app is bad work”), for which `other` is also appropriate; row 37 praises reduced advertising, a specific `usability` feature. For row 37, human `access` and model `other` are both inconsistent with the topic rule. The remaining human `access` label, row 30, concerns Premium restrictions and was model-labeled `billing`. The exclusion must not be described as a blanket correction of every model disagreement.

## Other disagreement causes

These are contract-based qualitative judgments, not replacement adjudicated labels; ambiguous cases require a second reviewer.

- **Human error:** general praise also assigned `usability` (rows 2, 7, 49, 50), and generic dislike assigned a specific defect or inflated severity (rows 6, 13, 20, 42); general praise/criticism belongs to `other`, with severity based on supported harm.
- **Human error:** ad interruptions labeled `playback` or `billing` (rows 4, 16, 33), recommendation praise labeled `playback` (row 22), and offline-listening failure labeled `usability` (row 45); the contract assigns these to `usability`, `catalog`, and `downloads`, respectively.
- **Human error:** requests to fix reported failures labeled pure `request` with severity 1 (rows 11, 32, 38); complaint precedence and supported functional loss apply even when the review asks for a fix.
- **Human and model error:** row 29 describes explicitly Premium-only controls; human `downloads` and model `usability` miss the `billing` rule, while human severity 5 also lacks explicit serious financial, privacy or data harm.
- **Human and model error:** row 37 praises reduced ads, so both human `access` and model `other` miss the specific `usability` topic; row 48 is vague update criticism, so neither human `playback` nor model `usability` has a clearly supported specific defect.
- **Ambiguous:** the resolved support update versus the older crash report (row 12) creates a temporal emphasis disagreement; the contract does not explicitly settle how to weight an update against its historical text.
- **Ambiguous:** “very diverse” praise (row 9), restricted song selection/rewind versus playback failure (row 43), and price versus ad-friction precedence (row 47) need closer interpretation rather than automatic acceptance of either label.
- **Ambiguous:** slang/non-English reviews (rows 19, 24) require competent language review before attributing the disagreement to the human or model; unsupported-language uncertainty should be reflected in `needs_review`.
- **Ambiguous:** a politicized catalog-removal allegation (row 1), mixed unrelated criticism (row 31), and fragmented pricing language (row 34) complicate supported topic/intent/severity; product complaints should be separated from unrelated slogans without treating allegations as established facts.

## Conclusion

The golden set measures the human labeler's application of the rules as much as the model's. The repeated general-praise error is a human labeling error; model errors and ambiguous cases remain. **Official topic agreement stays 12/50 (24.00%)**, with the original labels and metrics preserved. The recommended next step is a **blind relabel by a second human following the contract strictly**, followed by documented adjudication of disagreements, rather than relabeling to match the model or substituting the exclusion diagnostic for the official metric.
