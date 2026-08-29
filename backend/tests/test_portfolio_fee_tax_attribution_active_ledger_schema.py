"""
backend/tests/test_portfolio_fee_tax_attribution_active_ledger_schema.py
========================================================================
Schema & DB Invariant Verification for Supabase Migration 020:
public.portfolio_fee_tax_attribution_events active ledger reference &
reversal-race hardening (Phase 14G.1).
"""

from __future__ import annotations

import os
import re

import pytest

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations"
)
MIGRATION_018_PATH = os.path.join(MIGRATIONS_DIR, "018_fee_tax_attribution_events.sql")
MIGRATION_019_PATH = os.path.join(MIGRATIONS_DIR, "019_fee_tax_attribution_history_hardening.sql")
MIGRATION_020_PATH = os.path.join(MIGRATIONS_DIR, "020_fee_tax_attribution_active_ledger_reference_hardening.sql")


@pytest.fixture(scope="module")
def migration_020_sql() -> str:
    assert os.path.exists(MIGRATION_020_PATH), f"Migration file missing: {MIGRATION_020_PATH}"
    with open(MIGRATION_020_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sql_without_comments(migration_020_sql: str) -> str:
    no_line_comments = re.sub(r"--.*", "", migration_020_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestFeeTaxAttributionActiveLedgerSchema:
    """Static schema & DB invariant verification for migration 020."""

    def test_migration_chain_intact(self):
        """Item 33: Migrations 018, 019, 020 exist; migrations 001-019 untouched."""
        assert os.path.exists(MIGRATION_018_PATH)
        assert os.path.exists(MIGRATION_019_PATH)
        assert os.path.exists(MIGRATION_020_PATH)
        for i in range(1, 20):
            matches = [f for f in os.listdir(MIGRATIONS_DIR) if f.startswith(f"{i:03d}_")]
            assert len(matches) == 1, f"Expected exactly one migration for {i:03d}, got {matches}"

    def test_attribution_function_replaced_and_trigger_bound(self, sql_without_comments: str):
        """Item 34: Migration 020 replaces validate_fee_tax_attribution_event_integrity()."""
        assert re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.validate_fee_tax_attribution_event_integrity\(\)",
            sql_without_comments,
            re.IGNORECASE,
        )
        assert re.search(
            r"CREATE\s+TRIGGER\s+trg_validate_fee_tax_attribution_event_integrity\s+"
            r"BEFORE\s+INSERT\s+ON\s+public\.portfolio_fee_tax_attribution_events",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_charge_lock_retained(self, sql_without_comments: str):
        """Item 35: ALLOCATION path locks charge transaction row with FOR UPDATE."""
        alloc_block_match = re.search(
            r"IF\s+NEW\.event_type\s*=\s*'allocation'\s*THEN(.*?)ELSIF\s+NEW\.event_type\s*=\s*'reversal'",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert alloc_block_match is not None
        alloc_block = alloc_block_match.group(1)

        assert re.search(
            r"WHERE\s+id\s*=\s*v_charge_id[^;]*FOR\s+UPDATE",
            alloc_block,
            re.DOTALL | re.IGNORECASE,
        )

    def test_target_lock_added(self, sql_without_comments: str):
        """Item 36: ALLOCATION path locks target transaction row with FOR UPDATE."""
        alloc_block_match = re.search(
            r"IF\s+NEW\.event_type\s*=\s*'allocation'\s*THEN(.*?)ELSIF\s+NEW\.event_type\s*=\s*'reversal'",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert alloc_block_match is not None
        alloc_block = alloc_block_match.group(1)

        assert re.search(
            r"WHERE\s+id\s*=\s*NEW\.target_transaction_id[^;]*FOR\s+UPDATE",
            alloc_block,
            re.DOTALL | re.IGNORECASE,
        )

    def test_charge_reversal_pit_predicate(self, sql_without_comments: str):
        """Item 37, 39, 40: Rejects if charge was reversed at or before attribution recorded_at."""
        charge_reversal_check = (
            r"rev\.transaction_type\s*=\s*'reversal'\s+"
            r"AND\s+rev\.reverses_transaction_id\s*=\s*v_charge_id\s+"
            r"AND\s+rev\.recorded_at\s*<=\s*NEW\.recorded_at"
        )
        assert re.search(charge_reversal_check, sql_without_comments, re.IGNORECASE)

    def test_target_reversal_pit_predicate(self, sql_without_comments: str):
        """Item 38, 39, 40: Rejects if target was reversed at or before attribution recorded_at."""
        target_reversal_check = (
            r"rev\.transaction_type\s*=\s*'reversal'\s+"
            r"AND\s+rev\.reverses_transaction_id\s*=\s*NEW\.target_transaction_id\s+"
            r"AND\s+rev\.recorded_at\s*<=\s*NEW\.recorded_at"
        )
        assert re.search(target_reversal_check, sql_without_comments, re.IGNORECASE)

    def test_no_effective_date_in_active_check(self, sql_without_comments: str):
        """Item 41: Activity check is controlled strictly by recorded_at, not effective_date."""
        assert not re.search(r"effective_date", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"executed_at", sql_without_comments, re.IGNORECASE)

    def test_ledger_synchronization_function_and_trigger(self, sql_without_comments: str):
        """Item 42, 44, 45: Ledger reversal sync function locks reversed transaction row FOR UPDATE."""
        assert re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.lock_portfolio_transaction_reversal_target\(\)",
            sql_without_comments,
            re.IGNORECASE,
        )
        assert re.search(
            r"CREATE\s+TRIGGER\s+trg_lock_portfolio_transaction_reversal_target\s+"
            r"BEFORE\s+INSERT\s+ON\s+public\.portfolio_transactions",
            sql_without_comments,
            re.IGNORECASE,
        )
        # Function locks reversed transaction
        assert re.search(
            r"IF\s+NEW\.transaction_type\s*=\s*'reversal'\s+AND\s+NEW\.reverses_transaction_id\s+IS\s+NOT\s+NULL\s+THEN\s+"
            r"PERFORM\s+1\s+FROM\s+public\.portfolio_transactions\s+WHERE\s+id\s*=\s*NEW\.reverses_transaction_id\s+FOR\s+UPDATE;",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_ledger_sync_function_purity(self, sql_without_comments: str):
        """Item 43: Ledger sync function contains zero mutation, tax, fee, or cash-bucket logic."""
        func_match = re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.lock_portfolio_transaction_reversal_target\(\)\s*RETURNS\s+TRIGGER\s+AS\s*\$\$(.*?)\$\$\s*LANGUAGE\s+plpgsql;",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert func_match is not None
        func_body = func_match.group(1)

        # Prohibit DML mutations
        assert not re.search(r"\bINSERT\s+INTO\b", func_body, re.IGNORECASE)
        assert not re.search(r"\bUPDATE\s+public\.", func_body, re.IGNORECASE)
        assert not re.search(r"\bDELETE\s+FROM\b", func_body, re.IGNORECASE)

        prohibited = ["cash_bucket", "tax", "fee", "effective_date"]
        for p in prohibited:
            assert not re.search(rf"\b{p}\b", func_body, re.IGNORECASE)

    def test_existing_ledger_validator_not_replaced(self, sql_without_comments: str):
        """Item 46: Migration 020 must NOT touch validate_portfolio_transaction_integrity()."""
        assert not re.search(
            r"validate_portfolio_transaction_integrity",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_phase_14g_capacity_and_history_regressions(self, sql_without_comments: str):
        """Item 47, 48, 49, 50: Retains all Phase 14G capacity, duplicate-pair, causality, and monotonic rules."""
        # Single allocation capacity
        assert re.search(r"NEW\.allocated_amount\s*>\s*v_charge_cash_amount", sql_without_comments, re.IGNORECASE)
        # Cumulative active capacity
        assert re.search(r"v_active_allocated_total\s*\+\s*NEW\.allocated_amount\s*>\s*v_charge_cash_amount", sql_without_comments, re.IGNORECASE)
        # Active duplicate pair rejection
        assert re.search(r"ae\.charge_transaction_id\s*=\s*v_charge_id\s+AND\s+ae\.target_transaction_id\s*=\s*NEW\.target_transaction_id", sql_without_comments, re.IGNORECASE)
        # Knowledge-time causality
        assert re.search(r"NEW\.recorded_at\s*<\s*v_charge_recorded_at", sql_without_comments, re.IGNORECASE)
        assert re.search(r"NEW\.recorded_at\s*<\s*v_target_recorded_at", sql_without_comments, re.IGNORECASE)
        # Monotonic history
        assert re.search(r"NEW\.recorded_at\s*<\s*v_max_prior_recorded_at", sql_without_comments, re.IGNORECASE)
        # Reversal target is allocation
        assert re.search(r"v_reversed_event_type\s*<>\s*'allocation'", sql_without_comments, re.IGNORECASE)

    def test_no_column_changes_in_migration_020(self, sql_without_comments: str):
        """Item 52: Migration 020 contains zero ALTER TABLE or ADD/DROP COLUMN statements."""
        assert not re.search(r"ALTER\s+TABLE", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ADD\s+COLUMN", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"DROP\s+COLUMN", sql_without_comments, re.IGNORECASE)

    def test_no_privilege_changes_in_migration_020(self, sql_without_comments: str):
        """Item 53: Migration 020 contains zero privilege modifications."""
        assert not re.search(r"\bGRANT\b", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"\bREVOKE\b", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"CREATE\s+POLICY", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"DROP\s+POLICY", sql_without_comments, re.IGNORECASE)

    def test_no_rpc_created_in_migration_020(self, sql_without_comments: str):
        """Item 54: Migration 020 creates zero application write RPCs."""
        assert not re.search(
            r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+public\.(append|commit|reverse|insert)_",
            sql_without_comments,
            re.IGNORECASE,
        )
