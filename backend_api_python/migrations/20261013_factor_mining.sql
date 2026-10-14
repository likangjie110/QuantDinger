-- Factor mining (spec section 11): one row per run, one per distinct expression, one per
-- evaluation.  Applied from init.sql on every backend start, so every statement is idempotent.

CREATE TABLE IF NOT EXISTS qd_factor_mining_runs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL,
    source_name VARCHAR(255) DEFAULT '',
    market VARCHAR(100) DEFAULT '',
    timeframe VARCHAR(10) DEFAULT '',
    start_date VARCHAR(40) NOT NULL DEFAULT '',
    end_date VARCHAR(40) NOT NULL DEFAULT '',
    snapshot_id VARCHAR(64) NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    trials_count INTEGER NOT NULL DEFAULT 0,
    candidates_count INTEGER NOT NULL DEFAULT 0,
    frozen_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_factor_mining_runs_user_id
  ON qd_factor_mining_runs(user_id, id DESC);

-- run_id is ON DELETE SET NULL rather than CASCADE: a candidate is deduplicated across runs and
-- outlives the one that judged it, so losing that run clears the pointer instead of the library.
CREATE TABLE IF NOT EXISTS qd_factor_candidates (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES qd_factor_mining_runs(id) ON DELETE SET NULL,
    expression_json TEXT NOT NULL DEFAULT '{}',
    expression_hash VARCHAR(64) NOT NULL DEFAULT '',
    origin VARCHAR(32) NOT NULL DEFAULT '',
    template_id VARCHAR(64) NOT NULL DEFAULT '',
    market VARCHAR(100) DEFAULT '',
    timeframe VARCHAR(10) DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'search',
    reject_reason VARCHAR(120) NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    out_of_sample_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (user_id, expression_hash)
);

CREATE INDEX IF NOT EXISTS idx_factor_candidates_user_id
  ON qd_factor_candidates(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_factor_candidates_user_hash
  ON qd_factor_candidates(user_id, expression_hash);
CREATE INDEX IF NOT EXISTS idx_factor_candidates_user_status
  ON qd_factor_candidates(user_id, status, id DESC);

-- One row per evaluation, rejected candidates included: their count is the deflated-Sharpe
-- denominator the frozen review prices the search with.
CREATE TABLE IF NOT EXISTS qd_factor_trials (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES qd_factor_mining_runs(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    expression_hash VARCHAR(64) NOT NULL DEFAULT '',
    stage VARCHAR(8) NOT NULL DEFAULT '',
    verdict VARCHAR(10) NOT NULL DEFAULT '',
    reject_reason VARCHAR(120) NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_factor_trials_run_stage
  ON qd_factor_trials(run_id, stage);
