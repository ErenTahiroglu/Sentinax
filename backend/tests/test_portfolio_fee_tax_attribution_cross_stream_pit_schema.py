"""
backend/tests/test_portfolio_fee_tax_attribution_cross_stream_pit_schema.py
===========================================================================
Schema & DB Invariant Verification for Supabase Migration 021:
public.portfolio_fee_tax_attribution_events cross-stream PIT backdating &
attribution-reversal lock-domain hardening (Phase 14G.2).
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
MIGRATION_021_PATH = os.path.join(MIGRATIONS_DIR, "021_fee_tax_attribution_cross_stream_pit_hardening.sql")


@pytest.fixture(scope="module")
def migration_021_sql() -> str:
    assert os.path.exists(MIGRATION_021_PATH), f"Migration file missing: {MIGRATION_021_PATH}"
    with open(MIGRATION_021_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def sql_without_comments(migration_021_sql: str) -> str:
    no_line_comments = re.sub(r"--.*", "", migration_021_sql)
    no_block_comments = re.sub(r"/\*.*?\*/", "", no_line_comments, flags=re.DOTALL)
    return no_block_comments


class TestFeeTaxAttributionCrossStreamPITSchema:
    """Static schema & DB invariant verification for migration 021."""

    def test_migration_chain_intact(self):
        """Item 43: Migrations 018, 019, 020, 021 exist; migrations 001-020 untouched."""
        assert os.path.exists(MIGRATION_018_PATH)
        assert os.path.exists(MIGRATION_019_PATH)
        assert os.path.exists(MIGRATION_020_PATH)
        assert os.path.exists(MIGRATION_021_PATH)
        for i in range(1, 22):
            matches = [f for f in os.listdir(MIGRATIONS_DIR) if f.startswith(f"{i:03d}_")]
            assert len(matches) == 1, f"Expected exactly one migration for {i:03d}, got {matches}"

    def test_functions_replaced_and_triggers_bound(self, sql_without_comments: str):
        """Item 44: Migration 021 replaces both validate_fee_tax_attribution_event_integrity and lock_portfolio_transaction_reversal_target."""
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

    def test_attribution_reversal_target_resolution_and_locks(self, sql_without_comments: str):
        """Item 45, 46, 47, 48: REVERSAL resolves target_transaction_id and locks charge then target in order."""
        reversal_block_match = re.search(
            r"ELSIF\s+NEW\.event_type\s*=\s*'reversal'\s*THEN(.*)RETURN\s+NEW;",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert reversal_block_match is not None, "Could not find REVERSAL block in trigger function"
        reversal_block = reversal_block_match.group(1)

        # 1. Resolves target_transaction_id from referenced allocation
        assert re.search(
            r"SELECT[^;]*target_transaction_id[^;]*INTO[^;]*v_target_id[^;]*FROM\s+public\.portfolio_fee_tax_attribution_events",
            reversal_block,
            re.DOTALL | re.IGNORECASE,
        ), "REVERSAL block must resolve target_transaction_id"

        # 2. Charge lock appears before target lock
        charge_lock_pos = reversal_block.find("WHERE id = v_charge_id")
        target_lock_pos = reversal_block.find("WHERE id = v_target_id")
        assert charge_lock_pos != -1, "Missing charge lock in REVERSAL block"
        assert target_lock_pos != -1, "Missing target lock in REVERSAL block"
        assert charge_lock_pos < target_lock_pos, "Deterministic lock order violated: charge lock must precede target lock"

        # Both locks use FOR UPDATE
        assert re.search(
            r"WHERE\s+id\s*=\s*v_charge_id[^;]*FOR\s+UPDATE",
            reversal_block,
            re.DOTALL | re.IGNORECASE,
        )
        assert re.search(
            r"WHERE\s+id\s*=\s*v_target_id[^;]*FOR\s+UPDATE",
            reversal_block,
            re.DOTALL | re.IGNORECASE,
        )

    def test_ledger_reversal_lock_and_cross_stream_query(self, sql_without_comments: str):
        """Item 49, 50, 51, 52, 53, 54, 55: Ledger reversal locks target, queries related attributions, enforces strict recorded_at > max."""
        func_match = re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.lock_portfolio_transaction_reversal_target\(\)\s*RETURNS\s+TRIGGER\s+AS\s*\$\$(.*?)\$\$\s*LANGUAGE\s+plpgsql;",
            sql_without_comments,
            re.DOTALL | re.IGNORECASE,
        )
        assert func_match is not None
        func_body = func_match.group(1)

        # 1. Locks referenced ledger transaction with FOR UPDATE
        assert re.search(
            r"PERFORM\s+1\s+FROM\s+public\.portfolio_transactions\s+WHERE\s+id\s*=\s*NEW\.reverses_transaction_id\s+FOR\s+UPDATE;",
            func_body,
            re.IGNORECASE,
        )

        # 2. Considers both charge-side and target-side relations
        assert re.search(r"ae\.charge_transaction_id\s*=\s*NEW\.reverses_transaction_id", func_body, re.IGNORECASE)
        assert re.search(r"ae\.target_transaction_id\s*=\s*NEW\.reverses_transaction_id", func_body, re.IGNORECASE)

        # 3. Includes attribution-reversal history
        assert re.search(r"ae\.reverses_attribution_event_id\s+IN\s*\(\s*SELECT\s+alloc\.id", func_body, re.IGNORECASE)

        # 4. Strict cross-stream order rejection: NEW.recorded_at <= max
        assert re.search(
            r"v_max_related_attribution_recorded_at\s+IS\s+NOT\s+NULL\s+AND\s+NEW\.recorded_at\s*<=\s*v_max_related_attribution_recorded_at",
            func_body,
            re.IGNORECASE,
        )

        # 5. Non-reversal ledger path is a no-op (guard check)
        assert re.search(
            r"IF\s+NEW\.transaction_type\s*=\s*'reversal'\s+AND\s+NEW\.reverses_transaction_id\s+IS\s+NOT\s+NULL\s+THEN",
            func_body,
            re.IGNORECASE,
        )

    def test_closed_ledger_validator_not_replaced(self, sql_without_comments: str):
        """Item 56: Migration 021 must NOT replace validate_portfolio_transaction_integrity()."""
        assert not re.search(
            r"validate_portfolio_transaction_integrity",
            sql_without_comments,
            re.IGNORECASE,
        )

    def test_phase_14g_and_14g1_regressions_retained(self, sql_without_comments: str):
        """Item 57-61: Retains all Phase 14G/G.1 capacity, duplicate-pair, active-PIT, causality, and monotonic rules."""
        # Active charge & target PIT checks in ALLOCATION
        assert re.search(r"rev\.reverses_transaction_id\s*=\s*v_charge_id\s+AND\s+rev\.recorded_at\s*<=\s*NEW\.recorded_at", sql_without_comments, re.IGNORECASE)
        assert re.search(r"rev\.reverses_transaction_id\s*=\s*v_target_id\s+AND\s+rev\.recorded_at\s*<=\s*NEW\.recorded_at", sql_without_comments, re.IGNORECASE)

        # Capacity checks
        assert re.search(r"NEW\.allocated_amount\s*>\s*v_charge_cash_amount", sql_without_comments, re.IGNORECASE)
        assert re.search(r"v_active_allocated_total\s*\+\s*NEW\.allocated_amount\s*>\s*v_charge_cash_amount", sql_without_comments, re.IGNORECASE)

        # Duplicate pair rejection
        assert re.search(r"ae\.charge_transaction_id\s*=\s*v_charge_id\s+AND\s+ae\.target_transaction_id\s*=\s*v_target_id", sql_without_comments, re.IGNORECASE)

        # Causality & Monotonic history
        assert re.search(r"NEW\.recorded_at\s*<\s*v_charge_recorded_at", sql_without_comments, re.IGNORECASE)
        assert re.search(r"NEW\.recorded_at\s*<\s*v_target_recorded_at", sql_without_comments, re.IGNORECASE)
        assert re.search(r"NEW\.recorded_at\s*<\s*v_max_prior_recorded_at", sql_without_comments, re.IGNORECASE)

        # Exact type allowlists
        assert re.search(r"IN\s*\(\s*'fee'\s*,\s*'tax_withholding'\s*\)", sql_without_comments, re.IGNORECASE)
        target_types = ['buy', 'sell', 'dividend', 'interest', 'cash_deposit', 'cash_withdrawal', 'fx_conversion']
        for tt in target_types:
            assert f"'{tt}'" in sql_without_comments, f"Missing target type '{tt}' in migration 021"

    def test_no_schema_mutation_or_privilege_changes(self, sql_without_comments: str):
        """Item 63, 64, 65, 66, 67: Zero DDL alterations, privilege changes, RPCs, tax rules, or heuristics."""
        assert not re.search(r"ALTER\s+TABLE", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"ADD\s+COLUMN", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"DROP\s+COLUMN", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"\bGRANT\b", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"\bREVOKE\b", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"CREATE\s+POLICY", sql_without_comments, re.IGNORECASE)
        assert not re.search(r"DROP\s+POLICY", sql_without_comments, re.IGNORECASE)
        assert not re.search(
            r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+public\.(append|commit|reverse|insert)_",
            sql_without_comments,
            re.IGNORECASE,
        )
        prohibited = ["tax_rate", "tax_year", "tax_liability", "cost_basis", "effective_date", "executed_at"]
        for p in prohibited:
            assert not re.search(rf"\b{p}\b", sql_without_comments, re.IGNORECASE)
