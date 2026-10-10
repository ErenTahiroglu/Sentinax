"""Post-28B-1A hardening H1: SQL-text and CI-membership contract for migration 031 (the real-PostgreSQL behaviour is in test_legacy_pit_truncate_hardening_postgres.py)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
M031 = next(m for m in MIGRATIONS if m.name.startswith("031_"))
STEP = "Post-28B-1A H1 legacy PIT TRUNCATE closure"


def code() -> str:
    return re.sub(r"--[^\n]*", "", M031.read_text(encoding="utf-8"))


def test_migration_031_directly_follows_030_with_no_gap_or_rewrite_of_history() -> None:
    names = [m.name[:3] for m in MIGRATIONS]
    assert names == sorted(names) and names[-2:] == ["030", "031"] and len(set(names)) == len(names)


def test_migration_031_is_exactly_two_table_scoped_truncate_revokes() -> None:
    statements = [re.sub(r"\s+", " ", s).strip() for s in code().split(";") if s.strip()]
    assert statements == [
        "REVOKE TRUNCATE ON TABLE public.raw_provider_snapshots FROM PUBLIC, anon, authenticated, service_role",
        "REVOKE TRUNCATE ON TABLE public.normalized_observations FROM PUBLIC, anon, authenticated, service_role",
    ]


def test_migration_031_grants_nothing_and_touches_no_default_privilege_owner_trigger_policy_or_constraint() -> None:
    upper = code().upper()
    for token in ("GRANT", "DEFAULT PRIVILEGES", "OWNER", "TRIGGER", "POLICY", "CONSTRAINT", "DROP", "ALTER", "CREATE", "DELETE", "UPDATE", "INSERT", "SECURITY"):
        assert token not in upper, token
    assert "SELECT" not in upper.replace("REVOKE", "")                                  # no SELECT/INSERT/UPDATE privilege is revoked


def test_h1_ci_step_is_permanent_runs_against_real_postgres_and_keeps_earlier_gates() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"- name: {STEP}" in ci
    block = ci.split(f"- name: {STEP}", 1)[1].split("\n    - name:", 1)[0]
    assert "SENTINAX_TEST_POSTGRES_URL" in block and "psycopg[binary]==3.3.6" in block and "--disable-socket" not in block
    for t in ("test_legacy_pit_truncate_hardening_schema", "test_legacy_pit_truncate_hardening_postgres"):
        assert f"backend/tests/{t}.py" in block, t
    for earlier in ("Static invariant guards", "Phase 24 private scheduler correctness", "Phase 26 backtest architecture correctness", "Phase 26 PostgreSQL history-coverage concurrency",
                    "Phase 27 PostgreSQL Supabase security advisor regression", "Phase 28B-0 learning evidence contracts", "Phase 28B-1A learning evidence PostgreSQL persistence"):
        assert f"- name: {earlier}" in ci, earlier
