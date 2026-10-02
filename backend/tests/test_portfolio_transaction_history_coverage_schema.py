"""
backend/tests/test_portfolio_transaction_history_coverage_schema.py
===================================================================
Phase 26C2B2B1: migration 026 defines one atomic persistent-history coverage snapshot RPC for portfolio_transactions: a SHARE-locked, single-aggregate manifest of
(id, recorded_at epoch micros, economic fingerprint) for one explicit owner and portfolio through a recorded_at cutoff. SQL-text structural tests only (no
database is executed and no runtime lock behavior is claimed); Supabase Preview is the migration-execution check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
M026 = MIGRATIONS / "026_portfolio_transaction_history_coverage_snapshot.sql"
FUNCTION = "get_portfolio_transaction_history_coverage_snapshot"
INDEX = "idx_portfolio_transactions_history_coverage"


def strip_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", " ", text)


@pytest.fixture(scope="module")
def raw() -> str:
    assert M026.is_file(), "migration 026 is missing"
    return strip_comments(M026.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def flat(raw: str) -> str:
    return re.sub(r"\s+", " ", raw)


@pytest.fixture(scope="module")
def body(raw: str) -> str:
    match = re.search(r"AS\s+\$\$(.*)\$\$\s*;", raw, re.DOTALL)
    assert match, "function body not found"
    return match.group(1)


@pytest.fixture(scope="module")
def code(body: str) -> str:
    """Function body with string literals removed (so error messages never trigger structural checks), whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"'(?:[^']|'')*'", "''", body))


def test_migration_numbering_is_exact_and_local() -> None:
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert [n for n in names if n.startswith("026_")] == ["026_portfolio_transaction_history_coverage_snapshot.sql"]
    index = names.index("026_portfolio_transaction_history_coverage_snapshot.sql")
    assert index > 0 and names[index - 1] == "025_portfolio_transaction_write_surface_hardening.sql"          # adjacency only, a later 027 is allowed


def test_exactly_one_function_with_the_exact_signature_and_return_columns(flat: str) -> None:
    assert len(re.findall(r"CREATE OR REPLACE FUNCTION", flat, re.IGNORECASE)) == 1
    signature = re.search(rf"CREATE OR REPLACE FUNCTION public\.{FUNCTION}\((.*?)\) RETURNS TABLE \((.*?)\) LANGUAGE", flat, re.IGNORECASE)
    assert signature, "exact function not found"
    params = [p.strip() for p in signature.group(1).split(",")]
    assert params == ["p_owner_id UUID", "p_portfolio_id UUID", "p_as_of_recorded_at TIMESTAMPTZ"]
    assert "DEFAULT" not in signature.group(1).upper()
    columns = [c.strip() for c in signature.group(2).split(",")]
    assert columns == ["owner_id UUID", "portfolio_id UUID", "as_of_recorded_at TIMESTAMPTZ", "observed_at TIMESTAMPTZ", "transaction_count BIGINT",
                       "transaction_ids UUID[]", "recorded_at_epoch_micros BIGINT[]", "economic_fingerprints TEXT[]"]


def test_security_definer_volatile_plpgsql_with_secure_search_path(flat: str, code: str) -> None:
    header = re.search(r"RETURNS TABLE \(.*?\) (LANGUAGE.*?) AS \$\$", flat, re.IGNORECASE).group(1)
    assert re.search(r"LANGUAGE plpgsql", header, re.IGNORECASE) and re.search(r"\bVOLATILE\b", header, re.IGNORECASE)
    assert re.search(r"SECURITY DEFINER", header, re.IGNORECASE) and not re.search(r"SECURITY INVOKER|\bSTABLE\b|\bIMMUTABLE\b", header, re.IGNORECASE)
    assert re.search(r"SET search_path = pg_catalog, pg_temp", header, re.IGNORECASE)
    assert not re.search(r"\bEXECUTE\b", code, re.IGNORECASE) and not re.search(r"\bFORMAT\s*\(", code, re.IGNORECASE)          # no dynamic SQL
    for relation in re.findall(r"\b(?:FROM|JOIN|LOCK TABLE)\s+([A-Za-z_][\w.]*)", code, re.IGNORECASE):
        if relation.lower() != "t.recorded_at":                                                                # the FROM inside extract(epoch FROM t.recorded_at)
            assert relation.lower() in {"public.portfolio_transactions", "public.portfolios"}, relation        # fully schema-qualified, no caller-named relation


def test_execute_permissions_are_service_role_only(flat: str) -> None:
    signature = r"public\." + FUNCTION + r"\(UUID, UUID, TIMESTAMPTZ\)"
    for role in ("PUBLIC", "anon", "authenticated"):
        assert re.search(rf"REVOKE EXECUTE ON FUNCTION {signature} FROM {role};", flat, re.IGNORECASE), role
    assert re.search(rf"GRANT EXECUTE ON FUNCTION {signature} TO service_role;", flat, re.IGNORECASE)
    grants = re.findall(r"GRANT [^;]+;", flat, re.IGNORECASE)
    assert len(grants) == 1 and "service_role" in grants[0]
    assert flat.index("CREATE OR REPLACE FUNCTION") < flat.index("REVOKE EXECUTE")
    assert not re.search(r"ALTER DEFAULT PRIVILEGES", flat, re.IGNORECASE)


def test_inputs_are_validated_before_the_lock_and_nothing_reads_the_table_before_it(code: str) -> None:
    lock = code.index("LOCK TABLE public.portfolio_transactions IN SHARE MODE")
    for parameter in ("p_owner_id", "p_portfolio_id", "p_as_of_recorded_at"):
        assert re.search(rf"{parameter} IS NULL", code), parameter
    assert code.index("p_owner_id IS NULL") < lock and code.index("p_as_of_recorded_at IS NULL") < lock
    assert len(re.findall(r"LOCK TABLE", code, re.IGNORECASE)) == 1 and not re.search(r"ACCESS SHARE|pg_advisory|FOR SHARE|FOR UPDATE", code, re.IGNORECASE)
    for match in re.finditer(r"FROM public\.portfolio_transactions", code):
        assert match.start() > lock


def test_lock_precedes_observed_at_and_the_manifest_select(code: str) -> None:
    lock = code.index("LOCK TABLE public.portfolio_transactions IN SHARE MODE")
    observed = code.index("v_observed_at := pg_catalog.clock_timestamp()")
    assert lock < observed and len(re.findall(r"clock_timestamp", code)) == 1 and not re.search(r"\bnow\s*\(|transaction_timestamp|statement_timestamp", code)
    future = code.index("p_as_of_recorded_at > v_observed_at")
    exists = code.index("FROM public.portfolios")
    fingerprint = code.index("economic_fingerprint !~")
    manifest = code.index("array_agg")
    assert observed < future < exists < fingerprint < manifest
    assert "RAISE EXCEPTION" in code[future:future + 120]
    assert "p_owner_id" in code[exists:exists + 200] and "p_portfolio_id" in code[exists:exists + 200]
    assert not re.search(r"auth\.uid|current_user|session_user", code)


def test_exact_filter_and_no_extra_predicates_or_pagination(code: str) -> None:
    selects = re.findall(r"FROM public\.portfolio_transactions t WHERE (.*?)(?: INTO|;| AND t\.economic_fingerprint|\))", code)
    assert selects
    for predicate in re.findall(r"WHERE (t\.owner_id = p_owner_id AND t\.portfolio_id = p_portfolio_id AND t\.recorded_at <= p_as_of_recorded_at)", code):
        assert predicate
    assert len(re.findall(r"t\.owner_id = p_owner_id AND t\.portfolio_id = p_portfolio_id AND t\.recorded_at <= p_as_of_recorded_at", code)) == 2
    assert not re.search(r"effective_date|executed_at|account_id|transaction_type|\bLIMIT\b|\bOFFSET\b|\bFETCH\b|\bLOOP\b|\bFOR\b\s+\w+\s+IN|cursor", code, re.IGNORECASE)


def test_manifest_is_one_aggregate_with_identical_order_and_typed_empty_arrays(code: str) -> None:
    assert len(re.findall(r"\barray_agg\(", code)) == 3 and len(re.findall(r"\bcount\(", code)) == 2        # one validation count, one manifest count
    orders = re.findall(r"ORDER BY (t\.recorded_at ASC, t\.id ASC)\)", code)
    assert len(orders) == 3
    for typed in ("ARRAY[]::uuid[]", "ARRAY[]::bigint[]", "ARRAY[]::text[]"):
        assert code.count(typed) == 1, typed
    assert code.count("COALESCE") == 3
    assert "(EXTRACT(EPOCH FROM t.recorded_at) * 1000000)::bigint" in code                              # EXTRACT is SQL syntax: it cannot be schema-qualified
    assert "pg_catalog.extract" not in code and "date_part" not in code                                  # date_part would return a float
    assert "t.economic_fingerprint::text" in code and "t.id" in code
    manifest = code[code.index("count(*)::bigint"):]
    assert manifest.count("FROM public.portfolio_transactions t") == 1                                  # count and all three arrays come from one relation
    assert not re.search(r"effective_date ASC|executed_at ASC|ORDER BY t\.id\b(?!\s+ASC)", code)


def test_fingerprint_syntax_is_validated_without_repair(code: str) -> None:
    assert re.search(r"t\.economic_fingerprint !~ ''", code) and "'^[0-9a-f]{64}$'" in re.sub(r"\s+", " ", M026.read_text(encoding="utf-8"))
    assert not re.search(r"\blower\s*\(|\bupper\s*\(|\btranslate\s*\(|\breplace\s*\(", code, re.IGNORECASE)
    assert code.count("RAISE EXCEPTION") == 4                                                           # null input, future cutoff, unknown portfolio, bad fingerprint


def test_return_row_contract(code: str) -> None:
    assert re.search(r"RETURN QUERY SELECT p_owner_id, p_portfolio_id, p_as_of_recorded_at, v_observed_at, v_count, v_ids, v_micros, v_fingerprints", code)


def test_no_dml_hash_or_pgcrypto_in_the_function(code: str, flat: str) -> None:
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE)\b", code, re.IGNORECASE)
    assert not re.search(r"pgcrypto|\bdigest\s*\(|sha256|\bmd5\s*\(|history_sha256|manifest_sha256", flat, re.IGNORECASE)
    assert not re.search(r"CREATE (OR REPLACE )?(TRIGGER|TABLE)|ALTER TABLE|DROP ", flat, re.IGNORECASE)


def test_exactly_one_dedicated_coverage_index(flat: str) -> None:
    indexes = re.findall(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+) ON (\S+) \((.*?)\)", flat, re.IGNORECASE)
    assert indexes == [(INDEX, "public.portfolio_transactions", "owner_id, portfolio_id, recorded_at, id")]
    assert not re.search(r"idx_portfolio_transactions_audit_order", flat)                               # the existing audit-order index is untouched


def test_documentation_states_the_boundaries() -> None:
    doc = (ROOT / "docs" / "PORTFOLIO_TRANSACTION_HISTORY_COVERAGE_SNAPSHOT.md").read_text(encoding="utf-8")
    for needle in ("pagination", "SHARE", "SECURITY DEFINER", "service-role", "search_path", "observed_at", "recorded_at", "economic fingerprint", "future cutoff",
                   "empty", "not a permanent", "trusted", "C2B2B2", "concurrent"):
        assert needle in doc, needle
