"""
Phase 28B-0 boundary guard. The learning bounded context is evidence-contract only: pure (no clock, entropy, I/O, network, float), importing only an explicit allowlist, never
importing economic-decision authorities, and never imported by them. Also pins permanent CI membership of the whole Phase 28B-0 suite. Test-only: no production module.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.tests.invariants import static_guards as sg

ROOT = Path(__file__).resolve().parents[2]
LEARNING = ROOT / "backend" / "engine" / "learning"
MODULES = ["__init__", "_checks", "temporal_provenance", "source_authority", "universe_coverage", "observation_status", "preregistration", "evidence_binding", "adapters"]
STORE = ROOT / "backend" / "engine" / "learning_store"
STORE_MODULES = ["__init__", "evidence_binding_repository"]
CI_TESTS = [
    "test_learning_provenance", "test_learning_source_authority", "test_learning_universe_coverage", "test_learning_observation_status",
    "test_learning_preregistration", "test_learning_adapters", "test_learning_evidence_binding", "test_learning_store", "test_learning_boundary",
]
PG_TESTS = ["test_learning_persistence_postgres"]
STEP = "Phase 28B-0 learning evidence contracts"
PG_STEP = "Phase 28B-1A learning evidence PostgreSQL persistence"

STDLIB_ALLOWED = {"__future__", "dataclasses", "datetime", "decimal", "enum", "hashlib", "json", "re", "typing", "collections", "math", "uuid"}
STORE_PRIVATE_ALLOWED = {"backend.engine.private.storage_models"}
DRIVER_ROOTS = {"psycopg", "psycopg2", "asyncpg", "postgrest", "supabase", "sqlalchemy", "httpx", "requests", "aiohttp", "urllib", "socket", "os", "pathlib", "subprocess", "time", "asyncio", "threading", "random", "secrets"}
PRIVATE_ALLOWED = {"backend.engine.private.storage_models", "backend.engine.private.market_data.tefas_models"}
FORBIDDEN_PRIVATE_FRAGMENTS = (
    "portfolio", "allocation_rebalance", "backtest_", "scheduler_", "game_changer", "providers", "orchestrator", "identity", "user_view", "trade", "order", "ledger",
)
BANNED_DEPENDENCY_ROOTS = {
    "numpy", "pandas", "scipy", "sklearn", "torch", "tensorflow", "xgboost", "lightgbm", "statsmodels", "requests", "httpx", "aiohttp", "urllib", "socket",
    "subprocess", "os", "pathlib", "shutil", "sqlalchemy", "supabase", "redis", "sqlite3", "random", "secrets", "time", "asyncio", "threading",
}


def sources() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(LEARNING.glob("*.py"))}


def imported_modules(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed in the learning context"
            found.add(node.module or "")
    return found


def test_module_inventory_is_exactly_the_reviewed_set() -> None:
    assert sorted(sources()) == sorted(MODULES)          # growth forces a deliberate boundary review


def test_imports_are_allowlisted() -> None:
    for name, src in sources().items():
        for mod in imported_modules(src):
            root = mod.split(".")[0]
            assert root not in BANNED_DEPENDENCY_ROOTS, (name, mod)
            if root == "backend":
                assert mod.startswith("backend.engine.learning") or mod in PRIVATE_ALLOWED, (name, mod)
            else:
                assert mod in STDLIB_ALLOWED or root in STDLIB_ALLOWED, (name, mod)


def test_only_the_adapter_module_touches_private_code() -> None:
    for name, src in sources().items():
        private = {m for m in imported_modules(src) if m.startswith("backend.engine.private")}
        if name != "adapters":
            assert not private, (name, private)
        for m in private:
            assert not any(f in m for f in FORBIDDEN_PRIVATE_FRAGMENTS), (name, m)


def test_importing_learning_pulls_in_no_economic_decision_authority() -> None:
    code = (
        f"import json, sys\nsys.path.insert(0, {str(ROOT)!r})\n"
        "import backend.engine.learning.adapters, backend.engine.learning.preregistration, backend.engine.learning.universe_coverage, backend.engine.learning.evidence_binding, backend.engine.learning_store.evidence_binding_repository\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('backend.engine.private') or m.startswith('backend.services') or m.startswith('backend.api'))))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    loaded = json.loads(out.strip().splitlines()[-1])
    bad = [m for m in loaded if any(f in m for f in FORBIDDEN_PRIVATE_FRAGMENTS if f not in {"identity"}) or m.startswith(("backend.services", "backend.api"))]
    assert not bad, bad


@pytest.mark.parametrize("scan", [sg.scan_g1, sg.scan_g2, sg.scan_g4, sg.scan_g5])
def test_learning_sources_pass_clock_entropy_float_and_missing_zero_guards(scan) -> None:
    for name, src in sources().items():
        assert scan(src, f"backend/engine/learning/{name}.py") == [], (name, scan.__name__)


def test_no_io_calls_or_ambient_state_in_learning_sources() -> None:
    for name, src in sources().items():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "print", "input", "exec", "eval", "float", "__import__"}, (name, node.func.id)
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                pytest.fail(f"{name}: global/nonlocal state")


def test_learning_exposes_no_economic_authority_or_model_vocabulary() -> None:
    banned = {"buy", "sell", "rebalance", "recommend", "recommendation", "train", "predict", "fit", "weight", "sharpe", "trade", "order"}
    for name, src in sources().items():
        idents: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                idents.add(node.name)
            elif isinstance(node, ast.arg):
                idents.add(node.arg)
        parts = {w for i in idents for w in i.lower().split("_")}
        assert not (parts & banned), (name, parts & banned)


def test_no_module_outside_learning_imports_it() -> None:
    offenders: list[str] = []
    for base in ("backend",):
        for p in (ROOT / base).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith(("backend/engine/learning/", "backend/engine/learning_store/", "backend/tests/")) or ".venv" in rel:
                continue
            if "backend.engine.learning" in p.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(rel)
    assert offenders == []


def test_private_engine_does_not_reference_learning_by_path() -> None:
    for p in (ROOT / "backend" / "engine" / "private").rglob("*.py"):
        assert "engine.learning" not in p.read_text(encoding="utf-8", errors="ignore"), p.name


def test_only_learning_tests_import_learning_besides_boundary_exceptions() -> None:
    for p in (ROOT / "backend" / "tests").glob("*.py"):
        if "backend.engine.learning" in p.read_text(encoding="utf-8"):
            assert p.stem in CI_TESTS or p.stem in PG_TESTS or p.stem == "learning_support", p.name


def store_sources() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(STORE.glob("*.py"))}


def test_store_module_inventory_is_exactly_the_reviewed_set() -> None:
    assert sorted(store_sources()) == sorted(STORE_MODULES)


def test_store_imports_only_learning_contracts_and_the_private_storage_type() -> None:
    for name, src in store_sources().items():
        for mod in imported_modules(src):
            root = mod.split(".")[0]
            assert root not in DRIVER_ROOTS, (name, mod)             # no DB driver, HTTP client, clock, filesystem or process access: transports are injected
            if root == "backend":
                assert mod.startswith(("backend.engine.learning.", "backend.engine.learning_store")) or mod in STORE_PRIVATE_ALLOWED, (name, mod)
            else:
                assert mod in STDLIB_ALLOWED or root in STDLIB_ALLOWED, (name, mod)


def test_pure_learning_package_never_imports_the_store_or_a_database_driver() -> None:
    for name, src in sources().items():
        for mod in imported_modules(src):
            assert not mod.startswith("backend.engine.learning_store"), (name, mod)
            assert mod.split(".")[0] not in DRIVER_ROOTS, (name, mod)


def test_store_sources_pass_clock_entropy_float_and_missing_zero_guards() -> None:
    for name, src in store_sources().items():
        for scan in (sg.scan_g1, sg.scan_g2, sg.scan_g4, sg.scan_g5):
            assert scan(src, f"backend/engine/learning_store/{name}.py") == [], (name, scan.__name__)


def test_store_issues_no_sql_and_never_names_the_raw_snapshot_table() -> None:
    for name, src in store_sources().items():
        for token in ("raw_provider_snapshots", "INSERT INTO", "DELETE FROM", "TRUNCATE", "UPDATE public"):
            assert token not in src, (name, token)


def test_migration_030_creates_one_table_no_policy_no_security_definer_and_no_api_role_grant() -> None:
    sql = (ROOT / "supabase" / "migrations" / "030_learning_evidence_bindings.sql").read_text(encoding="utf-8")
    assert "SECURITY DEFINER" not in sql.upper() and "CREATE POLICY" not in sql.upper()
    assert sql.count("CREATE TABLE") == 1 and "ALTER TABLE public.raw_provider_snapshots" not in sql
    grants = [line.strip() for line in sql.splitlines() if line.strip().upper().startswith("GRANT")]
    assert grants and all(line.rstrip(";").endswith("TO service_role") for line in grants), grants


def test_postgres_ci_step_is_permanent_runs_against_real_postgres_and_is_not_socket_disabled() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"- name: {PG_STEP}" in ci
    block = ci.split(f"- name: {PG_STEP}", 1)[1].split("\n    - name:", 1)[0].split("\n    #", 1)[0]
    assert "SENTINAX_TEST_POSTGRES_URL" in block and "psycopg[binary]==3.3.6" in block and "--disable-socket" not in block
    for t in PG_TESTS:
        assert f"backend/tests/{t}.py" in block, t


def test_ci_gate_is_permanent_and_lists_every_learning_test() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"- name: {STEP}" in ci
    block = ci.split(f"- name: {STEP}", 1)[1].split("\n    - name:", 1)[0].split("\n    #", 1)[0]
    for t in CI_TESTS:
        assert f"backend/tests/{t}.py" in block, t
    assert "--disable-socket" in block
    for earlier in ("Static invariant guards", "Phase 24 private scheduler correctness", "Phase 26 backtest architecture correctness",
                    "Phase 26 PostgreSQL history-coverage concurrency", "Phase 27 PostgreSQL Supabase security advisor regression"):
        assert f"- name: {earlier}" in ci, earlier


def test_static_baseline_file_is_not_a_learning_concern() -> None:
    doc = json.loads((ROOT / "backend" / "tests" / "invariants" / "baseline.json").read_text(encoding="utf-8"))
    assert all("engine/learning" not in e["path"] for entries in doc["guards"].values() for e in entries)
