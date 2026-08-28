"""
Schema & DB Invariant Verification for Supabase Migration 014:
public.portfolio_import_claim_bindings (Phase 13P).
"""

from __future__ import annotations

import os
import re

import pytest

MIGRATION_014_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations", "014_portfolio_import_claim_bindings.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert os.path.exists(MIGRATION_014_PATH), f"Migration file missing: {MIGRATION_014_PATH}"
    with open(MIGRATION_014_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sql_without_comments(migration_sql: str) -> str:
    no_line_comments = re.sub(r"--.*", "", migration_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestPortfolioImportClaimSchema:
    """Static schema & DB invariant verification for migration 014."""

    def test_migration_014_file_exists(self, migration_sql: str):
        assert len(migration_sql) > 500

    def test_exact_table_created(self, migration_sql: str):
        """Matrix A: Exact table name created."""
        assert "CREATE TABLE IF NOT EXISTS public.portfolio_import_claim_bindings" in migration_sql

    def test_required_columns_present(self, sql_without_comments: str):
        """Matrix B: All required columns present."""
        required_cols = [
            r"owner_id\s+UUID\s+NOT\s+NULL",
            r"portfolio_id\s+UUID\s+NOT\s+NULL",
            r"account_id\s+UUID\s+NOT\s+NULL",
            r"source_key\s+VARCHAR\(64\)\s+NOT\s+NULL",
            r"file_content_sha256\s+VARCHAR\(64\)\s+NOT\s+NULL",
            r"record_ordinal\s+BIGINT\s+NOT\s+NULL",
            r"record_sha256\s+VARCHAR\(64\)\s+NOT\s+NULL",
            r"expected_plan_sha256\s+VARCHAR\(64\)\s+NOT\s+NULL",
            r"transaction_id\s+UUID\s+NOT\s+NULL",
            r"bound_at\s+TIMESTAMPTZ\s+NOT\s+NULL",
        ]
        for col_pattern in required_cols:
            assert re.search(col_pattern, sql_without_comments, re.IGNORECASE), f"Missing column pattern: {col_pattern}"

    def test_bound_at_default_is_timezone_independent(self, sql_without_comments: str):
        """Phase 13P.2: bound_at uses native now() DEFAULT without timezone reinterpretation."""
        assert re.search(
            r"bound_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\(\)",
            sql_without_comments,
            re.IGNORECASE,
        )
        assert re.search(r"bound_at[^\n,;]*timezone\s*\(", sql_without_comments, re.IGNORECASE) is None
        assert re.search(r"bound_at[^\n,;]*AT\s+TIME\s+ZONE", sql_without_comments, re.IGNORECASE) is None

    def test_composite_primary_key_and_no_surrogate_id(self, sql_without_comments: str):
        """Matrix C, D, E, F: Primary key is exactly the composite claim identity."""
        # No surrogate id UUID PK
        assert not re.search(r"id\s+UUID\s+PRIMARY\s+KEY", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"gen_random_uuid\(\)", sql_without_comments, re.IGNORECASE)

        # Primary key regex
        pk_pattern = (
            r"CONSTRAINT\s+pk_portfolio_import_claim_bindings\s+PRIMARY\s+KEY\s*\(\s*"
            r"portfolio_id\s*,\s*"
            r"account_id\s*,\s*"
            r"source_key\s*,\s*"
            r"file_content_sha256\s*,\s*"
            r"record_ordinal\s*,\s*"
            r"record_sha256\s*\)"
        )
        assert re.search(pk_pattern, sql_without_comments, re.IGNORECASE), "Composite PK mismatch"

    def test_foreign_keys_enforced_with_restrict(self, sql_without_comments: str):
        """Matrix G, H, I, J: Foreign keys enforce relational consistency ON DELETE RESTRICT."""
        # Portfolio FK
        fk_port = (
            r"FOREIGN\s+KEY\s*\(\s*portfolio_id\s*,\s*owner_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolios\s*\(\s*id\s*,\s*owner_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_port, sql_without_comments, re.IGNORECASE)

        # Account FK
        fk_acc = (
            r"FOREIGN\s+KEY\s*\(\s*account_id\s*,\s*portfolio_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolio_accounts\s*\(\s*id\s*,\s*portfolio_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_acc, sql_without_comments, re.IGNORECASE)

        # Transaction FK
        fk_tx = (
            r"FOREIGN\s+KEY\s*\(\s*transaction_id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolio_transactions\s*\(\s*id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_tx, sql_without_comments, re.IGNORECASE)

    def test_check_constraints(self, sql_without_comments: str):
        """Matrix K, L, M, N, O: Source key, SHAs, and ordinal check constraints."""
        assert re.search(r"source_key\s*~\s*'\^\[a-z0-9\]\[a-z0-9\._-\]\{0,63\}\$'", sql_without_comments)
        assert re.search(r"file_content_sha256\s*~\s*'\^\[0-9a-f\]\{64\}\$'", sql_without_comments)
        assert re.search(r"record_sha256\s*~\s*'\^\[0-9a-f\]\{64\}\$'", sql_without_comments)
        assert re.search(r"expected_plan_sha256\s*~\s*'\^\[0-9a-f\]\{64\}\$'", sql_without_comments)
        assert re.search(r"record_ordinal\s*>=\s*1", sql_without_comments)

    def test_anti_tamper_immutability_trigger(self, sql_without_comments: str):
        """Immutability trigger prevents UPDATE and DELETE."""
        assert "prevent_import_claim_binding_tamper" in sql_without_comments
        assert re.search(r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+public\.portfolio_import_claim_bindings", sql_without_comments, re.IGNORECASE)
        assert "Immutability violation" in sql_without_comments

    def test_rls_and_policies(self, sql_without_comments: str):
        """Matrix P, Q, R, S, T: RLS enabled with owner SELECT/INSERT only; no UPDATE, DELETE, or FOR ALL."""
        assert "ALTER TABLE public.portfolio_import_claim_bindings ENABLE ROW LEVEL SECURITY" in sql_without_comments
        assert "FOR SELECT" in sql_without_comments
        assert "FOR INSERT" in sql_without_comments
        assert "FOR UPDATE" not in sql_without_comments
        assert "FOR DELETE" not in sql_without_comments
        assert "FOR ALL" not in sql_without_comments

    def test_transaction_id_index_non_unique(self, sql_without_comments: str):
        """Transaction ID has a non-unique index."""
        idx_match = re.search(r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?idx_import_claim_bindings_transaction_id", sql_without_comments, re.IGNORECASE)
        assert idx_match is not None
        assert idx_match.group(1) is None, "transaction_id index must be NON-UNIQUE!"

    def test_no_forbidden_financial_or_provenance_columns(self, sql_without_comments: str):
        """No financial economics or provenance display metadata in claim table."""
        forbidden_cols = [
            "quantity",
            "unit_price",
            "trade_currency",
            "cash_amount",
            "cash_currency",
            "from_amount",
            "to_amount",
            "instrument_id",
            "filename",
            "imported_at",
            "raw_content",
            "raw_record",
            "external_source",
            "external_reference",
            "idempotency_key",
        ]
        for col in forbidden_cols:
            assert not re.search(rf"\b{col}\b\s+[A-Z]", sql_without_comments, re.IGNORECASE), f"Forbidden column found: {col}"
