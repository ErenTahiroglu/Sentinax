"""
backend/tests/test_risk_evidence_content_match.py
==================================================
Fail-closed tests for RiskEvidenceContentMatch (Phase 15B.8).

The primitive establishes ONLY: SHA256(caller_supplied_bytes) equals the canonical
content_sha256 of the PIT-bound provenance chain. Empty bytes are valid input.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import hmac
import inspect
from datetime import date, datetime, timezone

import pytest

from backend.engine.private import risk_evidence_content_match as module_under_test
from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, RiskAxis
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef

_PIT_MSG = "^pit_binding must be an exact RiskEvidencePITBinding instance$"
_CONTENT_MSG = "^content must be exact bytes$"
_MISMATCH_MSG = "^content digest mismatch$"
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _binding(content_sha256: str) -> RiskEvidencePITBinding:
    provenance = RiskEvidenceProvenanceRef(source_key="test.source", content_sha256=content_sha256)
    availability = RiskEvidenceAvailabilityRef(
        provenance_ref=provenance,
        available_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    temporal = AnalysisTemporalContext(
        horizon_context=AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2024, 6, 1)),
        pit_context=AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        ),
    )
    context = RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=temporal)
    return RiskEvidencePITBinding(context=context, availability_ref=availability)


class _HostileRepr:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")

    def __str__(self) -> str:
        raise RuntimeError("hostile str")

    def __len__(self) -> int:
        raise RuntimeError("hostile len")


class _BytesSub(bytes):
    pass


class _BindingSub(RiskEvidencePITBinding):
    pass


# --- matching ---------------------------------------------------------------

def test_matching_content_succeeds() -> None:
    content = b"test evidence content"
    binding = _binding(_sha(content))
    match = RiskEvidenceContentMatch(pit_binding=binding, content=content)
    assert match.pit_binding is binding
    assert match.content_length == 21
    assert type(match.content_length) is int


def test_known_vector_matches() -> None:
    binding = _binding("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    assert RiskEvidenceContentMatch(binding, b"abc").content_length == 3


def test_one_byte_mutation_fails_closed() -> None:
    binding = _binding(_sha(b"abc"))
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"abd")


def test_completely_different_content_fails_closed() -> None:
    binding = _binding(_sha(b"abc"))
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"entirely different bytes")


# --- empty bytes are valid input --------------------------------------------

def test_empty_bytes_match_their_real_digest() -> None:
    assert _sha(b"") == _EMPTY_SHA256
    binding = _binding(_EMPTY_SHA256)
    match = RiskEvidenceContentMatch(pit_binding=binding, content=b"")
    assert match.pit_binding is binding
    assert match.content_length == 0
    assert type(match.content_length) is int


def test_empty_bytes_with_non_matching_digest_is_ordinary_mismatch() -> None:
    binding = _binding(_sha(b"abc"))
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"")


def test_non_empty_bytes_against_empty_digest_is_mismatch() -> None:
    binding = _binding(_EMPTY_SHA256)
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"\x00")


# --- binary exactness -------------------------------------------------------

@pytest.mark.parametrize(
    "content",
    [
        b"\x00\xff\xfe\x00",
        b"\x00",
        b"a\x00b",
        b"\xff\xfe\xfd",
        bytes(range(256)),
        b"line\r\nline\n",
        b"  padded  ",
    ],
)
def test_binary_bytes_hash_exactly(content: bytes) -> None:
    match = RiskEvidenceContentMatch(pit_binding=_binding(_sha(content)), content=content)
    assert match.content_length == len(content)


def test_no_newline_or_whitespace_normalization() -> None:
    binding = _binding(_sha(b"line\n"))
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"line\r\n")
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"line")


# --- determinism ------------------------------------------------------------

def test_deterministic_success_and_failure() -> None:
    content = b"\x00deterministic\xff"
    good = _binding(_sha(content))
    bad = _binding(_sha(content + b"x"))
    lengths = {RiskEvidenceContentMatch(good, content).content_length for _ in range(5)}
    assert lengths == {len(content)}
    for _ in range(5):
        with pytest.raises(ValueError, match=_MISMATCH_MSG):
            RiskEvidenceContentMatch(bad, content)


# --- strict content types ---------------------------------------------------

@pytest.mark.parametrize(
    "content",
    [
        _BytesSub(b"abc"),
        bytearray(b"abc"),
        memoryview(b"abc"),
        "abc",
        None,
        True,
        0,
        object(),
        _HostileRepr(),
    ],
    ids=["bytes-subclass", "bytearray", "memoryview", "str", "None", "bool", "int", "object", "hostile"],
)
def test_non_exact_bytes_rejected_before_hashing(content: object, monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("hashing must not occur for invalid content type")

    binding = _binding(_sha(b"abc"))
    monkeypatch.setattr(module_under_test.hashlib, "sha256", _forbidden)
    with pytest.raises(TypeError, match=_CONTENT_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=content)


# --- strict PIT binding types -----------------------------------------------

def test_binding_subclass_rejected() -> None:
    base = _binding(_sha(b"abc"))
    sub = _BindingSub(context=base.context, availability_ref=base.availability_ref)
    with pytest.raises(TypeError, match=_PIT_MSG):
        RiskEvidenceContentMatch(pit_binding=sub, content=b"abc")


@pytest.mark.parametrize(
    "binding",
    [None, "binding", True, object(), _HostileRepr()],
    ids=["None", "str", "bool", "object", "hostile"],
)
def test_non_exact_pit_binding_rejected(binding: object) -> None:
    with pytest.raises(TypeError, match=_PIT_MSG):
        RiskEvidenceContentMatch(pit_binding=binding, content=b"abc")


def test_errors_are_static_and_do_not_chain_hostile_objects() -> None:
    with pytest.raises(TypeError) as exc:
        RiskEvidenceContentMatch(pit_binding=_HostileRepr(), content=_HostileRepr())
    assert str(exc.value) == "pit_binding must be an exact RiskEvidencePITBinding instance"
    with pytest.raises(TypeError) as exc2:
        RiskEvidenceContentMatch(pit_binding=_binding(_sha(b"abc")), content=_HostileRepr())
    assert str(exc2.value) == "content must be exact bytes"


# --- compare_digest is really used -----------------------------------------

def test_uses_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(module_under_test.hmac, "compare_digest", _spy)
    content = b"abc"
    RiskEvidenceContentMatch(_binding(_sha(content)), content)
    assert calls == [(_sha(content), _sha(content))]


def test_compare_digest_verdict_is_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_under_test.hmac, "compare_digest", lambda a, b: False)
    with pytest.raises(ValueError, match=_MISMATCH_MSG):
        RiskEvidenceContentMatch(_binding(_sha(b"abc")), b"abc")


# --- identity, frozen, retention -------------------------------------------

def test_pit_binding_identity_preserved() -> None:
    binding = _binding(_sha(b"abc"))
    assert RiskEvidenceContentMatch(binding, b"abc").pit_binding is binding


def test_frozen() -> None:
    match = RiskEvidenceContentMatch(_binding(_sha(b"abc")), b"abc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.content_length = 999  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.pit_binding = None  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.content = b"abc"  # type: ignore[attr-defined]


def test_retained_state_is_only_binding_and_length() -> None:
    content = b"sensitive evidence data"
    match = RiskEvidenceContentMatch(_binding(_sha(content)), content)
    assert [f.name for f in dataclasses.fields(match)] == ["pit_binding", "content_length"]
    assert set(vars(match)) == {"pit_binding", "content_length"}
    assert content not in vars(match).values()
    assert "sensitive" not in repr(match)
    assert not hasattr(match, "content")
    assert not hasattr(type(match), "__slots__") or "content" not in type(match).__slots__


# --- purity -----------------------------------------------------------------

def test_module_imports_only_pure_dependencies() -> None:
    tree = ast.parse(inspect.getsource(module_under_test))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "hashlib", "hmac", "backend"}
    assert "backend.engine.private.risk_evidence_pit_binding" in {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }


def test_module_has_no_mutable_global_state() -> None:
    public = {
        name for name, value in vars(module_under_test).items()
        if not name.startswith("_") and not inspect.ismodule(value)
    }
    assert public <= {"annotations", "dataclass", "RiskEvidencePITBinding", "RiskEvidenceContentMatch"}
