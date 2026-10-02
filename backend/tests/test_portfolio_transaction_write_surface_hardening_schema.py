"""
backend/tests/test_portfolio_transaction_write_surface_hardening_schema.py
==========================================================================
Phase 26C2B2A: migration 025 hardens the write surface of the append-only ledger `public.portfolio_transactions`. The legacy authenticated INSERT policy is
dropped and the table privileges are reset to an explicit final matrix: PUBLIC none, anon none, authenticated SELECT only, service_role SELECT and INSERT only
(never UPDATE, DELETE or TRUNCATE). SQL-text structural tests (no database is executed); Supabase Preview is the real migration-execution check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
M025 = MIGRATIONS / "025_portfolio_transaction_write_surface_hardening.sql"
M011 = MIGRATIONS / "011_portfolio_ledger_persistence.sql"
TABLE = "public.portfolio_transactions"
INSERT_POLICY = "Users can insert own portfolio transactions"
SELECT_POLICY = "Users can view own portfolio transactions"


def stripped(path: Path) -> str:
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", " ", path.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def sql() -> str:
    assert M025.is_file(), "migration 025 is missing"
    return stripped(M025)


def statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def final_matrix(sql: str) -> dict[str, set[str]]:
    matrix: dict[str, set[str]] = {"PUBLIC": set(), "anon": set(), "authenticated": set(), "service_role": set()}
    universe = {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    pattern = re.compile(rf"^(REVOKE|GRANT) (.+?) ON (?:TABLE )?{re.escape(TABLE)} (?:FROM|TO) (\w+)$", re.IGNORECASE)
    # start from the worst case: every privilege present for every role (platform default privileges), then replay the migration in order
    for role in matrix:
        matrix[role] = set(universe)
    for statement in statements(sql):
        match = pattern.match(statement)
        if not match:
            continue
        verb, privileges, role = match.group(1).upper(), match.group(2).upper(), match.group(3)
        role = role if role.upper() == "PUBLIC" else role.lower()
        role = "PUBLIC" if role.upper() == "PUBLIC" else role
        parsed = universe if re.fullmatch(r"(ALL)( PRIVILEGES)?", privileges) else {p.strip() for p in privileges.split(",")}
        assert parsed <= universe, privileges
        matrix[role] = (matrix[role] - parsed) if verb == "REVOKE" else (matrix[role] | parsed)
    return matrix


def test_migration_numbering_is_exact() -> None:
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert "024_private_scheduler_run_persistence.sql" in names
    assert [n for n in names if n.startswith("025_")] == ["025_portfolio_transaction_write_surface_hardening.sql"]
    index_025 = names.index("025_portfolio_transaction_write_surface_hardening.sql")                       # local adjacency only: a later 026 must not break this
    assert index_025 > 0 and names[index_025 - 1] == "024_private_scheduler_run_persistence.sql"


def test_legacy_authenticated_insert_policy_is_dropped_and_select_policy_is_kept(sql: str) -> None:
    assert f'DROP POLICY IF EXISTS "{INSERT_POLICY}" ON {TABLE}' in sql
    assert SELECT_POLICY not in sql                                                       # not dropped, recreated or renamed
    assert not re.search(r"CREATE POLICY", sql, re.IGNORECASE) and not re.search(r"ALTER POLICY", sql, re.IGNORECASE)
    assert not re.search(r"DROP POLICY IF EXISTS \"[^\"]*\" ON (?!public\.portfolio_transactions)", sql)
    assert len(re.findall(r"DROP POLICY", sql, re.IGNORECASE)) == 1
    original = stripped(M011)
    assert f'CREATE POLICY "{SELECT_POLICY}" ON {TABLE} FOR SELECT TO authenticated' in original
    assert f'CREATE POLICY "{INSERT_POLICY}" ON {TABLE} FOR INSERT TO authenticated' in original      # the bypass this migration removes


def test_final_privilege_matrix() -> None:
    sql = stripped(M025)
    matrix = final_matrix(sql)
    assert matrix == {"PUBLIC": set(), "anon": set(), "authenticated": {"SELECT"}, "service_role": {"SELECT", "INSERT"}}


def test_revokes_precede_grants_and_cover_every_role(sql: str) -> None:
    verbs = [(m.group(1).upper(), m.group(3)) for m in (re.match(rf"^(REVOKE|GRANT) (.+?) ON (?:TABLE )?{re.escape(TABLE)} (?:FROM|TO) (\w+)$", s, re.IGNORECASE)
                                                       for s in statements(sql)) if m]
    revoked = [role for verb, role in verbs if verb == "REVOKE"]
    assert {r.lower() for r in revoked} == {"public", "anon", "authenticated", "service_role"}
    first_grant = next(i for i, (verb, _) in enumerate(verbs) if verb == "GRANT")
    assert all(verb == "REVOKE" for verb, _ in verbs[:first_grant]) and len(revoked) == 4
    assert re.search(rf"GRANT SELECT ON TABLE {re.escape(TABLE)} TO authenticated", sql)
    assert re.search(rf"GRANT SELECT, INSERT ON TABLE {re.escape(TABLE)} TO service_role", sql)


def test_no_update_delete_truncate_or_all_grant_to_any_role(sql: str) -> None:
    for statement in statements(sql):
        if statement.upper().startswith("GRANT"):
            assert not re.search(r"\b(UPDATE|DELETE|TRUNCATE|ALL|REFERENCES|TRIGGER)\b", statement, re.IGNORECASE), statement
    matrix = final_matrix(sql)
    for role, privileges in matrix.items():
        assert not privileges & {"UPDATE", "DELETE", "TRUNCATE"}, role
    assert not re.search(r"\bGRANT\b[^;]*\bTO\s+(PUBLIC|anon)\b", sql, re.IGNORECASE)


def test_migration_is_narrow_and_changes_no_other_contract(sql: str) -> None:
    assert not re.search(r"CREATE (OR REPLACE )?(FUNCTION|TRIGGER|TABLE|INDEX)|ALTER TABLE|DROP TRIGGER|DROP FUNCTION|recorded_at|prevent_portfolio_transaction_tamper|now\(\)",
                         sql, re.IGNORECASE)
    for statement in statements(sql):
        assert statement.upper().startswith(("DROP POLICY", "REVOKE", "GRANT")), statement
        assert "commit_portfolio_import_claim" not in statement
    assert "portfolio_import" not in sql and TABLE in sql


def test_existing_append_only_trigger_justifies_this_as_additive_hardening() -> None:
    original = stripped(M011)
    assert f"CREATE TRIGGER trg_prevent_portfolio_transaction_tamper BEFORE UPDATE OR DELETE ON {TABLE}" in original
    assert "Immutability violation: portfolio_transactions records cannot be updated" in original
    assert "ALTER TABLE public.portfolio_transactions ENABLE ROW LEVEL SECURITY" in original
    assert "BEFORE TRUNCATE" not in original.upper()                                    # the row trigger never covered TRUNCATE


def test_canonical_repository_append_path_still_inserts_with_service_role_privilege() -> None:
    source = (ROOT / "backend" / "engine" / "private" / "portfolio" / "repository.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    append = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "append_transaction")
    segment = ast.get_source_segment(source, append) or ""
    assert re.search(r'\.table\("portfolio_transactions"\)\s*\.insert\(', segment)
    for node in ast.walk(tree):                                                         # the repository never updates or deletes the ledger
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"update", "delete", "upsert"}:
            inner = ast.get_source_segment(source, node.func.value) or ""
            assert "portfolio_transactions" not in inner
    assert "SUPABASE_SERVICE_ROLE_KEY" in (ROOT / "backend" / "api" / "dependencies.py").read_text(encoding="utf-8")      # current trusted backend client evidence


def test_no_frontend_or_browser_code_writes_the_ledger_directly() -> None:
    offenders = []
    for folder in ("frontend", "src", "frontend-vanilla-backup", "oracle_worker"):
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".js", ".ts", ".tsx", ".jsx", ".html", ".mjs"} and "node_modules" not in path.parts:
                if "portfolio_transactions" in path.read_text(encoding="utf-8", errors="ignore"):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_documentation_states_the_boundaries() -> None:
    doc = (ROOT / "docs" / "PORTFOLIO_TRANSACTION_WRITE_SURFACE_HARDENING.md").read_text(encoding="utf-8")
    for needle in ("recorded_at", "append-only", "authenticated", "TRUNCATE", "service_role", "SELECT", "INSERT", "does not prove", "C2B2B", "SUPABASE_SERVICE_ROLE_KEY"):
        assert needle in doc, needle
