"""
backend/tests/test_risk_evidence_planned_contribution_resolution.py
===================================================================
Tests for decoded PLANNED_CONTRIBUTION evidence resolution (Phase 15C.6).

Composition of: declared kind (15C.2 binding) + canonical PLANNED_CONTRIBUTION v1 schema (15C.5)
+ provenance digest revalidation (15B.8). Proves canonical bytes + declared kind + PIT/digest integrity ONLY;
it does not prove authoritative Portfolio.mode, repository linkage, or link integrity.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import risk_evidence_planned_contribution_resolution as module_under_test
from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import (
    AsOfMode, ContributionStatus, Currency, Horizon, PortfolioMode, RiskAxis, RiskEvidenceKind,
)
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_planned_contribution_resolution import (
    DecodedPlannedContributionRiskEvidence,
    resolve_planned_contribution_risk_evidence,
)
from backend.engine.private.risk_evidence_planned_contribution_schema import (
    PlannedContributionRiskFact,
    decode_planned_contribution_risk_fact,
    encode_planned_contribution_risk_fact,
)
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef

_BINDING_TYPE_MSG = r"^binding must be an exact RiskEvidenceKindBinding instance$"
_KIND_MSG = r"^binding kind must be PLANNED_CONTRIBUTION$"
_PRESENT_REQUIRED_MSG = r"^decoded planned contribution evidence requires content-matched evidence$"
_FACT_TYPE_MSG = r"^fact must be an exact PlannedContributionRiskFact instance$"
_BRANCH_MSG = r"^content must be supplied exactly for the content-matched planned contribution branch$"
_RESOLUTION_MSG = (
    r"^binding resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance$"
)
_DIGEST_MSG = r"^content digest mismatch$"
_ENVELOPE_MSG = r"^planned contribution risk evidence envelope is invalid$"
_CANON_MSG = r"^planned contribution risk evidence content is not canonical$"
_JSON_MSG = r"^planned contribution risk evidence content must be valid canonical UTF-8 JSON$"
_SIZE_CONTENT_MSG = r"^planned contribution risk evidence content exceeds maximum supported size$"
_SIZE_AMOUNT_MSG = r"^amount canonical representation exceeds maximum supported size$"
_BYTES_MSG = r"^content must be exact bytes$"

_P = UUID("11111111-1111-4111-8111-111111111111")
_C = UUID("33333333-3333-4333-8333-333333333333")
_G = UUID("44444444-4444-4444-8444-444444444444")
_B = UUID("55555555-5555-4555-8555-555555555555")

OTHER_KINDS = [RiskEvidenceKind.CASH_BALANCE, RiskEvidenceKind.INVESTMENT_GOAL]
PLANNED = RiskEvidenceKind.PLANNED_CONTRIBUTION


def _fact(amount: str = "1000", **overrides) -> PlannedContributionRiskFact:
    kwargs = dict(
        portfolio_id=_P,
        mode=PortfolioMode.MY_PORTFOLIO,
        contribution_id=_C,
        goal_id=_G,
        cash_bucket_id=None,
        expected_date=date(2026, 10, 1),
        amount=Decimal(amount),
        currency=Currency.TRY,
        status=ContributionStatus.PLANNED,
    )
    kwargs.update(overrides)
    return PlannedContributionRiskFact(**kwargs)


def _content(amount: str = "1000", **overrides) -> bytes:
    return encode_planned_contribution_risk_fact(_fact(amount, **overrides))


def _context(axis: RiskAxis = RiskAxis.CAPACITY) -> RiskAxisContext:
    temporal = AnalysisTemporalContext(
        horizon_context=AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 9, 1)),
        pit_context=AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc),
        ),
    )
    return RiskAxisContext(axis=axis, temporal_context=temporal)


def _content_match(content: bytes) -> RiskEvidenceContentMatch:
    availability = RiskEvidenceAvailabilityRef(
        provenance_ref=RiskEvidenceProvenanceRef(
            source_key="test.contribution", content_sha256=hashlib.sha256(content).hexdigest()
        ),
        available_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc),
    )
    pit = RiskEvidencePITBinding(context=_context(), availability_ref=availability)
    return RiskEvidenceContentMatch(pit_binding=pit, content=content)


def _binding(content: bytes, kind: RiskEvidenceKind = PLANNED) -> RiskEvidenceKindBinding:
    return RiskEvidenceKindBinding(kind=kind, resolution=_content_match(content))


def _missing_binding(kind: RiskEvidenceKind = PLANNED) -> RiskEvidenceKindBinding:
    missing = MissingRiskEvidence(context=_context(), missing_inputs=("contribution_snapshot",))
    return RiskEvidenceKindBinding(kind=kind, resolution=missing)


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


class _BindingSub(RiskEvidenceKindBinding):
    pass


class _FactSub(PlannedContributionRiskFact):
    pass


def _forged_binding(kind, resolution) -> RiskEvidenceKindBinding:
    forged = object.__new__(RiskEvidenceKindBinding)
    object.__setattr__(forged, "kind", kind)
    object.__setattr__(forged, "resolution", resolution)
    return forged


# --- structure ---------------------------------------------------------------

def test_decoded_fields_are_exactly_binding_and_fact() -> None:
    assert [f.name for f in dataclasses.fields(DecodedPlannedContributionRiskEvidence)] == ["binding", "fact"]


def test_decoded_is_frozen() -> None:
    decoded = DecodedPlannedContributionRiskEvidence(binding=_binding(_content()), fact=_fact())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decoded.fact = _fact("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "attr",
    ["content", "payload", "raw_content", "digest", "context", "kind", "schema_version", "owner_id",
     "portfolio", "contribution", "score", "capacity", "weight", "level", "threshold", "suitability",
     "overall_risk", "required_risk", "decode"],
)
def test_no_extra_surface(attr: str) -> None:
    decoded = DecodedPlannedContributionRiskEvidence(binding=_binding(_content()), fact=_fact())
    assert not hasattr(decoded, attr)


def test_no_raw_content_retained() -> None:
    content = _content()
    decoded = resolve_planned_contribution_risk_evidence(binding=_binding(content), content=content)
    assert content not in repr(decoded).encode()
    assert content not in repr(vars(decoded)).encode()
    assert not any(isinstance(v, (bytes, bytearray, memoryview)) for v in vars(decoded).values())


def test_resolver_signature_is_keyword_only_without_defaults() -> None:
    params = inspect.signature(resolve_planned_contribution_risk_evidence).parameters
    assert list(params) == ["binding", "content"]
    for p in params.values():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty


# --- happy paths -------------------------------------------------------------

def test_full_chain_resolves_and_preserves_identity() -> None:
    content = _content()
    binding = _binding(content)
    result = resolve_planned_contribution_risk_evidence(binding=binding, content=content)
    assert type(result) is DecodedPlannedContributionRiskEvidence
    assert result.binding is binding
    assert result.binding.resolution is binding.resolution
    assert result.binding.resolution.pit_binding is binding.resolution.pit_binding
    assert result.fact == decode_planned_contribution_risk_fact(content)
    assert result.fact == _fact()


@pytest.mark.parametrize("status", list(ContributionStatus))
@pytest.mark.parametrize("mode", list(PortfolioMode))
def test_all_statuses_and_modes_resolve_and_are_preserved(status, mode) -> None:
    content = _content(status=status, mode=mode)
    result = resolve_planned_contribution_risk_evidence(binding=_binding(content), content=content)
    assert result.fact.status is status
    assert result.fact.mode is mode


@pytest.mark.parametrize("goal,bucket", [(None, None), (_G, None), (None, _B), (_G, _B)])
def test_link_combinations_resolve_without_reference_checks(goal, bucket) -> None:
    content = _content(goal_id=goal, cash_bucket_id=bucket)
    result = resolve_planned_contribution_risk_evidence(binding=_binding(content), content=content)
    assert result.fact.goal_id == goal
    assert result.fact.cash_bucket_id == bucket


def test_equivalent_decimal_spelling_in_fact_matches_canonical_digest() -> None:
    binding = _binding(_content("1000"))
    decoded = DecodedPlannedContributionRiskEvidence(binding=binding, fact=_fact("1000.00"))
    assert decoded.fact.amount == Decimal("1000")


# --- missing branch ----------------------------------------------------------

def test_missing_binding_returned_by_identity() -> None:
    binding = _missing_binding()
    result = resolve_planned_contribution_risk_evidence(binding=binding, content=None)
    assert result is binding
    assert type(result.resolution) is MissingRiskEvidence


@pytest.mark.parametrize("content", [b"", b"abc", _content(), 0, "", _Hostile(), bytearray(b"x")])
def test_missing_binding_with_content_rejected(content) -> None:
    with pytest.raises(ValueError, match=_BRANCH_MSG):
        resolve_planned_contribution_risk_evidence(binding=_missing_binding(), content=content)


# --- present branch ----------------------------------------------------------

def test_present_binding_with_none_rejected() -> None:
    with pytest.raises(ValueError, match=_BRANCH_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(_content()), content=None)


def test_empty_bytes_is_content_not_missing() -> None:
    with pytest.raises(ValueError, match=_JSON_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(_content()), content=b"")


@pytest.mark.parametrize("bad", [bytearray(b"x"), memoryview(b"x"), "x", 1, object()])
def test_invalid_content_type_propagates(bad) -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(_content()), content=bad)


# --- same-axis kind closure ---------------------------------------------------

@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_wrong_declared_kind_rejected_for_content_branch(kind: RiskEvidenceKind) -> None:
    content = _content()
    assert kind.axis is PLANNED.axis
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(content, kind), content=content)


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_wrong_declared_kind_rejected_for_missing_branch(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_planned_contribution_risk_evidence(binding=_missing_binding(kind), content=None)


def test_wrong_kind_fails_before_content_inspection() -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_planned_contribution_risk_evidence(
            binding=_binding(_content(), RiskEvidenceKind.CASH_BALANCE), content=_Hostile()  # type: ignore[arg-type]
        )


def _envelope_bytes(**overrides) -> bytes:
    envelope = json.loads(_content())
    envelope.update(overrides)
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("kind", ["cash_balance", "investment_goal", "PLANNED_CONTRIBUTION"])
def test_planned_binding_over_other_envelope_kind_fails_at_decoder(kind: str) -> None:
    raw = _envelope_bytes(kind=kind)
    binding = _binding(raw)  # the digest stage accepts these exact bytes
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        resolve_planned_contribution_risk_evidence(binding=binding, content=raw)


def test_wrong_schema_version_fails_at_decoder() -> None:
    raw = _envelope_bytes(schema_version=2)
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(raw), content=raw)


def test_noncanonical_bytes_fail_at_decoder_even_when_digest_matches() -> None:
    raw = json.dumps(json.loads(_content()), sort_keys=True).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(raw), content=raw)


def test_oversized_content_fails_at_decoder_even_when_digest_matches() -> None:
    raw = b" " * 4097
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        resolve_planned_contribution_risk_evidence(binding=_binding(raw), content=raw)


# --- caller-content substitution ---------------------------------------------

def test_valid_different_amount_fails_digest() -> None:
    binding = _binding(_content("1000"))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_planned_contribution_risk_evidence(binding=binding, content=_content("2000"))


def test_valid_different_mode_fails_digest() -> None:
    binding = _binding(_content(mode=PortfolioMode.MY_PORTFOLIO))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_planned_contribution_risk_evidence(
            binding=binding, content=_content(mode=PortfolioMode.SANDBOX)
        )


@pytest.mark.parametrize("other", [ContributionStatus.CANCELLED, ContributionStatus.RECEIVED,
                                   ContributionStatus.CONFIRMED])
def test_valid_different_status_fails_digest(other: ContributionStatus) -> None:
    binding = _binding(_content(status=ContributionStatus.PLANNED))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_planned_contribution_risk_evidence(binding=binding, content=_content(status=other))


# --- manual wrapper forgery --------------------------------------------------

def test_direct_forged_pair_fails_digest() -> None:
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_binding(_content("1000")), fact=_fact("5000"))


@pytest.mark.parametrize(
    "override",
    [
        {"mode": PortfolioMode.SANDBOX},
        {"contribution_id": UUID(int=9)},
        {"goal_id": None},
        {"goal_id": UUID(int=9)},
        {"cash_bucket_id": _B},
        {"expected_date": date(2026, 10, 2)},
        {"amount": Decimal("1000.5")},
        {"currency": Currency.USD},
        {"status": ContributionStatus.RECEIVED},
        {"portfolio_id": UUID(int=9)},
    ],
)
def test_every_fact_field_is_bound_to_the_digest(override) -> None:
    binding = _binding(_content())
    forged = _fact(**{("amount" if k == "amount" else k): (str(v) if k == "amount" else v)
                      for k, v in override.items()})
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedPlannedContributionRiskEvidence(binding=binding, fact=forged)


def test_decoded_rejects_missing_binding() -> None:
    with pytest.raises(ValueError, match=_PRESENT_REQUIRED_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_missing_binding(), fact=_fact())


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_decoded_rejects_wrong_kind(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_binding(_content(), kind), fact=_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_decoded_rejects_invalid_binding(bad) -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedPlannedContributionRiskEvidence(binding=bad, fact=_fact())  # type: ignore[arg-type]


def test_decoded_rejects_binding_subclass() -> None:
    sub = _BindingSub(kind=PLANNED, resolution=_content_match(_content()))
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedPlannedContributionRiskEvidence(binding=sub, fact=_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_decoded_rejects_invalid_fact(bad) -> None:
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_binding(_content()), fact=bad)  # type: ignore[arg-type]


def test_decoded_rejects_fact_subclass() -> None:
    sub = _FactSub(portfolio_id=_P, mode=PortfolioMode.MY_PORTFOLIO, contribution_id=_C, goal_id=_G,
                   cash_bucket_id=None, expected_date=date(2026, 10, 1), amount=Decimal("1000"),
                   currency=Currency.TRY, status=ContributionStatus.PLANNED)
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_binding(_content()), fact=sub)


def test_decoded_validation_order() -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedPlannedContributionRiskEvidence(binding=None, fact=_Hostile())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=_KIND_MSG):
        DecodedPlannedContributionRiskEvidence(
            binding=_missing_binding(RiskEvidenceKind.CASH_BALANCE), fact=_Hostile()  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match=_PRESENT_REQUIRED_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_missing_binding(), fact=_Hostile())  # type: ignore[arg-type]


def test_encoder_resource_errors_propagate_unchanged() -> None:
    huge = _fact("1E+999999999")
    with pytest.raises(ValueError, match=_SIZE_AMOUNT_MSG):
        DecodedPlannedContributionRiskEvidence(binding=_binding(_content()), fact=huge)


# --- resolver strictness ------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_resolver_rejects_invalid_binding_before_content(bad) -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        resolve_planned_contribution_risk_evidence(binding=bad, content=_Hostile())  # type: ignore[arg-type]


def test_resolver_rejects_binding_subclass() -> None:
    content = _content()
    sub = _BindingSub(kind=PLANNED, resolution=_content_match(content))
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        resolve_planned_contribution_risk_evidence(binding=sub, content=content)


@pytest.mark.parametrize("resolution", [None, "x", object(), _Hostile(), _HostileMetaInstance()])
def test_resolver_rejects_forged_resolution_type(resolution) -> None:
    forged = _forged_binding(PLANNED, resolution)
    with pytest.raises(TypeError, match=_RESOLUTION_MSG):
        resolve_planned_contribution_risk_evidence(binding=forged, content=_Hostile())  # type: ignore[arg-type]


def test_digest_error_not_translated() -> None:
    with pytest.raises(ValueError) as info:
        resolve_planned_contribution_risk_evidence(binding=_binding(_content("1")), content=_content("2"))
    assert str(info.value) == "content digest mismatch"


# --- purity / scope ----------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_no_hashing_or_impure_imports() -> None:
    imports = _imports()
    for banned in ("hashlib", "hmac", "os", "pathlib", "random", "secrets", "json"):
        assert banned not in imports
    assert not any(m.startswith("backend.engine.private.portfolio") for m in imports)
    assert not any("persistence" in m or "repository" in m for m in imports)
    assert "backend.engine.private.risk_evidence_cash_schema" not in imports
    assert "backend.engine.private.risk_evidence_cash_resolution" not in imports


def test_no_generic_or_adapter_types_defined() -> None:
    for name in ("DecodedRiskEvidence", "RiskAssessmentFact", "GenericEvidenceResolution", "RiskFactProtocol",
                 "from_planned_contribution", "build_planned_contribution_risk_fact"):
        assert not hasattr(module_under_test, name)
