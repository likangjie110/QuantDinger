-- Preserve candidate ids/content while matching the library's market/timeframe scope.
ALTER TABLE qd_factor_candidates
    DROP CONSTRAINT IF EXISTS qd_factor_candidates_user_id_expression_hash_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_candidates_context_hash
    ON qd_factor_candidates(user_id, market, timeframe, expression_hash);

-- Durable execution ownership is only used by finite factor-mining jobs.
ALTER TABLE qd_agent_jobs
    ADD COLUMN IF NOT EXISTS execution_token VARCHAR(64) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_agent_jobs_mining_recovery
    ON qd_agent_jobs(status, lease_expires_at, created_at)
    WHERE kind = 'factor_mining' AND status IN ('queued', 'running');

ALTER TABLE qd_factor_mining_runs
    ADD COLUMN IF NOT EXISTS execution_token VARCHAR(64) NOT NULL DEFAULT '';
