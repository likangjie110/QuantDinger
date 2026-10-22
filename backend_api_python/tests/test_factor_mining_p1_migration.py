"""P1 schema upgrades preserve candidates and introduce durable execution ownership."""

from pathlib import Path


def test_p1_migration_is_idempotent_and_preserves_candidate_content():
    root = Path(__file__).resolve().parents[1] / "migrations"
    migration = (root / "20261019_factor_mining_p1.sql").read_text(encoding="utf-8")
    assert migration.strip() in (root / "init.sql").read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS qd_factor_candidates_user_id_expression_hash_key" in migration
    assert "ON qd_factor_candidates(user_id, market, timeframe, expression_hash)" in migration
    for field in ("execution_token", "lease_expires_at", "heartbeat_at", "attempts"):
        assert f"ADD COLUMN IF NOT EXISTS {field}" in migration
    assert "DELETE FROM" not in migration.upper()
    assert "UPDATE qd_factor_candidates" not in migration
