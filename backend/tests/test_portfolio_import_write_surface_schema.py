# backend/tests/test_portfolio_import_write_surface_schema.py
"""Static and contract tests for Phase 13Q.3 migration 016.

Verifies:
1. Migration 016 exists and is syntactically valid.
2. Direct INSERT policy on portfolio_import_claim_bindings is dropped.
3. Direct table INSERT privilege on portfolio_import_claim_bindings is revoked from authenticated.
4. RPC commit_portfolio_import_claim EXECUTE privilege is revoked from authenticated and PUBLIC.
5. RPC commit_portfolio_import_claim EXECUTE privilege is granted exclusively to service_role.
6. Authenticated SELECT policy and immutability triggers remain preserved across migrations.
7. Backend architecture relies on SUPABASE_SERVICE_ROLE_KEY for trusted operations.
"""

from pathlib import Path
import re
import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "supabase" / "migrations"
MIGRATION_014 = MIGRATIONS_DIR / "014_portfolio_import_claim_bindings.sql"
MIGRATION_015 = MIGRATIONS_DIR / "015_portfolio_import_atomic_commit.sql"
MIGRATION_016 = MIGRATIONS_DIR / "016_portfolio_import_write_surface_hardening.sql"


@pytest.fixture(scope="module")
def sql_016_raw() -> str:
    assert MIGRATION_016.exists(), f"Migration file {MIGRATION_016} not found"
    return MIGRATION_016.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sql_016_without_comments(sql_016_raw: str) -> str:
    cleaned = re.sub(r"--.*$", "", sql_016_raw, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)


class TestPortfolioImportWriteSurfaceSchema:
    """Security verification tests for Phase 13Q.3 import write-surface hardening."""

    def test_migration_016_file_exists(self):
        """Section 26 Matrix A: Migration 016 exists."""
        assert MIGRATION_016.exists()

    def test_authenticated_claim_insert_policy_dropped(self, sql_016_without_comments: str):
        """Section 26 Matrix B: Authenticated claim INSERT policy is dropped."""
        pattern = r'DROP\s+POLICY\s+IF\s+EXISTS\s+"Users can insert own import claim bindings"\s+ON\s+public\.portfolio_import_claim_bindings'
        assert re.search(pattern, sql_016_without_comments, re.IGNORECASE)

    def test_authenticated_claim_insert_privilege_revoked(self, sql_016_without_comments: str):
        """Section 26 Matrix D: Direct table INSERT privilege revoked from authenticated."""
        pattern = r"REVOKE\s+INSERT\s+ON\s+TABLE\s+public\.portfolio_import_claim_bindings\s+FROM\s+authenticated"
        assert re.search(pattern, sql_016_without_comments, re.IGNORECASE)

    def test_rpc_execute_revoked_from_authenticated(self, sql_016_without_comments: str):
        """Section 27 Matrix H: Revoke RPC EXECUTE from authenticated."""
        pattern = r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim\s*\(\s*JSONB\s*,\s*JSONB\s*\)\s+FROM\s+authenticated"
        assert re.search(pattern, sql_016_without_comments, re.IGNORECASE)

    def test_rpc_execute_revoked_from_public(self, sql_016_without_comments: str):
        """Section 27 Matrix I: Revoke RPC EXECUTE from PUBLIC."""
        pattern = r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim\s*\(\s*JSONB\s*,\s*JSONB\s*\)\s+FROM\s+PUBLIC"
        assert re.search(pattern, sql_016_without_comments, re.IGNORECASE)

    def test_rpc_execute_granted_to_service_role(self, sql_016_without_comments: str):
        """Section 27 Matrix J: Grant RPC EXECUTE strictly to service_role."""
        pattern = r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim\s*\(\s*JSONB\s*,\s*JSONB\s*\)\s+TO\s+service_role"
        assert re.search(pattern, sql_016_without_comments, re.IGNORECASE)

    def test_no_grants_to_anon_or_authenticated_in_016(self, sql_016_without_comments: str):
        """Section 27 Matrix K & L: No grants to anon or authenticated in 016."""
        assert not re.search(r"GRANT\s+.*?\s+TO\s+anon\b", sql_016_without_comments, re.IGNORECASE)
        assert not re.search(r"GRANT\s+.*?\s+TO\s+authenticated\b", sql_016_without_comments, re.IGNORECASE)

    def test_no_claim_update_or_delete_policies_in_016(self, sql_016_without_comments: str):
        """Section 26 Matrix E & F: No new UPDATE or DELETE policies on claim bindings."""
        assert not re.search(r"CREATE\s+POLICY\s+.*?\s+FOR\s+UPDATE", sql_016_without_comments, re.IGNORECASE)
        assert not re.search(r"CREATE\s+POLICY\s+.*?\s+FOR\s+DELETE", sql_016_without_comments, re.IGNORECASE)

    def test_authenticated_select_policy_preserved_in_014(self):
        """Section 26 Matrix C: Authenticated SELECT policy remains in migration 014."""
        sql_014 = MIGRATION_014.read_text(encoding="utf-8")
        assert 'CREATE POLICY "Users can view own import claim bindings"' in sql_014
        assert re.search(r"FOR\s+SELECT\s+TO\s+authenticated", sql_014, re.IGNORECASE)
        assert "owner_id" in sql_014 and "auth.uid()" in sql_014

    def test_claim_immutability_trigger_preserved_in_014(self):
        """Section 26 Matrix G: Immutability trigger remains in migration 014."""
        sql_014 = MIGRATION_014.read_text(encoding="utf-8")
        assert "trg_prevent_import_claim_binding_tamper" in sql_014
        assert "prevent_import_claim_binding_tamper" in sql_014

    def test_backend_uses_service_role_key(self):
        """Section 28: Prove architecture expects a service-role backend client."""
        deps_path = Path(__file__).resolve().parent.parent / "api" / "dependencies.py"
        assert deps_path.exists()
        deps_src = deps_path.read_text(encoding="utf-8")
        assert "SUPABASE_SERVICE_ROLE_KEY" in deps_src
