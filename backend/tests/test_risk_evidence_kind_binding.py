"""
backend/tests/test_risk_evidence_kind_binding.py
================================================
Tests for RiskEvidenceKindBinding (Phase 15C.2).

The binding establishes ONLY: declared RiskEvidenceKind + exact MissingRiskEvidence /
RiskEvidenceContentMatch + kind.axis is resolution-context axis. It does NOT decode
content or verify that bytes conform to the declared kind.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import date, datetime, timezone
from enum import Enum

import pytest

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, RiskAxis, RiskEvidenceKind
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef

_KIND_MSG = r"^kind must be an exact RiskEvidenceKind instance$"
_RES_MSG = r"^resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance$"
_AXIS_MSG = r"^risk evidence kind axis must match resolution context axis$"

ALL_KINDS = list(RiskEvidenceKind)


def _context(axis: RiskAxis = RiskAxis.CAPACITY) -> RiskAxisContext:
    temporal = AnalysisTemporalContext(
        horizon_context=AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2024, 6, 1)),
        pit_context=AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        ),
    )
    return RiskAxisContext(axis=axis, temporal_context=temporal)


def _missing(axis: RiskAxis = RiskAxis.CAPACITY) -> MissingRiskEvidence:
    return MissingRiskEvidence(context=_context(axis), missing_inputs=("diagnostic_input",))


def _pit_binding(content: bytes, axis: RiskAxis = RiskAxis.CAPACITY) -> RiskEvidencePITBinding:
    availability = RiskEvidenceAvailabilityRef(
        provenance_ref=RiskEvidenceProvenanceRef(
            source_key="test.source", content_sha256=hashlib.sha256(content).hexdigest()
        ),
        available_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    return RiskEvidencePITBinding(context=_context(axis), availability_ref=availability)


def _content_match(content: bytes = b"evidence", axis: RiskAxis = RiskAxis.CAPACITY) -> RiskEvidenceContentMatch:
    return RiskEvidenceContentMatch(pit_binding=_pit_binding(content, axis), content=content)


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")

    def __str__(self) -> str:
        raise RuntimeError("hostile str")


class _HostileMeta(type):
    @property
    def __name__(cls):  # type: ignore[override]
        raise RuntimeError("hostile metaclass __name__")


class _HostileMetaInstance(metaclass=_HostileMeta):
    pass


class _ForeignEnum(Enum):
    CASH_BALANCE = "cash_balance"


class _MissingSub(MissingRiskEvidence):
    pass


class _ContentMatchSub(RiskEvidenceContentMatch):
    pass


# --- structure ---------------------------------------------------------------

def test_fields_are_exactly_kind_and_resolution() -> None:
    assert [f.name for f in dataclasses.fields(RiskEvidenceKindBinding)] == ["kind", "resolution"]


def test_binding_is_frozen() -> None:
    binding = RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=_missing())
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.kind = RiskEvidenceKind.INVESTMENT_GOAL  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.resolution = _missing()  # type: ignore[misc]


@pytest.mark.parametrize(
    "attr",
    [
        "context", "axis", "content", "payload", "schema", "schema_version", "owner_id",
        "decode", "to_fact", "fact", "verified", "kind_verified", "semantic_match",
        "is_missing", "is_present", "validated", "score", "level", "weight", "rank",
        "threshold", "status", "suitability", "overall_risk", "required_risk",
    ],
)
def test_no_decoder_or_decision_surface(attr: str) -> None:
    binding = RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=_missing())
    assert not hasattr(binding, attr)


def test_no_raw_bytes_retained() -> None:
    content = b"raw-evidence-bytes"
    binding = RiskEvidenceKindBinding(
        kind=RiskEvidenceKind.CASH_BALANCE, resolution=_content_match(content)
    )
    assert content not in repr(binding).encode()
    assert content not in repr(vars(binding)).encode()
    assert not any(isinstance(v, (bytes, bytearray, memoryview)) for v in vars(binding).values())


# --- happy paths ------------------------------------------------------------

@pytest.mark.parametrize("kind", ALL_KINDS)
def test_missing_branch_succeeds_and_preserves_identity(kind: RiskEvidenceKind) -> None:
    missing = _missing()
    binding = RiskEvidenceKindBinding(kind=kind, resolution=missing)
    assert binding.kind is kind
    assert binding.resolution is missing
    assert binding.resolution.context is missing.context


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_content_match_branch_succeeds_and_preserves_identity(kind: RiskEvidenceKind) -> None:
    match = _content_match()
    binding = RiskEvidenceKindBinding(kind=kind, resolution=match)
    assert binding.kind is kind
    assert binding.resolution is match
    assert binding.resolution.pit_binding is match.pit_binding


def test_missing_is_not_zero_or_default() -> None:
    missing = _missing()
    binding = RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=missing)
    assert type(binding.resolution) is MissingRiskEvidence
    for falsy in (0, 0.0, False, b"", "", None):
        assert binding.resolution is not falsy


def test_same_axis_kind_mislabeling_is_not_detected_at_this_stage() -> None:
    # Bytes of any origin bind to any CAPACITY kind: no payload decoder exists yet.
    match = _content_match(b"pretend this is a cash balance payload")
    for kind in ALL_KINDS:
        assert RiskEvidenceKindBinding(kind=kind, resolution=match).kind is kind


# --- axis compatibility ------------------------------------------------------

@pytest.mark.parametrize("kind", ALL_KINDS)
def test_tolerance_context_missing_branch_rejected(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_AXIS_MSG):
        RiskEvidenceKindBinding(kind=kind, resolution=_missing(RiskAxis.TOLERANCE))


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_tolerance_context_content_branch_rejected(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_AXIS_MSG):
        RiskEvidenceKindBinding(kind=kind, resolution=_content_match(axis=RiskAxis.TOLERANCE))


# --- strict kind validation -------------------------------------------------

@pytest.mark.parametrize(
    "bad_kind",
    ["cash_balance", "CASH_BALANCE", _ForeignEnum.CASH_BALANCE, RiskAxis.CAPACITY, None, True, 1, object()],
)
def test_invalid_kind_rejected(bad_kind) -> None:
    with pytest.raises(TypeError, match=_KIND_MSG):
        RiskEvidenceKindBinding(kind=bad_kind, resolution=_missing())


def test_invalid_kind_fails_before_resolution_inspection() -> None:
    with pytest.raises(TypeError, match=_KIND_MSG):
        RiskEvidenceKindBinding(kind="cash_balance", resolution=_Hostile())  # type: ignore[arg-type]


# --- strict resolution validation -------------------------------------------

@pytest.mark.parametrize(
    "bad_resolution",
    [
        None, "x", {}, (), True, 1, object(),
        _pit_binding(b"a"),
        RiskEvidenceAvailabilityRef(
            provenance_ref=RiskEvidenceProvenanceRef(source_key="s", content_sha256="0" * 64),
            available_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        RiskEvidenceProvenanceRef(source_key="s", content_sha256="0" * 64),
    ],
)
def test_invalid_resolution_rejected(bad_resolution) -> None:
    with pytest.raises(TypeError, match=_RES_MSG):
        RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=bad_resolution)


def test_missing_subclass_rejected() -> None:
    sub = _MissingSub(context=_context(), missing_inputs=("diagnostic_input",))
    with pytest.raises(TypeError, match=_RES_MSG):
        RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=sub)


def test_content_match_subclass_rejected() -> None:
    content = b"abc"
    sub = _ContentMatchSub(pit_binding=_pit_binding(content), content=content)
    with pytest.raises(TypeError, match=_RES_MSG):
        RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=sub)


# --- callback safety ---------------------------------------------------------

def test_hostile_repr_str_kind_not_invoked() -> None:
    with pytest.raises(TypeError, match=_KIND_MSG):
        RiskEvidenceKindBinding(kind=_Hostile(), resolution=_missing())  # type: ignore[arg-type]


def test_hostile_repr_str_resolution_not_invoked() -> None:
    with pytest.raises(TypeError, match=_RES_MSG):
        RiskEvidenceKindBinding(kind=RiskEvidenceKind.CASH_BALANCE, resolution=_Hostile())  # type: ignore[arg-type]


def test_hostile_metaclass_name_not_invoked() -> None:
    with pytest.raises(TypeError, match=_KIND_MSG):
        RiskEvidenceKindBinding(kind=_HostileMetaInstance(), resolution=_missing())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=_RES_MSG):
        RiskEvidenceKindBinding(
            kind=RiskEvidenceKind.CASH_BALANCE, resolution=_HostileMetaInstance()  # type: ignore[arg-type]
        )
