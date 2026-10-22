-- Cumulative research identity and idempotent per-run trial contributions.
CREATE TABLE IF NOT EXISTS qd_factor_mining_experiments (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    context_hash VARCHAR(64) NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    cumulative_trials BIGINT NOT NULL DEFAULT 0 CHECK (cumulative_trials >= 0),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, context_hash),
    UNIQUE (id, user_id)
);

CREATE TABLE IF NOT EXISTS qd_factor_mining_experiment_runs (
    run_id INTEGER PRIMARY KEY REFERENCES qd_factor_mining_runs(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    experiment_id INTEGER NOT NULL,
    trials_count INTEGER NOT NULL CHECK (trials_count >= 0),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    FOREIGN KEY (experiment_id, user_id)
        REFERENCES qd_factor_mining_experiments(id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_factor_mining_experiment_runs_group
    ON qd_factor_mining_experiment_runs(user_id, experiment_id, run_id);
