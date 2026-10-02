"""
backend/tests/invariants/test_static_guards.py
===============================================
Static correctness guards G1-G5 for the Private Engine.

G1  no ambient clock          (pure manifest)
G2  no generated identity     (pure manifest)
G3  no I/O dependency         (pure manifest)
G4  no new explicit float()   (backend/engine/private/**, baseline ratchet)
G5  no new missing->zero      (backend/engine/private/**, baseline ratchet)

Static guards are not production enforcement until the backend pytest command is a CI gate.
Authoritative runtime: Python 3.12.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from backend.tests.invariants import static_guards as sg

PURE_PATH = "backend/engine/private/risk_evidence_pit_binding.py"
ANY_PATH = "backend/engine/private/example.py"


def _src(code: str) -> str:
    return textwrap.dedent(code).lstrip("\n")


def _kinds(violations: list[sg.Violation]) -> list[str]:
    return sorted(v.node_kind for v in violations)


# --- manifest ---------------------------------------------------------------

EXPECTED_MANIFEST = (
    "backend/engine/private/domain.py",
    "backend/engine/private/analysis_context.py",
    "backend/engine/private/analysis_horizon.py",
    "backend/engine/private/analysis_pit.py",
    "backend/engine/private/risk_context.py",
    "backend/engine/private/risk_evidence.py",
    "backend/engine/private/risk_evidence_provenance.py",
    "backend/engine/private/risk_evidence_availability.py",
    "backend/engine/private/risk_evidence_pit_binding.py",
    "backend/engine/private/risk_evidence_resolution.py",
    "backend/engine/private/risk_evidence_content_match.py",
    "backend/engine/private/risk_evidence_kind_binding.py",
    "backend/engine/private/risk_evidence_cash_schema.py",
    "backend/engine/private/risk_evidence_cash_resolution.py",
    "backend/engine/private/risk_evidence_planned_contribution_schema.py",
    "backend/engine/private/risk_evidence_planned_contribution_resolution.py",
    "backend/engine/private/risk_evidence_investment_goal_schema.py",
    "backend/engine/private/risk_evidence_investment_goal_resolution.py",
    "backend/engine/private/allocation_matrix.py",
    "backend/engine/private/allocation_benchmarks.py",
    "backend/engine/private/allocation_risk_parity.py",
    "backend/engine/private/allocation_hrp.py",
    "backend/engine/private/allocation_cvar.py",
    "backend/engine/private/allocation_user_views.py",
    "backend/engine/private/allocation_user_view_posterior.py",
    "backend/engine/private/allocation_rebalance.py",
    "backend/engine/private/allocation_rebalance_policy.py",
    "backend/engine/private/allocation_universe_composition.py",
    "backend/engine/private/allocation_candidate_universe.py",
    "backend/engine/private/game_changer_event.py",
    "backend/engine/private/game_changer_assessment.py",
    "backend/engine/private/game_changer_revision_family.py",
    "backend/engine/private/game_changer_gate.py",
    "backend/engine/private/game_changer_extraction.py",
    "backend/engine/private/scheduler_trigger.py",
    "backend/engine/private/scheduler_scheduled_occurrence.py",
    "backend/engine/private/scheduler_recurrence.py",
    "backend/engine/private/scheduler_calendar_evidence.py",
    "backend/engine/private/scheduler_calendar_applicability.py",
    "backend/engine/private/scheduler_event_occurrence.py",
    "backend/engine/private/scheduler_run_admission.py",
    "backend/engine/private/scheduler_run_lifecycle.py",
    "backend/engine/private/scheduler_run_persistence_codec.py",
    "backend/engine/private/scheduler_run_persistence_transport.py",
    "backend/engine/private/backtest_replay_point.py",
    "backend/engine/private/backtest_replay_plan.py",
)


def test_pure_manifest_is_exactly_the_authoritative_list() -> None:
    assert sg.PURE_MANIFEST == EXPECTED_MANIFEST
    for rel in sg.PURE_MANIFEST:
        assert (sg.REPO_ROOT / rel).is_file(), rel


# --- G1 synthetic RED controls ---------------------------------------------

@pytest.mark.parametrize(
    "code",
    [
        "from datetime import datetime\nx = datetime.now()\n",
        "from datetime import datetime\nx = datetime.utcnow()\n",
        "from datetime import datetime\nx = datetime.today()\n",
        "from datetime import date\nx = date.today()\n",
        "import datetime\nx = datetime.datetime.now()\n",
        "import datetime as dt\nx = dt.datetime.now()\n",
        "from datetime import datetime as D\nx = D.now()\n",
        "from datetime import date as d\nx = d.today()\n",
        "import time\nx = time.time()\n",
        "import time\nx = time.monotonic()\n",
        "import time\nx = time.perf_counter()\n",
        "import time as t\nx = t.time()\n",
        "from time import time as now\nx = now()\n",
        "from time import perf_counter\nx = perf_counter()\n",
        "from datetime import date\nfrom dataclasses import field\nf = field(default_factory=date.today)\n",
    ],
)
def test_g1_detects_ambient_clock(code: str) -> None:
    assert len(sg.scan_g1(_src(code), PURE_PATH)) == 1


def test_g1_allows_timezone_conversion_and_clock_lookalikes() -> None:
    code = _src(
        '''
        """Docstring mentions datetime.now() and time.time()."""
        from datetime import datetime, timezone
        # datetime.now() in a comment
        def convert(value: datetime) -> datetime:
            now = "datetime.now()"
            return value.astimezone(timezone.utc)
        class Clockish:
            def now(self):
                return 1
        x = Clockish().now()
        '''
    )
    assert sg.scan_g1(code, PURE_PATH) == []


# --- G2 synthetic RED controls ---------------------------------------------

@pytest.mark.parametrize(
    "code",
    [
        "import uuid\nx = uuid.uuid1()\n",
        "import uuid\nx = uuid.uuid3(uuid.NAMESPACE_DNS, 'a')\n",
        "import uuid\nx = uuid.uuid4()\n",
        "import uuid\nx = uuid.uuid5(uuid.NAMESPACE_DNS, 'a')\n",
        "from uuid import uuid4\nx = uuid4()\n",
        "from uuid import uuid4 as make_id\nx = make_id()\n",
        "import uuid as u\nx = u.uuid4()\n",
        "import random\nx = random.random()\n",
        "import random as r\nx = r.choice([1])\n",
        "from random import randint\nx = randint(1, 2)\n",
        "import secrets\nx = secrets.token_hex(8)\n",
        "from secrets import token_bytes as tb\nx = tb(4)\n",
        # generator passed as a callback/default factory: construction will invoke it later
        "from dataclasses import field\nfrom uuid import uuid4\nx = field(default_factory=uuid4)\n",
        "from dataclasses import field\nimport uuid\nx = field(default_factory=uuid.uuid4)\n",
        "from dataclasses import field\nfrom uuid import uuid4 as make_id\nx = field(default_factory=make_id)\n",
        "from dataclasses import field\nimport uuid as u\nx = field(default_factory=u.uuid4)\n",
        "from dataclasses import field\nimport random\nx = field(default_factory=random.random)\n",
        "from dataclasses import field\nfrom random import random as r\nx = field(default_factory=r)\n",
        "from dataclasses import field\nimport secrets\nx = field(default_factory=secrets.token_hex)\n",
        "from uuid import uuid4\nfactory = uuid4\n",
    ],
)
def test_g2_detects_generated_identity_or_entropy(code: str) -> None:
    assert len(sg.scan_g2(_src(code), PURE_PATH)) == 1


def test_g2_allows_parsing_existing_uuid() -> None:
    code = _src(
        '''
        import uuid
        from uuid import UUID
        from dataclasses import field
        x = UUID(existing_text)
        y = field(default_factory=lambda: UUID(existing_text))
        a = uuid.UUID("12345678-1234-5678-1234-567812345678")
        b = UUID("12345678-1234-5678-1234-567812345678")
        def f(x: uuid.UUID) -> str:
            return str(x)
        '''
    )
    assert sg.scan_g2(code, PURE_PATH) == []


# --- G3 synthetic RED controls ---------------------------------------------

@pytest.mark.parametrize(
    "code",
    [
        "import requests\n",
        "import httpx\n",
        "import urllib.request\n",
        "from urllib import parse\n",
        "import socket\n",
        "import aiohttp\n",
        "import sqlite3\n",
        "import sqlalchemy\n",
        "from sqlalchemy.orm import Session\n",
        "import supabase\n",
        "import redis\n",
        "import os\n",
        "import os.path\n",
        "from os import environ\n",
        "import pathlib\n",
        "from pathlib import Path\n",
        "import subprocess\n",
        "import shutil\n",
        "data = open('x').read()\n",
    ],
)
def test_g3_detects_io_dependency(code: str) -> None:
    assert len(sg.scan_g3(_src(code), PURE_PATH)) == 1


def test_g3_private_import_must_be_in_manifest() -> None:
    assert sg.scan_g3("from backend.engine.private.domain import RiskAxis\n", PURE_PATH) == []
    assert sg.scan_g3("from backend.engine.private import domain\n", PURE_PATH) == []
    assert len(sg.scan_g3("from backend.engine.private.identity import X\n", PURE_PATH)) == 1
    assert len(sg.scan_g3("import backend.engine.private.orchestrator\n", PURE_PATH)) == 1
    assert len(sg.scan_g3("from backend.engine.private import orchestrator\n", PURE_PATH)) == 1
    assert len(sg.scan_g3("from backend.engine.private import *\n", PURE_PATH)) == 1
    assert len(sg.scan_g3("from .identity import X\n", PURE_PATH)) == 1
    assert sg.scan_g3("from .domain import RiskAxis\n", PURE_PATH) == []


def test_g3_allows_stdlib_pure_imports_and_lookalikes() -> None:
    code = _src(
        '''
        """Docstring: import os, open(file), requests."""
        from __future__ import annotations
        import hashlib
        import hmac
        import re
        from dataclasses import dataclass
        from decimal import Decimal
        # import os
        class Reader:
            def open(self):
                return 1
        x = Reader().open()
        '''
    )
    assert sg.scan_g3(code, PURE_PATH) == []


# --- G4 synthetic RED controls ---------------------------------------------

def test_g4_detects_float_and_builtins_float_calls() -> None:
    code = _src(
        '''
        import builtins
        import builtins as b
        a = float("1")
        c = builtins.float("2")
        d = b.float("3")
        '''
    )
    found = sg.scan_g4(code, ANY_PATH)
    assert _kinds(found) == ["Call:builtins.float", "Call:builtins.float", "Call:float"]


def test_g4_ignores_annotations_literals_comments_docstrings_and_lookalikes() -> None:
    code = _src(
        '''
        """float(x) in docstring."""
        # float(y) in comment
        x: float = 1.5
        def f(a: float) -> float:
            return a + 0.5
        class C:
            def float(self):
                return 1
        y = C().float()
        z = "float(1)"
        isinstance(x, float)
        '''
    )
    assert sg.scan_g4(code, ANY_PATH) == []


# --- G5 synthetic RED controls ---------------------------------------------

@pytest.mark.parametrize(
    "code",
    [
        "x = m.get(k, 0)\n",
        "x = m.get(k, 0.0)\n",
        'from decimal import Decimal\nx = m.get(k, Decimal("0"))\n',
        "from decimal import Decimal\nx = m.get(k, Decimal(0))\n",
        'import decimal\nx = m.get(k, decimal.Decimal("0.00"))\n',
        "x = v or 0\n",
        "x = v or 0.0\n",
        'from decimal import Decimal\nx = v or Decimal("0")\n',
        "x = a or b or 0\n",
        "x = v if v is not None else 0\n",
        "x = v if v is not None else 0.0\n",
        'from decimal import Decimal\nx = v if v is not None else Decimal("0")\n',
        "x = 0 if v is None else v\n",
        "x = 0.0 if v is None else v\n",
        'from decimal import Decimal\nx = Decimal("0") if v is None else v\n',
    ],
)
def test_g5_detects_missing_to_zero_fallback(code: str) -> None:
    assert len(sg.scan_g5(_src(code), ANY_PATH)) == 1


def test_g5_ignores_non_zero_defaults_and_lookalikes() -> None:
    code = _src(
        '''
        """Docstring: m.get(k, 0) and v or 0."""
        from decimal import Decimal
        # m.get(k, 0)
        a = m.get(k)
        b = m.get(k, None)
        c = m.get(k, 1)
        d = m.get(k, "0")
        e = v or None
        f = v or 1
        g = v or Decimal("1")
        h = v if v is not None else 1
        i = 0 if v is not None else v
        j = True or False
        k2 = m.get(0, k)
        l = v and 0
        '''
    )
    assert sg.scan_g5(code, ANY_PATH) == []


# --- fingerprints & ordinals ------------------------------------------------

def test_fingerprint_is_stable_under_formatting_and_comments() -> None:
    a = "def f(v):\n    return float(v)\n"
    b = "def f(v):\n    # a comment\n    return float( v )  # trailing\n"
    c = "\n\n\ndef f(v):\n\n    return   float(\n        v\n    )\n"
    fa, fb, fc = (sg.scan_g4(s, ANY_PATH)[0] for s in (a, b, c))
    assert fa.fingerprint == fb.fingerprint == fc.fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", fa.fingerprint)
    assert (fa.qualname, fa.node_kind) == ("f", "Call:float")


def test_fingerprint_changes_when_semantics_change() -> None:
    base = sg.scan_g4("def f(v):\n    return float(v)\n", ANY_PATH)[0]
    assert sg.scan_g4("def f(v):\n    return float(v) * 2\n", ANY_PATH)[0].fingerprint != base.fingerprint
    assert sg.scan_g4("def f(v):\n    return float(w)\n", ANY_PATH)[0].fingerprint != base.fingerprint


def test_fingerprint_ignores_line_number_but_line_is_diagnostic() -> None:
    a = sg.scan_g4("def f(v):\n    return float(v)\n", ANY_PATH)[0]
    b = sg.scan_g4("\n\n\n\ndef f(v):\n    return float(v)\n", ANY_PATH)[0]
    assert a.fingerprint == b.fingerprint
    assert (a.line, b.line) == (2, 6)
    assert a.identity == b.identity


def test_compound_statement_fingerprint_uses_header_only() -> None:
    a = "def f(v):\n    if float(v) > 0:\n        return 1\n"
    b = "def f(v):\n    if float(v) > 0:\n        return 2\n        \n"
    assert sg.scan_g4(a, ANY_PATH)[0].fingerprint == sg.scan_g4(b, ANY_PATH)[0].fingerprint


def test_duplicate_fingerprints_get_distinct_ordinals() -> None:
    code = "def f(v):\n    a = float(v)\n    a = float(v)\n    a = float(v)\n"
    found = sg.scan_g4(code, ANY_PATH)
    assert [v.ordinal for v in found] == [0, 1, 2]
    assert len({v.fingerprint for v in found}) == 1
    assert len({v.identity for v in found}) == 3


def test_qualname_tracks_class_and_function_scope() -> None:
    code = _src(
        '''
        x = float(1)
        class A:
            def m(self):
                def inner():
                    return float(2)
                return inner
        '''
    )
    assert [v.qualname for v in sg.scan_g4(code, ANY_PATH)] == ["<module>", "A.m.inner"]


def test_two_identical_statements_in_different_scopes_are_distinct() -> None:
    code = "def f(v):\n    return float(v)\ndef g(v):\n    return float(v)\n"
    found = sg.scan_g4(code, ANY_PATH)
    assert len({v.identity for v in found}) == 2


# --- baseline machinery -----------------------------------------------------

def _entry(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": ANY_PATH,
        "qualname": "f",
        "node_kind": "Call:float",
        "fingerprint": "a" * 64,
        "ordinal": 0,
        "line": 2,
        "classification": "legacy_debt",
        "note": "n",
    }
    base.update(over)
    return base


def _doc(g4: list[dict[str, object]] | None = None, g5: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"schema_version": 1, "guards": {"G4": g4 or [], "G5": g5 or []}}


def test_baseline_valid_document_passes_validation() -> None:
    assert sg.validate_baseline(_doc([_entry()])) == []


@pytest.mark.parametrize(
    "doc",
    [
        {"schema_version": 2, "guards": {"G4": [], "G5": []}},
        {"schema_version": 1, "guards": {"G4": []}},
        {"schema_version": 1, "guards": {"G4": [], "G5": [], "G6": []}},
        {"schema_version": 1, "guards": {"G4": [], "G5": []}, "extra": 1},
        _doc([_entry(classification="bogus")]),
        _doc([_entry(note="")]),
        _doc([_entry(fingerprint="xyz")]),
        _doc([_entry(ordinal=-1)]),
        _doc([_entry(ordinal=True)]),
        _doc([_entry(line=0)]),
        _doc([{k: v for k, v in _entry().items() if k != "note"}]),
        _doc([{**_entry(), "extra": 1}]),
        _doc([_entry(), _entry()]),
        _doc([_entry(path="b.py"), _entry(path="a.py")]),
    ],
)
def test_baseline_invalid_documents_are_rejected(doc: dict[str, object]) -> None:
    assert sg.validate_baseline(doc) != []


def test_baseline_diff_semantics() -> None:
    cur = sg.scan_g4("def f(v):\n    return float(v)\n", ANY_PATH)
    exact = [{**_entry(fingerprint=cur[0].fingerprint, line=2)}]
    new, stale = sg.diff_against_baseline(cur, exact)
    assert (new, stale) == ([], [])
    new, stale = sg.diff_against_baseline(cur, [])
    assert len(new) == 1 and stale == []
    new, stale = sg.diff_against_baseline([], exact)
    assert new == [] and len(stale) == 1
    mutated = sg.scan_g4("def f(v):\n    return float(v) * 2\n", ANY_PATH)
    new, stale = sg.diff_against_baseline(mutated, exact)
    assert len(new) == 1 and len(stale) == 1
    moved = sg.scan_g4("def f(v):\n    return float(v)\n", "backend/engine/private/other.py")
    new, stale = sg.diff_against_baseline(moved, exact)
    assert len(new) == 1 and len(stale) == 1
    shifted = sg.scan_g4("\n\n\ndef f(v):\n    return float(v)\n", ANY_PATH)
    assert sg.diff_against_baseline(shifted, exact) == ([], [])


def test_baseline_counts_repeated_statements() -> None:
    code = "def f(v):\n    a = float(v)\n    a = float(v)\n"
    cur = sg.scan_g4(code, ANY_PATH)
    one_entry = [_entry(fingerprint=cur[0].fingerprint, ordinal=0)]
    new, stale = sg.diff_against_baseline(cur, one_entry)
    assert len(new) == 1 and stale == []


def test_canonical_dump_round_trips_and_is_sorted() -> None:
    doc = _doc([_entry(path="b.py"), _entry(path="a.py")])
    text = sg.dump_baseline(doc)
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert [e["path"] for e in parsed["guards"]["G4"]] == ["a.py", "b.py"]
    assert sg.dump_baseline(parsed) == text


# --- REAL TREE: G1-G3 -------------------------------------------------------

@pytest.mark.parametrize("rel", EXPECTED_MANIFEST)
def test_g1_g2_g3_pure_manifest_module_is_clean(rel: str) -> None:
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    found = sg.scan_g1(source, rel) + sg.scan_g2(source, rel) + sg.scan_g3(source, rel)
    assert found == [], [f"{v.guard} {v.path}:{v.line} {v.node_kind}" for v in found]


# --- REAL TREE: G4/G5 ratchets ---------------------------------------------

def test_private_scan_scope_excludes_tests_caches_and_non_private() -> None:
    files = sg.private_python_files()
    assert files, "no private engine files found"
    for rel in files:
        assert rel.startswith("backend/engine/private/") and rel.endswith(".py")
        assert "__pycache__" not in rel and "/tests/" not in rel
    assert "backend/engine/private/risk_evidence_content_match.py" in files
    assert files == sorted(files)


def test_baseline_file_is_valid_and_canonical() -> None:
    raw = sg.BASELINE_PATH.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert sg.validate_baseline(doc) == []
    assert raw == sg.dump_baseline(doc), "baseline.json is not in canonical form"


@pytest.mark.parametrize("guard", ["G4", "G5"])
def test_ratchet_no_new_violations(guard: str) -> None:
    doc = json.loads(sg.BASELINE_PATH.read_text(encoding="utf-8"))
    new, _stale = sg.diff_against_baseline(sg.scan_private_tree(guard), doc["guards"][guard])
    assert new == [], "NEW/MUTATED violations (fix the code; never add to baseline to silence):\n" + "\n".join(
        f"  {guard} {v.path}:{v.line} {v.qualname} {v.node_kind}" for v in new
    )


@pytest.mark.parametrize("guard", ["G4", "G5"])
def test_ratchet_baseline_has_no_stale_entries(guard: str) -> None:
    doc = json.loads(sg.BASELINE_PATH.read_text(encoding="utf-8"))
    _new, stale = sg.diff_against_baseline(sg.scan_private_tree(guard), doc["guards"][guard])
    assert stale == [], "STALE baseline entries (debt reduced: shrink baseline.json in the same commit):\n" + "\n".join(
        f"  {guard} {e['path']} {e['qualname']} {e['node_kind']}" for e in stale
    )
