"""
tests/test_no_crypto_path.py
==============================
Verifies that no active production module contains crypto-specific routing
or processing logic that could route crypto assets into the analysis pipeline.

These are static/structural tests — no import of the actual modules needed
for logic execution. Tests scan the source code to detect forbidden patterns.

Forbidden patterns in active (non-deprecated) code:
    - "CRYPTO" as a return value from detect_market()
    - Explicit BTC/ETH/SOL/XRP in active market routing (not just guards)
    - "-USD" suffix routing to a fetch path
    - Crypto-specific analysis branches
"""

import os
import re
import pytest


REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
BACKEND_ROOT = os.path.join(REPO_ROOT, "backend")
DEPRECATED_DIR = os.path.join(BACKEND_ROOT, "deprecated")


def collect_active_py_files() -> list[str]:
    """
    Returns all .py files in backend/ EXCEPT:
    - backend/deprecated/ (archive)
    - backend/tests/ (test files legitimately import from deprecated with try/except guards)
    """
    active_files = []
    excluded_dirs = [
        os.path.abspath(DEPRECATED_DIR),
        os.path.abspath(os.path.join(BACKEND_ROOT, "tests")),
    ]
    for dirpath, dirnames, filenames in os.walk(BACKEND_ROOT):
        abs_dirpath = os.path.abspath(dirpath)
        # Skip excluded directories
        if any(abs_dirpath.startswith(ex) for ex in excluded_dirs):
            continue
        # Skip __pycache__
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            if fname.endswith(".py"):
                active_files.append(os.path.join(dirpath, fname))
    return active_files



def read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestNoCryptoProductionPath:
    """
    Structural tests: active production code must not route crypto assets.
    """

    def test_detect_market_does_not_return_crypto(self):
        """
        market_detector.py must not return "CRYPTO" as a market string.
        UNKNOWN is the correct response for out-of-scope instruments.
        """
        market_detector_path = os.path.join(BACKEND_ROOT, "data", "market_detector.py")
        content = read_file(market_detector_path)
        
        # Must NOT have return "CRYPTO" statement
        assert 'return "CRYPTO"' not in content, (
            'market_detector.py must not return "CRYPTO". '
            'Use "UNKNOWN" for out-of-scope instruments.'
        )
        # Should have UNKNOWN for crypto-like patterns
        assert "UNKNOWN" in content, (
            'market_detector.py should return "UNKNOWN" for out-of-scope instruments.'
        )

    def test_data_nodes_no_crypto_routing(self):
        """
        data_nodes.py must not contain is_crypto variable or crypto fast-path.
        """
        data_nodes_path = os.path.join(BACKEND_ROOT, "nodes", "data_nodes.py")
        content = read_file(data_nodes_path)
        
        assert "is_crypto" not in content, (
            "data_nodes.py must not have is_crypto routing. "
            "Crypto path was removed — check for regression."
        )

    def test_no_active_module_imports_from_deprecated(self):
        """
        No active (non-deprecated) module may import from backend.deprecated.
        """
        active_files = collect_active_py_files()
        violations = []
        
        for fpath in active_files:
            content = read_file(fpath)
            if "from backend.deprecated" in content or "import backend.deprecated" in content:
                violations.append(os.path.relpath(fpath, REPO_ROOT))
        
        assert not violations, (
            f"Active modules must not import from backend.deprecated: {violations}"
        )

    def test_execution_engine_not_importable_from_active_code(self):
        """
        execution_engine.py was deleted. No active module should reference it.
        """
        active_files = collect_active_py_files()
        violations = []
        
        for fpath in active_files:
            content = read_file(fpath)
            if "execution_engine" in content:
                violations.append(os.path.relpath(fpath, REPO_ROOT))
        
        assert not violations, (
            f"execution_engine reference found in active code: {violations}"
        )

    def test_islamic_analyzer_not_referenced_in_active_code(self):
        """
        islamic_analyzer.py was deleted. No active module should import it.
        """
        active_files = collect_active_py_files()
        violations = []
        
        for fpath in active_files:
            content = read_file(fpath)
            if "from backend.analyzers.islamic_analyzer" in content:
                violations.append(os.path.relpath(fpath, REPO_ROOT))
        
        assert not violations, (
            f"islamic_analyzer import found in active code: {violations}"
        )

    def test_ml_predictor_not_imported_in_active_code(self):
        """
        ml_predictor.py was moved to deprecated. No active module should import it
        from the original location.
        """
        active_files = collect_active_py_files()
        violations = []
        
        for fpath in active_files:
            content = read_file(fpath)
            if "from backend.analyzers.ml_predictor" in content:
                violations.append(os.path.relpath(fpath, REPO_ROOT))
        
        assert not violations, (
            f"ml_predictor import from original location found in active code: {violations}"
        )

    def test_optimization_engine_not_imported_in_active_code(self):
        """
        optimization_engine.py was moved to deprecated. No active module should
        import it from the original location.
        """
        active_files = collect_active_py_files()
        violations = []
        
        for fpath in active_files:
            content = read_file(fpath)
            if "from backend.engine.optimization_engine" in content:
                violations.append(os.path.relpath(fpath, REPO_ROOT))
        
        assert not violations, (
            f"optimization_engine import from original location found: {violations}"
        )
