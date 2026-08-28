"""
backend/tests/test_portfolio_persistence_schema.py
==================================================
Comprehensive Schema & DB Invariant Verification for Supabase Migration 011 (Phase 12B.1, 12B.1A & 12B.1B).

Verifies that the Supabase SQL migration `011_portfolio_ledger_persistence.sql`:
    - Creates exactly the 6 authoritative domain tables (and no derived projection tables).
    - Preserves exact unconstrained NUMERIC precision for all monetary/quantity values (NO float/real/double, NO numeric(p,s) narrowing).
    - Enforces explicit non-finite rejection ('NaN', 'Infinity', '-Infinity') on all 7 financial NUMERIC fields.
    - Enforces exact parity with canonical `ContributionStatus` enum (rejects 'deferred').
    - Enforces exact parity with canonical `Currency` enum across all currency-bearing columns.
    - Enforces CashBucket identity/reference immutability via BEFORE UPDATE trigger.
    - Encodes all 10 transaction types with fail-closed field-family CHECK constraints.
    - Encodes external idempotency all-or-none validation and normalized partial unique index.
    - Encodes reference-only REVERSAL constraints, single-reversal unique index, and cross-row validation.
    - Encodes CashBucket reference consistency and currency matching.
    - Enforces strict append-only immutability (UPDATE and DELETE blocked by trigger on transactions).
    - Enforces Row Level Security (RLS) with auth.uid() owner isolation on all 6 tables.
    - Enforces ON DELETE RESTRICT on all ledger-critical foreign keys.
"""

import os
import re
import pytest

from backend.engine.private.domain import ContributionStatus, Currency, TransactionType

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
        """All financial fields must use unconstrained NUMERIC; REAL, FLOAT, DOUBLE PRECISION, and NUMERIC(p,s) are strictly forbidden."""
        forbidden_types = [r"\bREAL\b", r"\bFLOAT\b", r"\bDOUBLE\s+PRECISION\b", r"\bNUMERIC\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\)"]
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

    def test_all_seven_numeric_fields_reject_non_finite_values(self, sql_without_comments: str):
        """Phase 12B.1B: All 7 financial NUMERIC fields must explicitly reject 'NaN', 'Infinity', and '-Infinity'."""
        required_numeric_fields = [
            ("investment_goals", "target_amount", False),
            ("planned_contributions", "amount", False),
            ("portfolio_transactions", "quantity", True),
            ("portfolio_transactions", "unit_price", True),
            ("portfolio_transactions", "cash_amount", True),
            ("portfolio_transactions", "from_amount", True),
            ("portfolio_transactions", "to_amount", True),
        ]

        for table_name, col_name, is_nullable in required_numeric_fields:
            table_match = re.search(
                rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\.{table_name}\s*\((.*?)\);",
                sql_without_comments,
                re.DOTALL | re.IGNORECASE,
            )
            assert table_match is not None, f"Table {table_name} definition not found"
            t_def = table_match.group(1)

            col_match = re.search(
                rf"{col_name}\s+NUMERIC\s+(?:NOT\s+NULL\s+)?CHECK\s*\((.*?)\)(?:,|$)",
                t_def,
                re.DOTALL | re.IGNORECASE,
            )
            assert col_match is not None, f"CHECK constraint for {table_name}.{col_name} not found"
            check_clause = col_match.group(1)

            # Must contain explicit rejection of 'NaN', 'Infinity', '-Infinity'
            assert "'NaN'::numeric" in check_clause, f"Missing 'NaN'::numeric in {table_name}.{col_name}"
            assert "'Infinity'::numeric" in check_clause, f"Missing 'Infinity'::numeric in {table_name}.{col_name}"
            assert "'-Infinity'::numeric" in check_clause, f"Missing '-Infinity'::numeric in {table_name}.{col_name}"
            assert f"{col_name} > 0" in check_clause, f"Missing positive check ({col_name} > 0) in {table_name}.{col_name}"

            if is_nullable:
                assert f"{col_name} IS NULL" in check_clause, f"Nullable field {table_name}.{col_name} missing IS NULL check"

    def test_all_ten_transaction_types_represented(self, sql_without_comments: str):
        """All 10 canonical TransactionType enum values must be in the transaction_type check."""
        expected_types = {e.value for e in TransactionType}
        assert len(expected_types) == 10

        # Extract transaction_type CHECK list
        match = re.search(
            r"transaction_type\s+VARCHAR\(32\)\s+NOT\s+NULL\s+CHECK\s*\(\s*transaction_type\s+IN\s*\((.*?)\)\s*\)",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert match is not None, "transaction_type CHECK constraint not found"
        raw_items = match.group(1)
        found_types = set(re.findall(r"'([a-z_]+)'", raw_items))
        assert found_types == expected_types

    def test_contribution_status_exact_domain_parity(self, sql_without_comments: str):
        """Phase 12B.1A: planned_contributions.status must match ContributionStatus exactly."""
        expected_statuses = {e.value for e in ContributionStatus}
        assert expected_statuses == {"planned", "confirmed", "cancelled", "received"}

        # Extract status CHECK list from planned_contributions table
        match = re.search(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\.planned_contributions\s*\((.*?)\);",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert match is not None, "planned_contributions table definition not found"
        table_def = match.group(1)

        status_match = re.search(
            r"status\s+VARCHAR\(20\)\s+NOT\s+NULL\s+DEFAULT\s+'planned'\s+CHECK\s*\(\s*status\s+IN\s*\((.*?)\)\s*\)",
            table_def,
            re.DOTALL | re.IGNORECASE,
        )
        assert status_match is not None, "planned_contributions.status CHECK constraint not found"
        found_statuses = set(re.findall(r"'([a-z_]+)'", status_match.group(1)))
        assert found_statuses == expected_statuses

        # Explicitly verify 'deferred' is rejected and NOT present
        assert "deferred" not in found_statuses
        assert "'deferred'" not in table_def

    def test_currency_universe_exact_domain_parity(self, sql_without_comments: str):
        """Phase 12B.1A: All currency columns must be restricted to canonical Currency enum values."""
        expected_currencies = {e.value for e in Currency}
        assert expected_currencies == {"TRY", "USD", "EUR", "GBP", "XAU", "XAG"}

        # Helper to extract and verify currency IN (...) list
        def check_currency_constraint(table_name: str, col_name: str):
            table_match = re.search(
                rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\.{table_name}\s*\((.*?)\);",
                sql_without_comments,
                re.DOTALL | re.IGNORECASE,
            )
            assert table_match is not None, f"Table {table_name} definition not found"
            t_def = table_match.group(1)

            col_match = re.search(
                rf"{col_name}\s+VARCHAR\(10\)(?:.*?CHECK\s*\(\s*(?:{col_name}\s+IS\s+NULL\s+OR\s+)?{col_name}\s+IN\s*\((.*?)\)\s*\))",
                t_def,
                re.DOTALL | re.IGNORECASE,
            )
            assert col_match is not None, f"Currency CHECK constraint for {table_name}.{col_name} not found"
            found_curr = set(re.findall(r"'([A-Z]+)'", col_match.group(1)))
            assert found_curr == expected_currencies, (
                f"Mismatch in {table_name}.{col_name}: found {found_curr} != expected {expected_currencies}"
            )

        # 1. portfolios.base_currency
        check_currency_constraint("portfolios", "base_currency")
        # 2. portfolio_accounts.base_currency
        check_currency_constraint("portfolio_accounts", "base_currency")
        # 3. cash_buckets.currency
        check_currency_constraint("cash_buckets", "currency")
        # 4. investment_goals.target_currency
        check_currency_constraint("investment_goals", "target_currency")
        # 5. planned_contributions.currency
        check_currency_constraint("planned_contributions", "currency")
        # 6. portfolio_transactions trade_currency, cash_currency, from_currency, to_currency
        check_currency_constraint("portfolio_transactions", "trade_currency")
        check_currency_constraint("portfolio_transactions", "cash_currency")
        check_currency_constraint("portfolio_transactions", "from_currency")
        check_currency_constraint("portfolio_transactions", "to_currency")

    def test_cash_bucket_identity_immutability_trigger(self, sql_without_comments: str):
        """Phase 12B.1A: cash_buckets must have BEFORE UPDATE trigger protecting identity fields."""
        assert "prevent_cash_bucket_identity_mutation" in sql_without_comments
        assert "trg_prevent_cash_bucket_identity_mutation" in sql_without_comments
        assert "BEFORE UPDATE ON public.cash_buckets" in sql_without_comments

        # Extract trigger function definition
        func_match = re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.prevent_cash_bucket_identity_mutation\(\).*?BEGIN(.*?)END;",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert func_match is not None, "prevent_cash_bucket_identity_mutation function body not found"
        func_body = func_match.group(1)

        # Protected identity fields
        assert "OLD.id IS DISTINCT FROM NEW.id" in func_body
        assert "OLD.portfolio_id IS DISTINCT FROM NEW.portfolio_id" in func_body
        assert "OLD.owner_id IS DISTINCT FROM NEW.owner_id" in func_body
        assert "OLD.account_id IS DISTINCT FROM NEW.account_id" in func_body
        assert "OLD.currency IS DISTINCT FROM NEW.currency" in func_body

        # Ensure lifecycle fields (name, purpose, included_in_investable_assets, archived_at) are NOT blocked
        assert "OLD.name IS DISTINCT FROM" not in func_body
        assert "OLD.purpose IS DISTINCT FROM" not in func_body
        assert "OLD.included_in_investable_assets IS DISTINCT FROM" not in func_body
        assert "OLD.archived_at IS DISTINCT FROM" not in func_body

    def test_field_family_check_constraints_present(self, sql_without_comments: str):
        """CHECK constraint on field families must enforce mutually exclusive contracts."""
        assert "chk_tx_field_families" in sql_without_comments
        assert "chk_tx_external_identity" in sql_without_comments
        assert "chk_portfolio_provenance" in sql_without_comments

    def test_external_idempotency_rules_and_partial_unique_index(self, sql_without_comments: str):
        """External idempotency must use normalized partial unique index without constraining manual events."""
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
        """Cash bucket currency and ownership must be validated in transaction insert trigger."""
        assert "CashBucket portfolio % does not match transaction portfolio" in sql_without_comments
        assert "CashBucket account % does not match transaction account" in sql_without_comments
        assert "Referenced CashBucket currency % does not match transaction cash_currency" in sql_without_comments
        assert "Referenced funding CashBucket currency % does not match transaction trade_currency" in sql_without_comments

    def test_immutability_anti_tamper_trigger_on_transactions(self, sql_without_comments: str):
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


MIGRATION_012_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations", "012_portfolio_external_identity_lookup.sql"
)


@pytest.fixture(scope="module")
def migration_012_sql() -> str:
    assert os.path.exists(MIGRATION_012_PATH), f"Migration file missing: {MIGRATION_012_PATH}"
    with open(MIGRATION_012_PATH, "r", encoding="utf-8") as f:
        return f.read()


class TestMigration012ExternalIdentityLookup:
    """Static and structural validation of migration 012."""

    def test_migration_012_file_exists(self, migration_012_sql: str):
        assert len(migration_012_sql) > 200

    def test_migration_012_function_signature_and_security(self, migration_012_sql: str):
        """Must define lookup_portfolio_transaction_external_identity with SECURITY INVOKER."""
        no_comments = re.sub(r"--.*", "", migration_012_sql)

        assert "CREATE OR REPLACE FUNCTION public.lookup_portfolio_transaction_external_identity" in no_comments
        assert "RETURNS UUID" in no_comments
        assert "LANGUAGE sql" in no_comments
        assert "STABLE" in no_comments
        assert "SECURITY INVOKER" in no_comments
        assert "SECURITY DEFINER" not in no_comments

    def test_migration_012_deterministic_search_path(self, migration_012_sql: str):
        """Must set explicit deterministic search_path."""
        no_comments = re.sub(r"--.*", "", migration_012_sql)
        assert re.search(r"SET\s+search_path\s*=\s*public\s*,\s*pg_temp", no_comments, re.IGNORECASE)

    def test_migration_012_where_clause_and_normalization(self, migration_012_sql: str):
        """Must strictly scope owner, portfolio, account, and use normalized string comparisons."""
        no_comments = re.sub(r"--.*", "", migration_012_sql)

        assert "pt.owner_id = p_owner_id" in no_comments
        assert "pt.portfolio_id = p_portfolio_id" in no_comments
        assert "pt.account_id = p_account_id" in no_comments
        assert "upper(trim(pt.external_source)) = upper(trim(p_external_source))" in no_comments
        assert "trim(pt.external_reference) = trim(p_external_reference)" in no_comments
        assert "SELECT pt.id" in no_comments
        assert "NUMERIC" not in no_comments

    def test_migration_012_permissions_hardening(self, migration_012_sql: str):
        """Must revoke from PUBLIC and grant only to authenticated and service_role."""
        no_comments = re.sub(r"--.*", "", migration_012_sql)

        assert "REVOKE EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity" in no_comments
        assert "FROM PUBLIC" in no_comments
        assert "GRANT EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity" in no_comments
        assert "TO authenticated, service_role" in no_comments
