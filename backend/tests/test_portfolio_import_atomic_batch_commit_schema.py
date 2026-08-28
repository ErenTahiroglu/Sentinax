# backend/tests/test_portfolio_import_atomic_batch_commit_schema.py
"""Static and contract tests for Phase 13R atomic batch commit migration 017.

Verifies:
1. Migration 017 exists and defines public.commit_portfolio_import_claim_batch.
2. Input validation: non-null JSON array, non-empty array, exact item keys ["binding", "transaction"].
3. Delegation to closed public.commit_portfolio_import_claim (no duplicate INSERTs in 017).
4. Outer atomicity: single loop in subtransaction, conflict raises dedicated SQLSTATE P13R1.
5. Conflict handling: catches strictly P13R1, returns empty success arrays, conflict ordinal/tx_id.
6. Generic DB errors are not swallowed.
7. Security: SECURITY INVOKER, VOLATILE, search_path = public, pg_temp, service_role execution only.
"""

from pathlib import Path
import re
import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "supabase" / "migrations"
MIGRATION_017 = MIGRATIONS_DIR / "017_portfolio_import_atomic_batch_commit.sql"


@pytest.fixture(scope="module")
def sql_017_raw() -> str:
    assert MIGRATION_017.exists(), f"Migration file {MIGRATION_017} not found"
    return MIGRATION_017.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sql_017_without_comments(sql_017_raw: str) -> str:
    cleaned = re.sub(r"--.*$", "", sql_017_raw, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)


class TestPortfolioImportAtomicBatchCommitSchema:
    """Security and structural verification tests for migration 017."""

    def test_migration_017_file_exists(self):
        """Section 56 Matrix A: Migration 017 exists."""
        assert MIGRATION_017.exists()

    def test_batch_rpc_signature(self, sql_017_without_comments: str):
        """Section 56 Matrix B: Function name and signature."""
        pattern = r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.commit_portfolio_import_claim_batch\s*\(\s*p_items\s+JSONB\s*\)"
        assert re.search(pattern, sql_017_without_comments, re.IGNORECASE)

    def test_input_json_array_validation(self, sql_017_without_comments: str):
        """Section 56 Matrix C & D: JSON array required, empty array rejected."""
        assert "p_items IS NULL OR jsonb_typeof(p_items) <> 'array'" in sql_017_without_comments
        assert "jsonb_array_length(p_items) = 0" in sql_017_without_comments
        assert "p_items array cannot be empty" in sql_017_without_comments

    def test_exact_item_keys_validation(self, sql_017_without_comments: str):
        """Section 56 Matrix E & F: Item keys must be exactly ['binding', 'transaction']."""
        assert "ARRAY['binding', 'transaction']" in sql_017_without_comments
        assert "Item payload keys must be exactly" in sql_017_without_comments
        assert "jsonb_typeof(v_tx_json) <> 'object'" in sql_017_without_comments
        assert "jsonb_typeof(v_b_json) <> 'object'" in sql_017_without_comments

    def test_delegates_to_closed_single_intent_rpc(self, sql_017_without_comments: str):
        """Section 56 Matrix H & 58: Delegates to commit_portfolio_import_claim with zero duplicate INSERTs."""
        assert "public.commit_portfolio_import_claim(" in sql_017_without_comments
        # No direct table writes in migration 017
        assert "INSERT INTO public.portfolio_transactions" not in sql_017_without_comments
        assert "INSERT INTO public.portfolio_import_claim_bindings" not in sql_017_without_comments

    def test_outer_subtransaction_and_dedicated_sqlstate(self, sql_017_without_comments: str):
        """Section 57 Matrix J, K, L, M, N: Outer exception block catches dedicated SQLSTATE P13R1."""
        assert "ERRCODE = 'P13R1'" in sql_017_without_comments
        assert "EXCEPTION WHEN SQLSTATE 'P13R1' THEN" in sql_017_without_comments

    def test_status_aggregation_rules(self, sql_017_without_comments: str):
        """Section 59 Matrix O, P, Q, R: Aggregation logic for appended, idempotent_duplicate, conflict."""
        assert "'appended'" in sql_017_without_comments
        assert "'idempotent_duplicate'" in sql_017_without_comments
        assert "'conflict'" in sql_017_without_comments
        assert "v_has_appended" in sql_017_without_comments

    def test_conflict_return_contract(self, sql_017_without_comments: str):
        """Section 60 Matrix S-W: Conflict returns empty arrays and conflict details."""
        assert "ARRAY[]::UUID[]" in sql_017_without_comments
        assert "ARRAY[]::TEXT[]" in sql_017_without_comments
        assert "v_conflict_ordinal" in sql_017_without_comments
        assert "v_conflict_tx_id" in sql_017_without_comments
        assert "v_conflict_diagnostic" in sql_017_without_comments

    def test_sql_security_and_permissions(self, sql_017_without_comments: str):
        """Section 61 Matrix X-AC: Security invoker, volatile, explicit search_path, service_role only."""
        assert "SECURITY INVOKER" in sql_017_without_comments
        assert "VOLATILE" in sql_017_without_comments
        assert re.search(r"SET\s+search_path\s*=\s*public\s*,\s*pg_temp", sql_017_without_comments, re.IGNORECASE)

        assert re.search(r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim_batch\s*\(\s*JSONB\s*\)\s+FROM\s+PUBLIC", sql_017_without_comments, re.IGNORECASE)
        assert re.search(r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim_batch\s*\(\s*JSONB\s*\)\s+FROM\s+authenticated", sql_017_without_comments, re.IGNORECASE)
        assert re.search(r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim_batch\s*\(\s*JSONB\s*\)\s+TO\s+service_role", sql_017_without_comments, re.IGNORECASE)
