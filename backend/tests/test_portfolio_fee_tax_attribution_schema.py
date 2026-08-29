"""
backend/tests/test_portfolio_fee_tax_attribution_schema.py
=========================================================
Schema & DB Invariant Verification for Supabase Migration 018:
public.portfolio_fee_tax_attribution_events (Phase 14F).
"""

from __future__ import annotations

import os
import re
from typing import List

import pytest

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations"
)
MIGRATION_018_PATH = os.path.join(MIGRATIONS_DIR, "018_fee_tax_attribution_events.sql")


@pytest.fixture(scope="module")
def migration_sql() -> str:
    assert os.path.exists(MIGRATION_018_PATH), f"Migration file missing: {MIGRATION_018_PATH}"
    with open(MIGRATION_018_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sql_without_comments(migration_sql: str) -> str:
    no_line_comments = re.sub(r"--.*", "", migration_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestFeeTaxAttributionSchemaMigration:
    """Static schema & DB invariant verification for migration 018."""

    def test_migration_018_file_exists_and_earlier_untouched(self):
        """Item 41: Migration 018 exists and migrations 001-017 remain intact."""
        assert os.path.exists(MIGRATION_018_PATH)
        for i in range(1, 18):
            matches = [f for f in os.listdir(MIGRATIONS_DIR) if f.startswith(f"{i:03d}_")]
            assert len(matches) == 1, f"Expected exactly one migration for {i:03d}, got {matches}"

    def test_exact_table_created(self, migration_sql: str):
        """Item 3: Exact table name created."""
        assert "CREATE TABLE IF NOT EXISTS public.portfolio_fee_tax_attribution_events" in migration_sql

    def test_required_columns_present(self, sql_without_comments: str):
        """Item 4, 42: Exactly the 10 domain/persistence columns present."""
        required_cols = [
            r"id\s+UUID\s+NOT\s+NULL",
            r"portfolio_id\s+UUID\s+NOT\s+NULL",
            r"account_id\s+UUID\s+NOT\s+NULL",
            r"owner_id\s+UUID\s+NOT\s+NULL",
            r"event_type\s+VARCHAR\(\d+\)\s+NOT\s+NULL",
            r"recorded_at\s+TIMESTAMPTZ\s+NOT\s+NULL",
            r"charge_transaction_id\s+UUID",
            r"target_transaction_id\s+UUID",
            r"allocated_amount\s+NUMERIC",
            r"reverses_attribution_event_id\s+UUID",
        ]
        for col_pattern in required_cols:
            assert re.search(col_pattern, sql_without_comments, re.IGNORECASE), f"Missing column pattern: {col_pattern}"

    def test_prohibited_denormalized_columns_absent(self, sql_without_comments: str):
        """Item 4, 42, 59: Prohibited denormalized and status mutation columns are absent."""
        prohibited_cols = [
            "currency",
            "cash_currency",
            "trade_currency",
            "instrument_id",
            "charge_transaction_type",
            "target_transaction_type",
            "effective_date",
            "executed_at",
            "notes",
            "tax_rate",
            "tax_year",
            "broker",
            "source",
            "updated_at",
            "deleted_at",
            "is_reversed",
            "reversed_at",
            "status",
            "active",
        ]
        table_def_match = re.search(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.portfolio_fee_tax_attribution_events\s*\((.*?)\);",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert table_def_match is not None, "Could not find table definition block"
        table_def = table_def_match.group(1)

        for col in prohibited_cols:
            pattern = rf"\b{col}\b"
            # Exclude references in column definitions (only allow in comments, which are stripped)
            # In table definition, make sure no column with this name is defined
            col_def_pattern = rf"^\s*{col}\s+[A-Za-z]"
            assert not re.search(col_def_pattern, table_def, re.MULTILINE | re.IGNORECASE), (
                f"Prohibited column '{col}' defined in table: {col_def_pattern}"
            )

    def test_no_id_default(self, sql_without_comments: str):
        """Item 5, 43: ID column must NOT have gen_random_uuid() or other default."""
        table_def_match = re.search(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.portfolio_fee_tax_attribution_events\s*\((.*?)\);",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert table_def_match is not None
        table_def = table_def_match.group(1)
        assert not re.search(r"id\s+UUID[^\n,;]*DEFAULT", table_def, re.IGNORECASE), "id column must not have a DEFAULT"
        assert not re.search(r"gen_random_uuid\(\)", table_def, re.IGNORECASE), "id must not use gen_random_uuid()"

    def test_no_recorded_at_default(self, sql_without_comments: str):
        """Item 6, 44: recorded_at must NOT have now() or other database default."""
        table_def_match = re.search(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.portfolio_fee_tax_attribution_events\s*\((.*?)\);",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert table_def_match is not None
        table_def = table_def_match.group(1)
        assert not re.search(r"recorded_at\s+TIMESTAMPTZ[^\n,;]*DEFAULT", table_def, re.IGNORECASE), (
            "recorded_at column must not have a DEFAULT"
        )

    def test_allocated_amount_numeric_type(self, sql_without_comments: str):
        """Item 15, 45: allocated_amount uses exact NUMERIC and rejects REAL/FLOAT."""
        assert re.search(r"allocated_amount\s+NUMERIC", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"allocated_amount\s+(REAL|FLOAT|DOUBLE)", sql_without_comments, re.IGNORECASE)

    def test_event_type_universe_check(self, sql_without_comments: str):
        """Item 14: event_type check constraint allows strictly allocation and reversal."""
        assert re.search(
            r"chk_fee_tax_attribution_event_type\s+CHECK\s*\(\s*event_type\s+IN\s*\(\s*'allocation'\s*,\s*'reversal'\s*\)\s*\)",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_field_family_check_constraints(self, sql_without_comments: str):
        """Item 16, 46: Family CHECK constraint enforces ALLOCATION vs REVERSAL shapes."""
        chk_pattern = re.search(
            r"CONSTRAINT\s+chk_fee_tax_attribution_field_families\s+CHECK\s*\((.*?)\)\s*\);",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert chk_pattern is not None, "chk_fee_tax_attribution_field_families constraint missing"
        chk_body = chk_pattern.group(1)

        # ALLOCATION shape checks
        assert "event_type = 'allocation'" in chk_body
        assert "charge_transaction_id IS NOT NULL" in chk_body
        assert "target_transaction_id IS NOT NULL" in chk_body
        assert "allocated_amount IS NOT NULL" in chk_body
        assert "allocated_amount > 0" in chk_body
        assert "reverses_attribution_event_id IS NULL" in chk_body
        assert "charge_transaction_id <> target_transaction_id" in chk_body

        # REVERSAL shape checks
        assert "event_type = 'reversal'" in chk_body
        assert "charge_transaction_id IS NULL" in chk_body
        assert "target_transaction_id IS NULL" in chk_body
        assert "allocated_amount IS NULL" in chk_body
        assert "reverses_attribution_event_id IS NOT NULL" in chk_body
        assert "reverses_attribution_event_id <> id" in chk_body

    def test_composite_foreign_keys_and_restrict(self, sql_without_comments: str):
        """Item 8-13, 47: All foreign keys enforce relational scope with ON DELETE RESTRICT."""
        # 1. Portfolio FK (portfolio_id, owner_id) -> portfolios
        fk_port = (
            r"FOREIGN\s+KEY\s*\(\s*portfolio_id\s*,\s*owner_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolios\s*\(\s*id\s*,\s*owner_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_port, sql_without_comments, re.IGNORECASE), "Portfolio composite FK missing"

        # 2. Account FK (account_id, portfolio_id) -> portfolio_accounts
        fk_acc = (
            r"FOREIGN\s+KEY\s*\(\s*account_id\s*,\s*portfolio_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolio_accounts\s*\(\s*id\s*,\s*portfolio_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_acc, sql_without_comments, re.IGNORECASE), "Account composite FK missing"

        # 3. Charge Transaction FK (charge_transaction_id, portfolio_id, account_id) -> portfolio_transactions
        fk_charge = (
            r"FOREIGN\s+KEY\s*\(\s*charge_transaction_id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolio_transactions\s*\(\s*id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_charge, sql_without_comments, re.IGNORECASE), "Charge tx composite FK missing"

        # 4. Target Transaction FK (target_transaction_id, portfolio_id, account_id) -> portfolio_transactions
        fk_target = (
            r"FOREIGN\s+KEY\s*\(\s*target_transaction_id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolio_transactions\s*\(\s*id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_target, sql_without_comments, re.IGNORECASE), "Target tx composite FK missing"

        # 5. Reversal Event FK (reverses_attribution_event_id, portfolio_id, account_id) -> portfolio_fee_tax_attribution_events
        fk_rev = (
            r"FOREIGN\s+KEY\s*\(\s*reverses_attribution_event_id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"REFERENCES\s+public\.portfolio_fee_tax_attribution_events\s*\(\s*id\s*,\s*portfolio_id\s*,\s*account_id\s*\)\s*"
            r"ON\s+DELETE\s+RESTRICT"
        )
        assert re.search(fk_rev, sql_without_comments, re.IGNORECASE), "Reversal event composite FK missing"

        # Candidate key enabling the reversal self-reference
        uq_cand = (
            r"CONSTRAINT\s+uq_fee_tax_attribution_event_id_portfolio_account\s+UNIQUE\s*\(\s*"
            r"id\s*,\s*portfolio_id\s*,\s*account_id\s*\)"
        )
        assert re.search(uq_cand, sql_without_comments, re.IGNORECASE), "Composite candidate key missing"

    def test_single_reversal_unique_index(self, sql_without_comments: str):
        """Item 19, 48: Partial unique index prevents double reversal of an attribution event."""
        uq_idx = (
            r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+uq_fee_tax_attribution_single_reversal\s+"
            r"ON\s+public\.portfolio_fee_tax_attribution_events\s*\(\s*reverses_attribution_event_id\s*\)\s+"
            r"WHERE\s+event_type\s*=\s*'reversal'"
        )
        assert re.search(uq_idx, sql_without_comments, re.IGNORECASE)

    def test_query_indexes(self, sql_without_comments: str):
        """Item 31-34: Deterministic PIT and FK lookup indexes."""
        # PIT index
        assert re.search(
            r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_fee_tax_attribution_events_pit\s+ON\s+public\.portfolio_fee_tax_attribution_events\s*\(\s*portfolio_id\s*,\s*recorded_at\s*,\s*id\s*\)",
            sql_without_comments,
            re.IGNORECASE,
        )
        # Account PIT index
        assert re.search(
            r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_fee_tax_attribution_events_account_pit\s+ON\s+public\.portfolio_fee_tax_attribution_events\s*\(\s*portfolio_id\s*,\s*account_id\s*,\s*recorded_at\s*,\s*id\s*\)",
            sql_without_comments,
            re.IGNORECASE,
        )
        # Charge lookup index
        assert re.search(
            r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_fee_tax_attribution_events_charge_tx\s+ON\s+public\.portfolio_fee_tax_attribution_events\s*\(\s*charge_transaction_id\s*\)\s+WHERE\s+charge_transaction_id\s+IS\s+NOT\s+NULL",
            sql_without_comments,
            re.IGNORECASE,
        )
        # Target lookup index
        assert re.search(
            r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_fee_tax_attribution_events_target_tx\s+ON\s+public\.portfolio_fee_tax_attribution_events\s*\(\s*target_transaction_id\s*\)\s+WHERE\s+target_transaction_id\s+IS\s+NOT\s+NULL",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_relational_validation_trigger(self, sql_without_comments: str):
        """Item 20, 21, 22, 49, 50, 51: DB trigger enforces charge types, target types, and anti-reversal-of-reversal."""
        # Function exists
        assert "validate_fee_tax_attribution_event_integrity" in sql_without_comments

        # Charge type allowlist: 'fee', 'tax_withholding'
        assert re.search(r"IN\s*\(\s*'fee'\s*,\s*'tax_withholding'\s*\)", sql_without_comments, re.IGNORECASE)

        # Target type allowlist: 7 types
        target_types = ['buy', 'sell', 'dividend', 'interest', 'cash_deposit', 'cash_withdrawal', 'fx_conversion']
        for tt in target_types:
            assert f"'{tt}'" in sql_without_comments, f"Missing target type '{tt}' in trigger function"

        # Anti-reversal-of-reversal check: referenced event must have event_type = 'allocation'
        assert re.search(r"v_reversed_event_type\s*<>\s*'allocation'", sql_without_comments, re.IGNORECASE)

        # Trigger binding on BEFORE INSERT
        assert re.search(
            r"CREATE\s+TRIGGER\s+trg_validate_fee_tax_attribution_event_integrity\s+"
            r"BEFORE\s+INSERT\s+ON\s+public\.portfolio_fee_tax_attribution_events",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_immutability_anti_tamper_trigger(self, sql_without_comments: str):
        """Item 25, 52: Dedicated anti-tamper trigger prevents UPDATE and DELETE."""
        assert "prevent_fee_tax_attribution_event_tamper" in sql_without_comments
        assert re.search(
            r"CREATE\s+TRIGGER\s+trg_prevent_fee_tax_attribution_event_tamper\s+"
            r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+public\.portfolio_fee_tax_attribution_events",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_rls_and_privileges_hardening(self, sql_without_comments: str):
        """Item 26-29, 53-55: RLS enabled, authenticated SELECT only, service_role SELECT/INSERT only."""
        # RLS enabled
        assert re.search(
            r"ALTER\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
            sql_without_comments,
            re.IGNORECASE,
        )

        # Authenticated SELECT policy
        assert re.search(
            r"CREATE\s+POLICY\s+\"Users can view own fee tax attribution events\"\s+"
            r"ON\s+public\.portfolio_fee_tax_attribution_events\s+"
            r"FOR\s+SELECT\s+TO\s+authenticated\s+"
            r"USING\s*\(\s*\(\s*SELECT\s+auth\.uid\(\)\s*\)\s*=\s*owner_id\s*\)",
            sql_without_comments,
            re.IGNORECASE,
        )

        # No authenticated INSERT policy
        assert not re.search(
            r"CREATE\s+POLICY[^\n]*FOR\s+INSERT\s+TO\s+authenticated",
            sql_without_comments,
            re.IGNORECASE,
        )

        # Revocations from PUBLIC, anon, authenticated
        assert re.search(r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+FROM\s+PUBLIC", sql_without_comments, re.IGNORECASE)
        assert re.search(r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+FROM\s+anon", sql_without_comments, re.IGNORECASE)
        assert re.search(r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+FROM\s+authenticated", sql_without_comments, re.IGNORECASE)

        # Grant SELECT to authenticated
        assert re.search(r"GRANT\s+SELECT\s+ON\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+TO\s+authenticated", sql_without_comments, re.IGNORECASE)

        # Service role privileges: SELECT, INSERT only
        assert re.search(r"REVOKE\s+ALL\s+ON\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+FROM\s+service_role", sql_without_comments, re.IGNORECASE)
        assert re.search(r"GRANT\s+SELECT,\s*INSERT\s+ON\s+TABLE\s+public\.portfolio_fee_tax_attribution_events\s+TO\s+service_role", sql_without_comments, re.IGNORECASE)

    def test_no_attribution_write_rpcs_created(self, sql_without_comments: str):
        """Item 30, 56: Migration creates zero RPC write endpoints."""
        assert not re.search(r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+public\.(append|commit|reverse|insert)_fee_tax", sql_without_comments, re.IGNORECASE)

    def test_no_ledger_or_other_table_mutations(self, sql_without_comments: str):
        """Item 57: Zero modifications to portfolio_transactions or other existing tables."""
        assert not re.search(r"ALTER\s+TABLE\s+public\.portfolio_transactions", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ALTER\s+TABLE\s+public\.portfolios\b", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ALTER\s+TABLE\s+public\.portfolio_accounts\b", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ALTER\s+TABLE\s+public\.portfolio_import_claim_bindings\b", sql_without_comments, re.IGNORECASE)
