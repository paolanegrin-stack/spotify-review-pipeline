CREATE TABLE IF NOT EXISTS runs (
    run_id text PRIMARY KEY, summary jsonb NOT NULL,
    declared_scope_count integer, loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS records (
    review_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs ON DELETE CASCADE,
    source_sha256 text NOT NULL, status text NOT NULL, reason text,
    topic text, intent text, severity integer, sentiment numeric,
    needs_review boolean, evidence_quote text, cache_source_id text,
    label_config text, review_rating text, review_timestamp text, app_version text
);
CREATE TABLE IF NOT EXISTS issues (
    run_id text NOT NULL REFERENCES runs ON DELETE CASCADE,
    issue_id text NOT NULL, name text, description text,
    representative_ids jsonb, member_count integer,
    PRIMARY KEY (run_id, issue_id)
);
CREATE TABLE IF NOT EXISTS membership (
    run_id text NOT NULL, issue_id text NOT NULL,
    review_id text NOT NULL REFERENCES records ON DELETE CASCADE,
    PRIMARY KEY (run_id, issue_id, review_id),
    FOREIGN KEY (run_id, issue_id) REFERENCES issues ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ranking (
    run_id text NOT NULL, issue_id text NOT NULL, rank integer NOT NULL,
    complaint_count integer NOT NULL, severity_sum integer NOT NULL,
    mean_severity numeric(12,6) NOT NULL, priority_score integer NOT NULL,
    PRIMARY KEY (run_id, issue_id),
    FOREIGN KEY (run_id, issue_id) REFERENCES issues ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS claims (
    run_id text NOT NULL, claim_id text NOT NULL, issue_id text NOT NULL,
    metric text NOT NULL, value text NOT NULL,
    PRIMARY KEY (run_id, claim_id),
    FOREIGN KEY (run_id, issue_id) REFERENCES issues ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS memo (
    run_id text PRIMARY KEY REFERENCES runs ON DELETE CASCADE,
    markdown text NOT NULL, check_status jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS verify_summary (
    run_id text PRIMARY KEY REFERENCES runs ON DELETE CASCADE,
    summary jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS records_status_idx ON records(status);
CREATE INDEX IF NOT EXISTS records_topic_idx ON records(topic);
CREATE INDEX IF NOT EXISTS records_intent_idx ON records(intent);
CREATE INDEX IF NOT EXISTS records_run_idx ON records(run_id);
CREATE INDEX IF NOT EXISTS membership_issue_idx ON membership(issue_id);
