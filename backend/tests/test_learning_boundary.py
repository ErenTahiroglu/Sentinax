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
MODULES = ["__init__", "_checks", "temporal_provenance", "source_authority", "universe_coverage", "observation_status", "preregistration", "adapters"]
CI_TESTS = [
    "test_learning_provenance", "test_learning_source_authority", "test_learning_universe_coverage", "test_learning_observation_status",
    "test_learning_preregistration", "test_learning_adapters", "test_learning_boundary",
]
STEP = "Phase 28B-0 learning evidence contracts"

STDLIB_ALLOWED = {"__future__", "dataclasses", "datetime", "decimal", "enum", "hashlib", "json", "re", "typing", "collections", "math", "uuid"}
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
        "import backend.engine.learning.adapters, backend.engine.learning.preregistration, backend.engine.learning.universe_coverage\n"
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
            if rel.startswith(("backend/engine/learning/", "backend/tests/")) or ".venv" in rel:
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
            assert p.stem in CI_TESTS or p.stem == "learning_support", p.name


def test_ci_gate_is_permanent_and_lists_every_learning_test() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"- name: {STEP}" in ci
    block = ci.split(f"- name: {STEP}", 1)[1].split("\n    - name:", 1)[0]
    for t in CI_TESTS:
        assert f"backend/tests/{t}.py" in block, t
    assert "--disable-socket" in block
    for earlier in ("Static invariant guards", "Phase 24 private scheduler correctness", "Phase 26 backtest architecture correctness",
                    "Phase 26 PostgreSQL history-coverage concurrency", "Phase 27 PostgreSQL Supabase security advisor regression"):
        assert f"- name: {earlier}" in ci, earlier


def test_static_baseline_file_is_not_a_learning_concern() -> None:
    doc = json.loads((ROOT / "backend" / "tests" / "invariants" / "baseline.json").read_text(encoding="utf-8"))
    assert all("engine/learning" not in e["path"] for entries in doc["guards"].values() for e in entries)
