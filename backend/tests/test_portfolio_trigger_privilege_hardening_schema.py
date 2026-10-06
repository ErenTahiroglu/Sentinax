"""
backend/tests/test_portfolio_trigger_privilege_hardening_schema.py
===================================================================
Phase 27 FIX A: migration 027 re-declares the two Phase 14G.2 cross-stream trigger functions as SECURITY DEFINER with a pinned search_path so their
SELECT ... FOR UPDATE row locks work under the migration-025 write surface (service_role: SELECT + INSERT only). SQL-text structural tests only and
supplemental: the authoritative regression is the real PostgreSQL test_portfolio_trigger_privilege_postgres.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
M021 = MIGRATIONS / "021_fee_tax_attribution_cross_stream_pit_hardening.sql"
M025 = MIGRATIONS / "025_portfolio_transaction_write_surface_hardening.sql"
M027 = MIGRATIONS / "027_portfolio_trigger_privilege_hardening.sql"
FUNCTIONS = (
    "lock_portfolio_transaction_reversal_target",
    "validate_fee_tax_attribution_event_integrity",
)
PIN = "SET search_path = pg_catalog, pg_temp"


def code(path: Path) -> str:
    return re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


def collapsed(path: Path) -> str:
    return re.sub(r"\s+", " ", code(path))


def function_block(text: str, name: str) -> str:
    m = re.search(
        rf"CREATE OR REPLACE FUNCTION public\.{name}\(\)\s+RETURNS TRIGGER AS \$\$(.*?)\$\$ LANGUAGE plpgsql([^;]*);",
        text,
        re.S,
    )
    assert m, name
    return m.group(0)


@pytest.fixture(scope="module")
def sql() -> str:
    assert M027.is_file(), "migration 027 is missing"
    return code(M027)


def test_both_functions_are_security_definer_with_the_pinned_search_path(sql):
    for name in FUNCTIONS:
        tail = function_block(sql, name).rsplit("$$", 1)[1]
        assert "SECURITY DEFINER" in tail
        assert PIN in tail
        assert tail.count("search_path") == 1
    assert "public" not in re.findall(r"search_path\s*=\s*([^;]*);", sql)[0]


def test_function_bodies_are_identical_to_the_closed_migration_021_bodies(sql):
    for name in FUNCTIONS:
        old = function_block(code(M021), name).rsplit("$$", 1)[0]
        new = function_block(sql, name).rsplit("$$", 1)[0]
        assert new == old, f"{name}: only execution security may change"


def test_row_locks_are_not_weakened(sql):
    assert function_block(sql, FUNCTIONS[0]).count("FOR UPDATE") == 1
    assert function_block(sql, FUNCTIONS[1]).count("FOR UPDATE") >= 4


def test_application_relations_are_schema_qualified_and_no_dynamic_sql(sql):
    flat = re.sub(r"\s+", " ", sql)
    for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE|INTO) ([a-z_][a-z0-9_.]*)", flat):
        ref = m.group(1)
        assert ref.startswith("public.") or ref.startswith("v_") or ref == "strict", ref
    assert not re.search(r"\bEXECUTE\s+(?!FUNCTION)", flat)
    assert "format(" not in flat.lower()


def test_no_grant_of_any_kind_and_execute_is_revoked_from_every_caller(sql):
    flat = re.sub(r"\s+", " ", sql)
    assert not re.search(r"\bGRANT\b", flat, re.I)
    assert not re.search(r"\bALTER\s+(TABLE|DEFAULT PRIVILEGES|ROLE|FUNCTION)\b", flat, re.I)
    for name in FUNCTIONS:
        assert f"REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC, anon, authenticated, service_role;" in flat


def test_no_table_ddl_trigger_rebinding_or_data_change(sql):
    flat = re.sub(r"\s+", " ", sql)
    for forbidden in ("CREATE TABLE", "DROP TABLE", "ALTER TABLE", "CREATE TRIGGER", "DROP TRIGGER", "INSERT INTO", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in flat.upper(), forbidden
    assert not re.search(r"\bUPDATE public\.", flat)


def test_migration_025_still_leaves_service_role_with_select_and_insert_only():
    flat = collapsed(M025)
    assert "GRANT SELECT, INSERT ON TABLE public.portfolio_transactions TO service_role" in flat
    assert "REVOKE ALL PRIVILEGES ON TABLE public.portfolio_transactions FROM service_role" in flat
    assert not re.search(r"GRANT[^;]*\bUPDATE\b", flat, re.I)
    assert not re.search(r"GRANT[^;]*\bUPDATE\b", collapsed(M027), re.I)


def test_trigger_bindings_remain_defined_by_migration_021_only():
    flat = collapsed(M021)
    assert "CREATE TRIGGER trg_lock_portfolio_transaction_reversal_target BEFORE INSERT ON public.portfolio_transactions" in flat
    assert (
        "CREATE TRIGGER trg_validate_fee_tax_attribution_event_integrity BEFORE INSERT ON public.portfolio_fee_tax_attribution_events"
        in flat
    )
    assert "CREATE TRIGGER" not in collapsed(M027)
