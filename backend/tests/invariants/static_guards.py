"""
backend/tests/invariants/static_guards.py
=========================================
Deterministic AST scanner used by the Private Engine static correctness guards (G1-G5).

Guards:
    G1  no ambient clock          - pure manifest only, no baseline
    G2  no generated identity     - pure manifest only, no baseline
    G3  no I/O dependency         - pure manifest only, no baseline
    G4  explicit float() casts    - backend/engine/private/**, baseline ratchet
    G5  missing -> zero fallbacks - backend/engine/private/**, baseline ratchet

Design notes:
    - Pure static analysis (`ast`, `tokenize`, `hashlib`); nothing is imported or executed.
    - Names are resolved through the module's own import aliases, so `import datetime as dt`
      and `from time import time as now` are detected. Comments/docstrings/strings never match.
    - Ratchet identity: (path, qualname, node_kind, fingerprint, ordinal). `line` is diagnostic only.
    - Fingerprint: SHA-256 of the normalized token stream of the enclosing statement (header only
      for compound statements). Comments and formatting are irrelevant; semantic tokens are not.
      f-strings are collapsed to a single token so Python 3.9 and 3.12 agree.
    - Not covered (accepted gaps): dynamic imports (`importlib`, `__import__`), clock/entropy hidden
      behind helper functions, implicit float creation (`/`, math, numpy), zero fallbacks that are
      not one of the listed syntactic forms.

Authoritative runtime: Python 3.12.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import tokenize
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_ROOT = "backend/engine/private"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

PURE_MANIFEST: tuple[str, ...] = (
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
)
_MANIFEST_MODULES = frozenset(p[: -len(".py")].replace("/", ".") for p in PURE_MANIFEST)

CLOCK_CALLS = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "time.time",
        "time.monotonic",
        "time.perf_counter",
    }
)
UUID_CALLS = frozenset({"uuid.uuid1", "uuid.uuid3", "uuid.uuid4", "uuid.uuid5"})
ENTROPY_MODULES = ("random", "secrets")
IO_ROOTS = frozenset(
    {
        "requests", "httpx", "urllib", "socket", "aiohttp",
        "sqlite3", "sqlalchemy", "supabase", "redis",
        "os", "pathlib", "subprocess", "shutil",
    }
)
_PRIVATE_PACKAGE = "backend.engine.private"

CLASSIFICATIONS = frozenset(
    {"legacy_debt", "legitimate_nonmissing_semantics", "legitimate_boundary_semantics"}
)
_ENTRY_KEYS = ("path", "qualname", "node_kind", "fingerprint", "ordinal", "line", "classification", "note")
_IDENTITY_KEYS = ("path", "qualname", "node_kind", "fingerprint", "ordinal")
_HEX64 = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Violation:
    guard: str
    path: str
    qualname: str
    node_kind: str
    fingerprint: str
    ordinal: int
    line: int  # diagnostic only, never part of identity

    @property
    def identity(self) -> tuple[str, str, str, str, int]:
        return (self.path, self.qualname, self.node_kind, self.fingerprint, self.ordinal)


# --- AST helpers --------------------------------------------------------------

class _Context:
    """Parsed module plus parent links, scope names and import aliases."""

    def __init__(self, source: str, path: str) -> None:
        self.source = source
        self.path = path
        self.lines = source.splitlines(keepends=True)
        self.tree = ast.parse(source)
        self.parent: dict[int, ast.AST] = {}
        self.scope: dict[int, str] = {}
        self.aliases: dict[str, str] = {}
        self._index(self.tree, [])

    def _index(self, node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            self.parent[id(child)] = node
            self.scope[id(child)] = ".".join(scope) or "<module>"
            if isinstance(child, ast.Import):
                for a in child.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        root = a.name.split(".")[0]
                        self.aliases[root] = root
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                for a in child.names:
                    if a.name != "*":
                        self.aliases[a.asname or a.name] = f"{child.module}.{a.name}"
            inner = scope + [child.name] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else scope
            self._index(child, inner)

    def resolve(self, node: ast.AST) -> str | None:
        """Dotted name of a Name/Attribute chain with the base resolved through import aliases."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        base = self.aliases.get(cur.id)
        if base is None:
            return None if parts else cur.id
        return ".".join([base, *reversed(parts)])

    def qualname(self, node: ast.AST) -> str:
        return self.scope.get(id(node), "<module>")

    def statement(self, node: ast.AST) -> ast.stmt:
        cur: ast.AST | None = node
        while cur is not None and not isinstance(cur, ast.stmt):
            cur = self.parent.get(id(cur))
        assert isinstance(cur, ast.stmt)
        return cur

    def statement_text(self, stmt: ast.stmt) -> str:
        """Statement source; for compound statements only the header (up to the first body item)."""
        body = getattr(stmt, "body", None)
        start_line, start_col = stmt.lineno, stmt.col_offset
        if isinstance(body, list) and body:
            end_line, end_col = body[0].lineno, body[0].col_offset
        else:
            end_line, end_col = stmt.end_lineno or stmt.lineno, stmt.end_col_offset or 0
        chunk = self.lines[start_line - 1 : end_line]
        if not chunk:
            return ""
        chunk = list(chunk)
        if end_line == start_line:
            chunk[0] = chunk[0][:end_col]
            chunk[0] = chunk[0][start_col:]
        else:
            chunk[-1] = chunk[-1][:end_col]
            chunk[0] = chunk[0][start_col:]
        return "".join(chunk)


_SKIP_TOKENS = {
    tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER,
}
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def _normalized_tokens(text: str) -> list[str]:
    out: list[str] = []
    depth = 0
    fstart: tuple[int, int] | None = None
    lines = text.splitlines(keepends=True)

    def slice_between(a: tuple[int, int], b: tuple[int, int]) -> str:
        if a[0] == b[0]:
            return lines[a[0] - 1][a[1] : b[1]]
        parts = [lines[a[0] - 1][a[1] :]]
        parts.extend(lines[a[0] : b[0] - 1])
        parts.append(lines[b[0] - 1][: b[1]])
        return "".join(parts)

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if _FSTRING_START is not None and tok.type == _FSTRING_START:
                if depth == 0:
                    fstart = tok.start
                depth += 1
                continue
            if _FSTRING_END is not None and tok.type == _FSTRING_END:
                depth -= 1
                if depth == 0 and fstart is not None:
                    out.append(slice_between(fstart, tok.end))
                    fstart = None
                continue
            if depth > 0 or tok.type in _SKIP_TOKENS:
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        pass  # headers of compound statements may end mid-block; tokens read so far are kept
    return out


def _fingerprint(ctx: _Context, node: ast.AST) -> str:
    text = ctx.statement_text(ctx.statement(node))
    normalized = "\x1f".join(_normalized_tokens(text))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _finalize(ctx: _Context, guard: str, hits: list[tuple[ast.AST, str]]) -> list[Violation]:
    """Turn raw hits into Violations with deterministic ordinals (source order per identity key)."""
    hits = sorted(hits, key=lambda h: (getattr(h[0], "lineno", 0), getattr(h[0], "col_offset", 0), h[1]))
    counters: dict[tuple[str, str, str], int] = {}
    out: list[Violation] = []
    for node, kind in hits:
        qn = ctx.qualname(node)
        fp = _fingerprint(ctx, node)
        key = (qn, kind, fp)
        ordinal = counters.get(key, 0)
        counters[key] = ordinal + 1
        out.append(Violation(guard, ctx.path, qn, kind, fp, ordinal, getattr(node, "lineno", 0)))
    return out


# --- G1 / G2 --------------------------------------------------------------------

def _resolved_references(ctx: _Context) -> list[tuple[ast.AST, str]]:
    """Every outermost Name/Attribute load whose base resolves through an import alias."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(ctx.tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(getattr(node, "ctx", None), ast.Load):
            dotted = ctx.resolve(node)
            if dotted is not None:
                found.append((node, dotted))
    return found


def scan_g1(source: str, path: str) -> list[Violation]:
    ctx = _Context(source, path)
    hits = [(n, f"Clock:{d}") for n, d in _resolved_references(ctx) if d in CLOCK_CALLS]
    return _finalize(ctx, "G1", hits)


def scan_g2(source: str, path: str) -> list[Violation]:
    ctx = _Context(source, path)
    hits: list[tuple[ast.AST, str]] = []
    for n, d in _resolved_references(ctx):
        if d in UUID_CALLS or any(d.startswith(m + ".") for m in ENTROPY_MODULES):
            hits.append((n, f"Entropy:{d}"))
    return _finalize(ctx, "G2", hits)


# --- G3 ---------------------------------------------------------------------------

def _private_import_ok(module: str) -> bool:
    return module in _MANIFEST_MODULES


def scan_g3(source: str, path: str) -> list[Violation]:
    ctx = _Context(source, path)
    hits: list[tuple[ast.AST, str]] = []
    own_pkg = path[: path.rfind("/")].replace("/", ".") if "/" in path else ""

    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in IO_ROOTS:
                    hits.append((node, f"Import:{a.name}"))
                elif a.name == _PRIVATE_PACKAGE or a.name.startswith(_PRIVATE_PACKAGE + "."):
                    if not _private_import_ok(a.name):
                        hits.append((node, f"PrivateImport:{a.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                base_parts = own_pkg.split(".") if own_pkg else []
                if node.level - 1 > len(base_parts):
                    hits.append((node, "PrivateImport:<unresolvable-relative>"))
                    continue
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                module = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            root = module.split(".")[0]
            if root in IO_ROOTS:
                hits.append((node, f"Import:{module}"))
            elif module == _PRIVATE_PACKAGE:
                for a in node.names:
                    if a.name == "*" or not _private_import_ok(f"{module}.{a.name}"):
                        hits.append((node, f"PrivateImport:{module}.{a.name}"))
            elif module.startswith(_PRIVATE_PACKAGE + "."):
                if not _private_import_ok(module):
                    hits.append((node, f"PrivateImport:{module}"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            if "open" not in ctx.aliases:
                hits.append((node, "Call:open"))
    return _finalize(ctx, "G3", hits)


# --- G4 ---------------------------------------------------------------------------

def scan_g4(source: str, path: str) -> list[Violation]:
    ctx = _Context(source, path)
    hits: list[tuple[ast.AST, str]] = []
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "float" and "float" not in ctx.aliases:
            hits.append((node, "Call:float"))
        elif isinstance(func, ast.Attribute) and ctx.resolve(func) == "builtins.float":
            hits.append((node, "Call:builtins.float"))
    return _finalize(ctx, "G4", hits)


# --- G5 ---------------------------------------------------------------------------

def _is_zero(ctx: _Context, node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value == 0
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        name = ctx.resolve(node.func)
        if name in ("decimal.Decimal", "Decimal"):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and type(arg.value) in (int, float, str):
                try:
                    return Decimal(arg.value if type(arg.value) is not float else str(arg.value)) == 0
                except (InvalidOperation, ValueError):
                    return False
    return False


def _is_none_test(node: ast.AST, op: type) -> bool:
    return (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], op)
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is None
    )


def scan_g5(source: str, path: str) -> list[Violation]:
    ctx = _Context(source, path)
    hits: list[tuple[ast.AST, str]] = []
    for node in ast.walk(ctx.tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) >= 2
            and _is_zero(ctx, node.args[1])
        ):
            hits.append((node, "Call:get-default-zero"))
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(_is_zero(ctx, v) for v in node.values[1:]):
                hits.append((node, "BoolOp:or-zero"))
        elif isinstance(node, ast.IfExp):
            if (_is_none_test(node.test, ast.IsNot) and _is_zero(ctx, node.orelse)) or (
                _is_none_test(node.test, ast.Is) and _is_zero(ctx, node.body)
            ):
                hits.append((node, "IfExp:none-zero"))
    return _finalize(ctx, "G5", hits)


# --- tree scanning ------------------------------------------------------------------

def private_python_files() -> list[str]:
    root = REPO_ROOT / PRIVATE_ROOT
    return sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


_SCANNERS = {"G4": scan_g4, "G5": scan_g5}


def scan_private_tree(guard: str) -> list[Violation]:
    scanner = _SCANNERS[guard]
    out: list[Violation] = []
    for rel in private_python_files():
        out.extend(scanner((REPO_ROOT / rel).read_text(encoding="utf-8"), rel))
    return out


# --- baseline -------------------------------------------------------------------------

def _entry_identity(entry: dict) -> tuple:
    return tuple(entry[k] for k in _IDENTITY_KEYS)


def validate_baseline(doc: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "guards"}:
        return ["top level must be exactly {schema_version, guards}"]
    if doc["schema_version"] != 1 or type(doc["schema_version"]) is not int:
        errors.append("schema_version must be 1")
    guards = doc["guards"]
    if not isinstance(guards, dict) or set(guards) != {"G4", "G5"}:
        return errors + ["guards must be exactly {G4, G5}"]
    for name, entries in guards.items():
        if not isinstance(entries, list):
            errors.append(f"{name}: entries must be a list")
            continue
        seen: set[tuple] = set()
        previous: tuple | None = None
        for i, e in enumerate(entries):
            where = f"{name}[{i}]"
            if not isinstance(e, dict) or set(e) != set(_ENTRY_KEYS):
                errors.append(f"{where}: keys must be exactly {list(_ENTRY_KEYS)}")
                continue
            for k in ("path", "qualname", "node_kind"):
                if type(e[k]) is not str or not e[k]:
                    errors.append(f"{where}: {k} must be a non-empty string")
            fp = e["fingerprint"]
            if type(fp) is not str or len(fp) != 64 or not set(fp) <= _HEX64:
                errors.append(f"{where}: fingerprint must be 64 lowercase hex chars")
            if type(e["ordinal"]) is not int or e["ordinal"] < 0:
                errors.append(f"{where}: ordinal must be an int >= 0")
            if type(e["line"]) is not int or e["line"] < 1:
                errors.append(f"{where}: line must be an int >= 1")
            if e["classification"] not in CLASSIFICATIONS:
                errors.append(f"{where}: classification must be one of {sorted(CLASSIFICATIONS)}")
            if type(e["note"]) is not str or not e["note"].strip():
                errors.append(f"{where}: note must be a non-empty string")
            try:
                ident = _entry_identity(e)
            except KeyError:
                continue
            if ident in seen:
                errors.append(f"{where}: duplicate identity")
            seen.add(ident)
            if previous is not None and ident < previous and not any("ordinal" in x for x in errors[-1:]):
                errors.append(f"{where}: entries must be sorted by identity")
            previous = ident
    return errors


def diff_against_baseline(
    current: list[Violation], baseline_entries: list[dict]
) -> tuple[list[Violation], list[dict]]:
    """Return (new_or_mutated_violations, stale_baseline_entries)."""
    base = {_entry_identity(e): e for e in baseline_entries}
    cur = {v.identity: v for v in current}
    new = [v for ident, v in cur.items() if ident not in base]
    stale = [e for ident, e in base.items() if ident not in cur]
    return new, stale


def dump_baseline(doc: dict) -> str:
    """Canonical, deterministic JSON: fixed key order, sorted entries, 2-space indent, trailing newline."""
    out_guards: dict[str, list[dict]] = {}
    for name in ("G4", "G5"):
        entries = sorted(doc["guards"][name], key=_entry_identity)
        out_guards[name] = [{k: e[k] for k in _ENTRY_KEYS} for e in entries]
    canonical = {"schema_version": doc["schema_version"], "guards": out_guards}
    return json.dumps(canonical, indent=2, ensure_ascii=False) + "\n"
