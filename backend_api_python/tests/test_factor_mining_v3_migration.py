from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "20261018_factor_mining_v3.sql"
INIT = Path(__file__).resolve().parents[1] / "migrations" / "init.sql"

def test_the_startup_schema_contains_the_proposals_table_and_indexes():
    init = INIT.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS qd_factor_proposals" in init
    assert "UNIQUE (run_id, round, ordinal)" in init
    assert "idx_factor_proposals_user_run" in init
    assert "idx_factor_proposals_user_hash" in init
    assert "ADD COLUMN IF NOT EXISTS job_id" in init
    assert "idx_factor_mining_runs_user_job" in init

def test_the_v3_migration_is_appended_verbatim_to_init_sql():
    sql = MIGRATION.read_text(encoding="utf-8").strip()
    assert sql in INIT.read_text(encoding="utf-8")
