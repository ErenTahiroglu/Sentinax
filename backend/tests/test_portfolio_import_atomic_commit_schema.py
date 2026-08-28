"""
backend/tests/test_portfolio_import_atomic_commit_schema.py
===========================================================
Static schema & invariant verification for migration 015:
commit_portfolio_import_claim atomic commit RPC.
"""

from pathlib import Path
import re
import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "supabase"
    / "migrations"
    / "015_portfolio_import_atomic_commit.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert MIGRATION_PATH.exists(), f"Migration file missing at {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sql_without_comments(migration_sql: str) -> str:
    no_line_comments = re.sub(r"--.*", "", migration_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestPortfolioImportAtomicCommitSchema:
    """Static SQL inspection tests for migration 015."""

    def test_migration_file_exists(self, migration_sql: str):
        assert len(migration_sql) > 500

    def test_rpc_function_signature(self, sql_without_comments: str):
        """Matrix A: Function name, arguments, and return table structure."""
        fn_pattern = (
            r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+public\.commit_portfolio_import_claim\s*\(\s*"
            r"p_transaction\s+JSONB\s*,\s*"
            r"p_binding\s+JSONB\s*\)\s*"
            r"RETURNS\s+TABLE\s*\(\s*"
            r"commit_status\s+TEXT\s*,\s*"
            r"transaction_id\s+UUID\s*,\s*"
            r"diagnostic\s+TEXT\s*\)"
        )
        assert re.search(fn_pattern, sql_without_comments, re.IGNORECASE)

    def test_rpc_security_and_search_path(self, sql_without_comments: str):
        """Matrix B: Security invoker, volatile, and explicit search_path."""
        assert "SECURITY INVOKER" in sql_without_comments
        assert "VOLATILE" in sql_without_comments
        assert re.search(r"SET\s+search_path\s*=\s*public\s*,\s*pg_temp", sql_without_comments, re.IGNORECASE)

    def test_rpc_permissions_grant_and_revoke(self, sql_without_comments: str):
        """Matrix C: Revoke from PUBLIC, grant to authenticated and service_role."""
        assert re.search(r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim\s*\(\s*JSONB\s*,\s*JSONB\s*\)\s+FROM\s+PUBLIC", sql_without_comments, re.IGNORECASE)
        assert re.search(r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+public\.commit_portfolio_import_claim\s*\(\s*JSONB\s*,\s*JSONB\s*\)\s+TO\s+authenticated\s*,\s*service_role", sql_without_comments, re.IGNORECASE)

    def test_exact_transaction_keys_validation(self, sql_without_comments: str):
        """Matrix D: p_transaction key set validation."""
        expected_tx_keys = [
            "account_id", "cash_amount", "cash_bucket_id", "cash_currency",
            "economic_fingerprint", "effective_date", "executed_at", "external_reference",
            "external_source", "from_amount", "from_currency", "id", "instrument_id",
            "notes", "owner_id", "portfolio_id", "quantity", "recorded_at",
            "reverses_transaction_id", "to_amount", "to_currency", "trade_currency",
            "transaction_type", "unit_price"
        ]
        for k in expected_tx_keys:
            assert f"'{k}'" in sql_without_comments

    def test_exact_binding_keys_validation(self, sql_without_comments: str):
        """Matrix E: p_binding key set validation."""
        expected_binding_keys = [
            "account_id", "expected_plan_sha256", "file_content_sha256", "owner_id",
            "portfolio_id", "record_ordinal", "record_sha256", "source_key", "transaction_id"
        ]
        for k in expected_binding_keys:
            assert f"'{k}'" in sql_without_comments

    def test_non_null_identity_checks(self, sql_without_comments: str):
        """Phase 13Q.1: Explicit non-null validation on all eight identity fields."""
        assert "Transaction and binding identity fields must be non-null" in sql_without_comments
        assert "v_tx_owner_id IS NULL" in sql_without_comments
        assert "v_tx_portfolio_id IS NULL" in sql_without_comments
        assert "v_tx_account_id IS NULL" in sql_without_comments
        assert "v_tx_id IS NULL" in sql_without_comments
        assert "v_binding_owner_id IS NULL" in sql_without_comments
        assert "v_binding_portfolio_id IS NULL" in sql_without_comments
        assert "v_binding_account_id IS NULL" in sql_without_comments
        assert "v_binding_tx_id IS NULL" in sql_without_comments

    def test_cross_payload_identity_checks(self, sql_without_comments: str):
        """Matrix F & Phase 13Q.1: Null-safe IS DISTINCT FROM identity equality between transaction and binding."""
        assert "Cross-payload identity mismatch" in sql_without_comments
        assert "v_tx_owner_id IS DISTINCT FROM v_binding_owner_id" in sql_without_comments
        assert "v_tx_portfolio_id IS DISTINCT FROM v_binding_portfolio_id" in sql_without_comments
        assert "v_tx_account_id IS DISTINCT FROM v_binding_account_id" in sql_without_comments
        assert "v_tx_id IS DISTINCT FROM v_binding_tx_id" in sql_without_comments
        assert "v_tx_owner_id <> v_binding_owner_id" not in sql_without_comments

    def test_binding_claim_fields_precheck_validation(self, sql_without_comments: str):
        """Phase 13Q.1 Matrix I-M, N-U: Binding claim fields non-null and domain grammar before claim lookup."""
        assert "Binding claim fields must be non-null" in sql_without_comments
        assert "v_binding_source_key !~ '^[a-z0-9][a-z0-9._-]{0,63}$'" in sql_without_comments
        assert "v_binding_file_sha !~ '^[0-9a-f]{64}$'" in sql_without_comments
        assert "v_binding_ordinal < 1" in sql_without_comments
        assert "v_binding_rec_sha !~ '^[0-9a-f]{64}$'" in sql_without_comments
        assert "v_binding_plan_sha !~ '^[0-9a-f]{64}$'" in sql_without_comments

    def test_external_identity_must_be_null(self, sql_without_comments: str):
        """Matrix G: External identity must be null in import transaction."""
        assert "p_transaction->>'external_source' IS NOT NULL" in sql_without_comments
        assert "p_transaction->>'external_reference' IS NOT NULL" in sql_without_comments
        assert "Import transactions must have null external_source and external_reference" in sql_without_comments

    def test_cash_bucket_must_be_null(self, sql_without_comments: str):
        """Matrix H: cash_bucket_id must be null in import transaction."""
        assert "p_transaction->>'cash_bucket_id' IS NOT NULL" in sql_without_comments
        assert "Import transactions must have null cash_bucket_id" in sql_without_comments

    def test_reversal_must_be_null(self, sql_without_comments: str):
        """Matrix I: Reversals forbidden in import transaction."""
        assert "p_transaction->>'reverses_transaction_id' IS NOT NULL" in sql_without_comments
        assert "transaction_type' = 'reversal'" in sql_without_comments
        assert "Import transactions cannot be reversals" in sql_without_comments

    def test_notes_must_be_null(self, sql_without_comments: str):
        """Matrix J: Notes must be null in import transaction."""
        assert "p_transaction->>'notes' IS NOT NULL" in sql_without_comments
        assert "Import transactions must have null notes" in sql_without_comments

    def test_economic_fingerprint_hex_validation(self, sql_without_comments: str):
        """Matrix K: Economic fingerprint 64-char hex format validation."""
        assert r"^[0-9a-f]{64}$" in sql_without_comments

    def test_timestamp_timezone_safety(self, sql_without_comments: str):
        """Matrix L: Timestamp timezone safety."""
        assert "recorded_at must be an explicit timezone-aware timestamp string" in sql_without_comments

    def test_atomic_dual_insert_and_race_handling(self, sql_without_comments: str):
        """Matrix M: Inserts into both tables and handles unique_violation race."""
        assert "INSERT INTO public.portfolio_transactions" in sql_without_comments
        assert "INSERT INTO public.portfolio_import_claim_bindings" in sql_without_comments
        assert "EXCEPTION WHEN unique_violation THEN" in sql_without_comments
        assert "appended" in sql_without_comments
        assert "idempotent_duplicate" in sql_without_comments
        assert "conflict" in sql_without_comments
