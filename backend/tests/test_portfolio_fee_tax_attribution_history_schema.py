"""
backend/tests/test_portfolio_fee_tax_attribution_history_schema.py
==================================================================
Schema & DB Invariant Verification for Supabase Migration 019:
public.portfolio_fee_tax_attribution_events history & concurrency
hardening (Phase 14G).
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


@pytest.fixture(scope="module")
def migration_019_sql() -> str:
    assert os.path.exists(MIGRATION_019_PATH), f"Migration file missing: {MIGRATION_019_PATH}"
    with open(MIGRATION_019_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sql_without_comments(migration_019_sql: str) -> str:
    no_line_comments = re.sub(r"--.*", "", migration_019_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestFeeTaxAttributionHistoryHardeningSchema:
    """Static schema & DB invariant verification for migration 019."""

    def test_migration_chain_intact(self):
        """Item 38: Migration 018 and 019 exist; migrations 001-018 untouched."""
        assert os.path.exists(MIGRATION_018_PATH)
        assert os.path.exists(MIGRATION_019_PATH)
        for i in range(1, 19):
            matches = [f for f in os.listdir(MIGRATIONS_DIR) if f.startswith(f"{i:03d}_")]
            assert len(matches) == 1, f"Expected exactly one migration for {i:03d}, got {matches}"

    def test_function_replaced_and_trigger_bound(self, sql_without_comments: str):
        """Item 39: Migration 019 replaces validate_fee_tax_attribution_event_integrity()."""
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

    def test_charge_row_locked_for_update_in_allocation(self, sql_without_comments: str):
        """Item 40: ALLOCATION path locks referenced charge ledger transaction with FOR UPDATE."""
        alloc_block_match = re.search(
            r"IF\s+NEW\.event_type\s*=\s*'allocation'\s*THEN(.*?)ELSIF\s+NEW\.event_type\s*=\s*'reversal'",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert alloc_block_match is not None, "Could not find ALLOCATION block in trigger function"
        alloc_block = alloc_block_match.group(1)

        assert re.search(
            r"FROM\s+public\.portfolio_transactions\s+WHERE[^;]*FOR\s+UPDATE",
            alloc_block,
            re.IGNORECASE,
        ), "ALLOCATION block must lock portfolio_transactions with FOR UPDATE"

    def test_reversal_locks_charge_row_for_update(self, sql_without_comments: str):
        """Item 41: REVERSAL path resolves referenced allocation's charge_transaction_id and locks it with FOR UPDATE."""
        reversal_block_match = re.search(
            r"ELSIF\s+NEW\.event_type\s*=\s*'reversal'\s*THEN(.*)RETURN\s+NEW;",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert reversal_block_match is not None, "Could not find REVERSAL block in trigger function"
        reversal_block = reversal_block_match.group(1)

        # Fetches referenced allocation
        assert re.search(
            r"FROM\s+public\.portfolio_fee_tax_attribution_events\s+WHERE\s+id\s*=\s*NEW\.reverses_attribution_event_id",
            reversal_block,
            re.IGNORECASE,
        )
        # Locks charge transaction
        assert re.search(
            r"FROM\s+public\.portfolio_transactions\s+WHERE[^;]*FOR\s+UPDATE",
            reversal_block,
            re.IGNORECASE,
        ), "REVERSAL block must lock charge transaction with FOR UPDATE"

    def test_single_allocation_capacity_check(self, sql_without_comments: str):
        """Item 42: SQL rejects single allocation exceeding charge cash_amount."""
        assert re.search(
            r"NEW\.allocated_amount\s*>\s*v_charge_cash_amount",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_cumulative_active_capacity_check(self, sql_without_comments: str):
        """Item 43, 44: Cumulative active allocation sum + NEW.allocated_amount <= charge.cash_amount."""
        # Sum of active allocations
        assert re.search(
            r"SELECT\s+COALESCE\s*\(\s*SUM\s*\(\s*ae\.allocated_amount\s*\)\s*,\s*0::numeric\s*\)",
            sql_without_comments,
            re.IGNORECASE,
        )
        # Cumulative capacity check
        assert re.search(
            r"v_active_allocated_total\s*\+\s*NEW\.allocated_amount\s*>\s*v_charge_cash_amount",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_active_definition_and_no_status_columns(self, sql_without_comments: str):
        """Item 44: Active status derived strictly via append-only history (NOT EXISTS reversal)."""
        active_subquery = (
            r"NOT\s+EXISTS\s*\(\s*"
            r"SELECT\s+1\s+FROM\s+public\.portfolio_fee_tax_attribution_events\s+rev\s+"
            r"WHERE\s+rev\.portfolio_id\s*=\s*NEW\.portfolio_id\s+"
            r"AND\s+rev\.account_id\s*=\s*NEW\.account_id\s+"
            r"AND\s+rev\.event_type\s*=\s*'reversal'\s+"
            r"AND\s+rev\.reverses_attribution_event_id\s*=\s*ae\.id\s*\)"
        )
        assert re.search(active_subquery, sql_without_comments, re.IGNORECASE)

        # No status column additions in migration
        assert not re.search(r"ALTER\s+TABLE", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ADD\s+COLUMN", sql_without_comments, re.IGNORECASE)

    def test_active_duplicate_pair_rejection_and_reversed_pair_reuse(self, sql_without_comments: str):
        """Item 45, 46: At most one ACTIVE allocation per exact charge->target pair."""
        dup_query = (
            r"EXISTS\s*\(\s*"
            r"SELECT\s+1\s+FROM\s+public\.portfolio_fee_tax_attribution_events\s+ae\s+"
            r"WHERE\s+ae\.portfolio_id\s*=\s*NEW\.portfolio_id\s+"
            r"AND\s+ae\.account_id\s*=\s*NEW\.account_id\s+"
            r"AND\s+ae\.charge_transaction_id\s*=\s*v_charge_id\s+"
            r"AND\s+ae\.target_transaction_id\s*=\s*NEW\.target_transaction_id\s+"
            r"AND\s+ae\.event_type\s*=\s*'allocation'\s+"
            r"AND\s+NOT\s+EXISTS\s*\(\s*"
            r"SELECT\s+1\s+FROM\s+public\.portfolio_fee_tax_attribution_events\s+rev\s+"
            r"WHERE\s+rev\.portfolio_id\s*=\s*NEW\.portfolio_id\s+"
            r"AND\s+rev\.account_id\s*=\s*NEW\.account_id\s+"
            r"AND\s+rev\.event_type\s*=\s*'reversal'\s+"
            r"AND\s+rev\.reverses_attribution_event_id\s*=\s*ae\.id\s*\)\s*\)"
        )
        assert re.search(dup_query, sql_without_comments, re.IGNORECASE)

    def test_knowledge_time_causality_allocation(self, sql_without_comments: str):
        """Item 47: Allocation recorded_at >= charge recorded_at and target recorded_at."""
        assert re.search(r"NEW\.recorded_at\s*<\s*v_charge_recorded_at", sql_without_comments, re.IGNORECASE)
        assert re.search(r"NEW\.recorded_at\s*<\s*v_target_recorded_at", sql_without_comments, re.IGNORECASE)

    def test_knowledge_time_causality_reversal(self, sql_without_comments: str):
        """Item 48: Reversal recorded_at >= referenced allocation recorded_at."""
        assert re.search(r"NEW\.recorded_at\s*<\s*v_ref_allocation_recorded_at", sql_without_comments, re.IGNORECASE)

    def test_per_charge_monotonic_history(self, sql_without_comments: str):
        """Item 49, 50: NEW.recorded_at >= MAX(prior recorded_at for charge allocations & reversals)."""
        max_subquery = (
            r"SELECT\s+MAX\s*\(\s*ae\.recorded_at\s*\)\s+INTO\s+v_max_prior_recorded_at\s+"
            r"FROM\s+public\.portfolio_fee_tax_attribution_events\s+ae\s+"
            r"WHERE\s+ae\.portfolio_id\s*=\s*NEW\.portfolio_id\s+"
            r"AND\s+ae\.account_id\s*=\s*NEW\.account_id\s+"
            r"AND\s*\(\s*"
            r"ae\.charge_transaction_id\s*=\s*v_charge_id\s+"
            r"OR\s+ae\.reverses_attribution_event_id\s+IN\s*\(\s*"
            r"SELECT\s+alloc\.id\s+FROM\s+public\.portfolio_fee_tax_attribution_events\s+alloc\s+"
            r"WHERE\s+alloc\.portfolio_id\s*=\s*NEW\.portfolio_id\s+"
            r"AND\s+alloc\.account_id\s*=\s*NEW\.account_id\s+"
            r"AND\s+alloc\.charge_transaction_id\s*=\s*v_charge_id\s*\)\s*\)"
        )
        assert re.search(max_subquery, sql_without_comments, re.IGNORECASE)

        # Monotonicity check uses non-strict comparison (>= allows same timestamp)
        assert re.search(r"NEW\.recorded_at\s*<\s*v_max_prior_recorded_at", sql_without_comments, re.IGNORECASE)

    def test_exact_numeric_and_no_floating_point(self, sql_without_comments: str):
        """Item 51: Exact NUMERIC arithmetic used; zero REAL/FLOAT/DOUBLE PRECISION."""
        assert re.search(r"0::numeric", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"\b(REAL|FLOAT|DOUBLE\s+PRECISION)\b", sql_without_comments, re.IGNORECASE)

    def test_phase_14f_rules_retained(self, sql_without_comments: str):
        """Item 52: Charge types, target types, and anti-reversal-of-reversal strictly preserved."""
        # Charge type allowlist
        assert re.search(r"IN\s*\(\s*'fee'\s*,\s*'tax_withholding'\s*\)", sql_without_comments, re.IGNORECASE)

        # Target type allowlist
        target_types = ['buy', 'sell', 'dividend', 'interest', 'cash_deposit', 'cash_withdrawal', 'fx_conversion']
        for tt in target_types:
            assert f"'{tt}'" in sql_without_comments, f"Missing target type '{tt}' in migration 019"

        # Anti-reversal-of-reversal check
        assert re.search(r"v_reversed_event_type\s*<>\s*'allocation'", sql_without_comments, re.IGNORECASE)

    def test_no_schema_expansion_and_no_table_alterations(self, sql_without_comments: str):
        """Item 53: Zero table column alterations or additions."""
        assert not re.search(r"ALTER\s+TABLE", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ADD\s+COLUMN", sql_without_comments, re.IGNORECASE)

    def test_no_rpc_created(self, sql_without_comments: str):
        """Item 54: Zero callable append/commit RPCs created."""
        assert not re.search(
            r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+public\.(append|commit|reverse|insert)_fee_tax",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_no_privilege_broadening(self, sql_without_comments: str):
        """Item 55: No privilege grants broadening client access."""
        assert not re.search(r"GRANT\s+INSERT[^\n;]*TO\s+(authenticated|anon|PUBLIC)", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"GRANT\s+(UPDATE|DELETE)", sql_without_comments, re.IGNORECASE)

    def test_no_tax_or_heuristic_logic(self, sql_without_comments: str):
        """Item 56: Zero tax calculations or heuristic matching."""
        prohibited_terms = [
            "tax_rate",
            "tax_year",
            "tax_liability",
            "withholding_rate",
            "jurisdiction",
            "treaty",
            "effective_date",
            "executed_at",
        ]
        for term in prohibited_terms:
            assert not re.search(rf"\b{term}\b", sql_without_comments, re.IGNORECASE)
