"""
backend/tests/test_portfolio_persistence_schema.py
==================================================
Comprehensive Schema & DB Invariant Verification for Supabase Migration 011 (Phase 12B.1).

Verifies that the Supabase SQL migration `011_portfolio_ledger_persistence.sql`:
    - Creates exactly the 6 authoritative domain tables (and no derived projection tables).
    - Preserves exact NUMERIC precision for all monetary/quantity values (NO float/real/double).
    - Encodes all 10 transaction types with fail-closed field-family CHECK constraints.
    - Encodes external idempotency all-or-none validation and normalized partial unique index.
    - Encodes reference-only REVERSAL constraints, single-reversal unique index, and cross-row validation.
    - Encodes CashBucket reference consistency and currency matching.
    - Enforces strict append-only immutability (UPDATE and DELETE blocked by trigger).
    - Enforces Row Level Security (RLS) with auth.uid() owner isolation on all 6 tables.
    - Enforces ON DELETE RESTRICT on all ledger-critical foreign keys.
"""

import os
import re
import pytest

MIGRATION_011_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations", "011_portfolio_ledger_persistence.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert os.path.exists(MIGRATION_011_PATH), f"Migration file missing: {MIGRATION_011_PATH}"
    with open(MIGRATION_011_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sql_without_comments(migration_sql: str) -> str:
    # Strip line comments and block comments
    no_line_comments = re.sub(r"--.*", "", migration_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestPortfolioPersistenceSchema:
    """Static and structural validation of migration 011."""

    def test_migration_011_file_exists(self, migration_sql: str):
        assert len(migration_sql) > 1000

    def test_exactly_six_persistent_tables_created(self, migration_sql: str):
        """Must create exactly the 6 authorized domain tables."""
        tables_created = re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\.([a-z_]+)",
            migration_sql,
            re.IGNORECASE,
        )
        expected_tables = {
            "portfolios",
            "portfolio_accounts",
            "cash_buckets",
            "investment_goals",
            "planned_contributions",
            "portfolio_transactions",
        }
        assert set(tables_created) == expected_tables

    def test_no_derived_authority_tables_created(self, migration_sql: str):
        """Derived projection concepts must NOT be persisted as tables."""
        forbidden_tables = [
            "position_lots",
            "current_positions",
            "holdings",
            "cash_balances",
            "tax_lots",
            "portfolio_values",
            "portfolio_weights",
            "valuation_caches",
        ]
        for tbl in forbidden_tables:
            assert f"CREATE TABLE public.{tbl}" not in migration_sql
            assert f"CREATE TABLE IF NOT EXISTS public.{tbl}" not in migration_sql

    def test_exact_numeric_precision_used_no_floating_point(self, sql_without_comments: str):
        """All financial fields must use NUMERIC; REAL, FLOAT, and DOUBLE PRECISION are strictly forbidden."""
        forbidden_types = [r"\bREAL\b", r"\bFLOAT\b", r"\bDOUBLE\s+PRECISION\b"]
        for pattern in forbidden_types:
            assert not re.search(pattern, sql_without_comments, re.IGNORECASE), f"Forbidden type found matching {pattern}"

        # Ensure financial fields in portfolio_transactions, goals, and contributions use NUMERIC
        assert re.search(r"quantity\s+NUMERIC", sql_without_comments)
        assert re.search(r"unit_price\s+NUMERIC", sql_without_comments)
        assert re.search(r"cash_amount\s+NUMERIC", sql_without_comments)
        assert re.search(r"from_amount\s+NUMERIC", sql_without_comments)
        assert re.search(r"to_amount\s+NUMERIC", sql_without_comments)
        assert re.search(r"target_amount\s+NUMERIC", sql_without_comments)
        assert re.search(r"amount\s+NUMERIC", sql_without_comments)

    def test_all_ten_transaction_types_represented(self, sql_without_comments: str):
        """All 10 canonical TransactionType enum values must be in the transaction_type check."""
        expected_types = [
            "buy", "sell", "cash_deposit", "cash_withdrawal",
            "dividend", "interest", "fx_conversion", "fee",
            "tax_withholding", "reversal"
        ]
        for t in expected_types:
            assert f"'{t}'" in sql_without_comments

    def test_field_family_check_constraints_present(self, sql_without_comments: str):
        """CHECK constraint on field families must enforce mutually exclusive contracts."""
        assert "chk_tx_field_families" in sql_without_comments
        assert "chk_tx_external_identity" in sql_without_comments
        assert "chk_portfolio_provenance" in sql_without_comments

    def test_external_idempotency_rules_and_partial_unique_index(self, sql_without_comments: str):
        """External idempotency must use normalized partial unique index without constraining manual events."""
        # Index on upper(trim(external_source)), trim(external_reference)
        assert "idx_portfolio_transactions_external_idempotency" in sql_without_comments
        assert "upper(trim(external_source))" in sql_without_comments
        assert "trim(external_reference)" in sql_without_comments
        assert "WHERE external_source IS NOT NULL AND external_reference IS NOT NULL" in sql_without_comments

    def test_reversal_single_reversal_index_and_integrity_trigger(self, sql_without_comments: str):
        """Reversals must be single-use, reference-only, and protected by trigger."""
        assert "idx_portfolio_transactions_unique_reversal" in sql_without_comments
        assert "validate_portfolio_transaction_integrity" in sql_without_comments
        assert "trg_validate_portfolio_transaction_integrity" in sql_without_comments

        # Trigger logic checks
        assert "Cross-portfolio reversal rejected" in sql_without_comments
        assert "Cross-account reversal rejected" in sql_without_comments
        assert "Reversal of a reversal transaction is strictly forbidden" in sql_without_comments

    def test_cash_bucket_currency_and_portfolio_integrity_trigger(self, sql_without_comments: str):
        """Cash bucket currency and ownership must be validated in trigger."""
        assert "CashBucket portfolio % does not match transaction portfolio" in sql_without_comments
        assert "CashBucket account % does not match transaction account" in sql_without_comments
        assert "Referenced CashBucket currency % does not match transaction cash_currency" in sql_without_comments
        assert "Referenced funding CashBucket currency % does not match transaction trade_currency" in sql_without_comments

    def test_immutability_anti_tamper_trigger(self, sql_without_comments: str):
        """Strict append-only immutability: UPDATE and DELETE on portfolio_transactions must be blocked."""
        assert "prevent_portfolio_transaction_tamper" in sql_without_comments
        assert "trg_prevent_portfolio_transaction_tamper" in sql_without_comments
        assert "BEFORE UPDATE OR DELETE ON public.portfolio_transactions" in sql_without_comments
        assert "Immutability violation" in sql_without_comments

    def test_rls_enabled_on_all_six_tables(self, sql_without_comments: str):
        """Row Level Security must be explicitly enabled on all 6 tables."""
        tables = [
            "portfolios",
            "portfolio_accounts",
            "cash_buckets",
            "investment_goals",
            "planned_contributions",
            "portfolio_transactions",
        ]
        for tbl in tables:
            assert f"ALTER TABLE public.{tbl} ENABLE ROW LEVEL SECURITY;" in sql_without_comments
            assert f"auth.uid() = owner_id" in sql_without_comments or f"(SELECT auth.uid()) = owner_id" in sql_without_comments

    def test_foreign_key_on_delete_restrict_safety(self, sql_without_comments: str):
        """Critical foreign keys must use ON DELETE RESTRICT to protect historical ledger integrity."""
        assert re.search(r"REFERENCES\s+public\.instruments\s*\(\s*id\s*\)\s+ON\s+DELETE\s+RESTRICT", sql_without_comments, re.IGNORECASE)
        assert re.search(r"REFERENCES\s+public\.portfolios\s*\(\s*id\s*,\s*owner_id\s*\)\s+ON\s+DELETE\s+RESTRICT", sql_without_comments, re.IGNORECASE)
        assert re.search(r"REFERENCES\s+public\.portfolio_accounts\s*\(\s*id\s*,\s*portfolio_id\s*\)\s+ON\s+DELETE\s+RESTRICT", sql_without_comments, re.IGNORECASE)
        assert re.search(r"REFERENCES\s+public\.portfolio_transactions\s*\(\s*id\s*\)\s+ON\s+DELETE\s+RESTRICT", sql_without_comments, re.IGNORECASE)
        assert "ON DELETE CASCADE" not in sql_without_comments  # No destructive cascades on ledger/account data

    def test_sandbox_provenance_same_owner_foreign_key(self, sql_without_comments: str):
        """Sandbox provenance reference must point to a portfolio belonging to the SAME owner."""
        assert "fk_portfolios_source_portfolio" in sql_without_comments
        assert re.search(r"FOREIGN\s+KEY\s*\(\s*source_portfolio_id\s*,\s*owner_id\s*\)\s+REFERENCES\s+public\.portfolios\s*\(\s*id\s*,\s*owner_id\s*\)", sql_without_comments, re.IGNORECASE)

    def test_planned_contributions_have_no_cash_authority(self, sql_without_comments: str):
        """Planned contributions must NOT have any triggers mutating balances or cash."""
        assert "cash_balances" not in sql_without_comments
        assert "UPDATE public.cash_buckets" not in sql_without_comments
        assert "INSERT INTO public.portfolio_transactions" not in sql_without_comments
