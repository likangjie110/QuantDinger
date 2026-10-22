"""Fresh schema and ordered migrations must expose the same research constraints."""

from pathlib import Path


def test_research_migration_is_appended_verbatim_and_idempotent():
    root = Path(__file__).resolve().parents[1] / "migrations"
    migration = (root / "20261019_factor_mining_research.sql").read_text(encoding="utf-8")
    assert migration.strip() in (root / "init.sql").read_text(encoding="utf-8")
    assert "UNIQUE (user_id, context_hash)" in migration
    assert "run_id INTEGER PRIMARY KEY" in migration
    assert "FOREIGN KEY (experiment_id, user_id)" in migration
    assert migration.count("CREATE TABLE IF NOT EXISTS") == 2
