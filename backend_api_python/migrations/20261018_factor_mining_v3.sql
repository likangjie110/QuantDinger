CREATE TABLE IF NOT EXISTS qd_factor_proposals (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES qd_factor_mining_runs(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    expression_json TEXT NOT NULL DEFAULT '{}',
    expression_hash VARCHAR(64) NOT NULL DEFAULT '',
    hypothesis TEXT NOT NULL DEFAULT '',
    model VARCHAR(100) NOT NULL DEFAULT '',
    provider VARCHAR(50) NOT NULL DEFAULT '',
    prompt_hash VARCHAR(64) NOT NULL DEFAULT '',
    dsl_version INTEGER NOT NULL DEFAULT 1,
    source VARCHAR(10) NOT NULL DEFAULT 'llm',
    status VARCHAR(20) NOT NULL DEFAULT 'proposed',
    reject_reason VARCHAR(120) NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (run_id, round, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_factor_proposals_user_run
  ON qd_factor_proposals(user_id, run_id, round, ordinal);
CREATE INDEX IF NOT EXISTS idx_factor_proposals_user_hash
  ON qd_factor_proposals(user_id, expression_hash);

ALTER TABLE qd_factor_mining_runs
  ADD COLUMN IF NOT EXISTS job_id VARCHAR(40) NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_factor_mining_runs_user_job
  ON qd_factor_mining_runs(user_id, job_id);
