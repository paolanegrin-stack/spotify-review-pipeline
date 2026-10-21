# Declared final scope

Select the first 100,000 nonempty-text reviews by ascending SHA-256 of UTF-8 `berkeley-fall-2026-assignment-5-v1:review_id`, preserving exact review IDs. Ties use review ID. All 50 golden IDs are included; human labels are never read or sent to a model.

100,000 is the assignment minimum declared by the user for this run. The checked-in GRADING_CONTRACT.md instead says classify all 660,609 nonempty rows; this declared scope is a change from that file. Excluded records receive no labels.

Only scoped IDs may be enriched or receive duplicate-text cache results. Downstream stages use completed scoped records. After completion, explicitly run `python -m src.scope --run-id full_v1 --quarantine-out-of-scope` to account for excluded pending IDs.
