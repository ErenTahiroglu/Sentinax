"""
backend/tests/test_risk_evidence_investment_goal_resolution.py
==============================================================
Tests for decoded INVESTMENT_GOAL evidence resolution (Phase 15C.7).

Composition of: declared kind (15C.2 binding) + canonical INVESTMENT_GOAL v1 schema + provenance digest
revalidation (15B.8). Proves canonical bytes + declared kind + PIT/digest integrity ONLY; it does not prove
authoritative Portfolio.mode, repository existence, or owner access.
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

from backend.engine.private import risk_evidence_investment_goal_resolution as module_under_test
from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import (
    AsOfMode, Currency, GoalPriority, GoalStatus, Horizon, PortfolioMode, RiskAxis, RiskEvidenceKind,
)
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_investment_goal_resolution import (
    DecodedInvestmentGoalRiskEvidence,
    resolve_investment_goal_risk_evidence,
)
from backend.engine.private.risk_evidence_investment_goal_schema import (
    InvestmentGoalRiskFact,
    decode_investment_goal_risk_fact,
    encode_investment_goal_risk_fact,
)
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef

_BINDING_TYPE_MSG = r"^binding must be an exact RiskEvidenceKindBinding instance$"
_KIND_MSG = r"^binding kind must be INVESTMENT_GOAL$"
_PRESENT_REQUIRED_MSG = r"^decoded investment goal evidence requires content-matched evidence$"
_FACT_TYPE_MSG = r"^fact must be an exact InvestmentGoalRiskFact instance$"
_BRANCH_MSG = r"^content must be supplied exactly for the content-matched investment goal branch$"
_RESOLUTION_MSG = (
    r"^binding resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance$"
)
_DIGEST_MSG = r"^content digest mismatch$"
_ENVELOPE_MSG = r"^investment goal risk evidence envelope is invalid$"
_CANON_MSG = r"^investment goal risk evidence content is not canonical$"
_JSON_MSG = r"^investment goal risk evidence content must be valid canonical UTF-8 JSON$"
_SIZE_CONTENT_MSG = r"^investment goal risk evidence content exceeds maximum supported size$"
_SIZE_AMOUNT_MSG = r"^target amount canonical representation exceeds maximum supported size$"
_BYTES_MSG = r"^content must be exact bytes$"

_P = UUID("11111111-1111-4111-8111-111111111111")
_G = UUID("33333333-3333-4333-8333-333333333333")
_T = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)

OTHER_KINDS = [RiskEvidenceKind.CASH_BALANCE, RiskEvidenceKind.PLANNED_CONTRIBUTION]
GOAL = RiskEvidenceKind.INVESTMENT_GOAL


def _fact(amount: str = "100000", **overrides) -> InvestmentGoalRiskFact:
    kwargs = dict(
        portfolio_id=_P,
        mode=PortfolioMode.MY_PORTFOLIO,
        goal_id=_G,
        target_amount=Decimal(amount),
        target_currency=Currency.TRY,
        target_date=date(2027, 7, 1),
        priority=GoalPriority.MEDIUM,
        status=GoalStatus.ACTIVE,
        archived_at=None,
    )
    kwargs.update(overrides)
    return InvestmentGoalRiskFact(**kwargs)


def _content(amount: str = "100000", **overrides) -> bytes:
    return encode_investment_goal_risk_fact(_fact(amount, **overrides))


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
            source_key="test.goal", content_sha256=hashlib.sha256(content).hexdigest()
        ),
        available_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc),
    )
    pit = RiskEvidencePITBinding(context=_context(), availability_ref=availability)
    return RiskEvidenceContentMatch(pit_binding=pit, content=content)


def _binding(content: bytes, kind: RiskEvidenceKind = GOAL) -> RiskEvidenceKindBinding:
    return RiskEvidenceKindBinding(kind=kind, resolution=_content_match(content))


def _missing_binding(kind: RiskEvidenceKind = GOAL) -> RiskEvidenceKindBinding:
    missing = MissingRiskEvidence(context=_context(), missing_inputs=("goal_snapshot",))
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


class _FactSub(InvestmentGoalRiskFact):
    pass


def _forged_binding(kind, resolution) -> RiskEvidenceKindBinding:
    forged = object.__new__(RiskEvidenceKindBinding)
    object.__setattr__(forged, "kind", kind)
    object.__setattr__(forged, "resolution", resolution)
    return forged


# --- structure ---------------------------------------------------------------

def test_decoded_fields_are_exactly_binding_and_fact() -> None:
    assert [f.name for f in dataclasses.fields(DecodedInvestmentGoalRiskEvidence)] == ["binding", "fact"]


def test_decoded_is_frozen() -> None:
    decoded = DecodedInvestmentGoalRiskEvidence(binding=_binding(_content()), fact=_fact())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decoded.fact = _fact("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "attr",
    ["content", "payload", "raw_content", "digest", "context", "kind", "schema_version", "owner_id",
     "portfolio", "goal", "score", "capacity", "weight", "level", "threshold", "suitability",
     "overall_risk", "required_risk", "decode"],
)
def test_no_extra_surface(attr: str) -> None:
    decoded = DecodedInvestmentGoalRiskEvidence(binding=_binding(_content()), fact=_fact())
    assert not hasattr(decoded, attr)


def test_no_raw_content_retained() -> None:
    content = _content()
    decoded = resolve_investment_goal_risk_evidence(binding=_binding(content), content=content)
    assert content not in repr(decoded).encode()
    assert content not in repr(vars(decoded)).encode()
    assert not any(isinstance(v, (bytes, bytearray, memoryview)) for v in vars(decoded).values())


def test_resolver_signature_is_keyword_only_without_defaults() -> None:
    params = inspect.signature(resolve_investment_goal_risk_evidence).parameters
    assert list(params) == ["binding", "content"]
    for p in params.values():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty


# --- happy paths -------------------------------------------------------------

def test_full_chain_resolves_and_preserves_identity() -> None:
    content = _content()
    binding = _binding(content)
    result = resolve_investment_goal_risk_evidence(binding=binding, content=content)
    assert type(result) is DecodedInvestmentGoalRiskEvidence
    assert result.binding is binding
    assert result.binding.resolution is binding.resolution
    assert result.binding.resolution.pit_binding is binding.resolution.pit_binding
    assert result.fact == decode_investment_goal_risk_fact(content)
    assert result.fact == _fact()


@pytest.mark.parametrize("status", list(GoalStatus))
@pytest.mark.parametrize("mode", list(PortfolioMode))
@pytest.mark.parametrize("priority", list(GoalPriority))
def test_all_statuses_modes_priorities_resolve_and_are_preserved(status, mode, priority) -> None:
    content = _content(status=status, mode=mode, priority=priority)
    result = resolve_investment_goal_risk_evidence(binding=_binding(content), content=content)
    assert (result.fact.status, result.fact.mode, result.fact.priority) == (status, mode, priority)


@pytest.mark.parametrize("target_date", [None, date(2027, 7, 1)])
@pytest.mark.parametrize("archived_at", [None, _T])
def test_optional_fields_resolve(target_date, archived_at) -> None:
    content = _content(target_date=target_date, archived_at=archived_at)
    result = resolve_investment_goal_risk_evidence(binding=_binding(content), content=content)
    assert result.fact.target_date == target_date
    assert result.fact.archived_at == archived_at


def test_equivalent_decimal_spelling_in_fact_matches_canonical_digest() -> None:
    binding = _binding(_content("100000"))
    decoded = DecodedInvestmentGoalRiskEvidence(binding=binding, fact=_fact("100000.00"))
    assert decoded.fact.target_amount == Decimal("100000")


# --- missing branch ----------------------------------------------------------

def test_missing_binding_returned_by_identity() -> None:
    binding = _missing_binding()
    result = resolve_investment_goal_risk_evidence(binding=binding, content=None)
    assert result is binding
    assert type(result.resolution) is MissingRiskEvidence


@pytest.mark.parametrize("content", [b"", b"abc", _content(), 0, "", _Hostile(), bytearray(b"x")])
def test_missing_binding_with_content_rejected(content) -> None:
    with pytest.raises(ValueError, match=_BRANCH_MSG):
        resolve_investment_goal_risk_evidence(binding=_missing_binding(), content=content)


# --- present branch ----------------------------------------------------------

def test_present_binding_with_none_rejected() -> None:
    with pytest.raises(ValueError, match=_BRANCH_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(_content()), content=None)


def test_empty_bytes_is_content_not_missing() -> None:
    with pytest.raises(ValueError, match=_JSON_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(_content()), content=b"")


@pytest.mark.parametrize("bad", [bytearray(b"x"), memoryview(b"x"), "x", 1, object()])
def test_invalid_content_type_propagates(bad) -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(_content()), content=bad)


# --- same-axis kind closure ---------------------------------------------------

@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_wrong_declared_kind_rejected_for_content_branch(kind: RiskEvidenceKind) -> None:
    content = _content()
    assert kind.axis is GOAL.axis
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(content, kind), content=content)


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_wrong_declared_kind_rejected_for_missing_branch(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_investment_goal_risk_evidence(binding=_missing_binding(kind), content=None)


def test_wrong_kind_fails_before_content_inspection() -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_investment_goal_risk_evidence(
            binding=_binding(_content(), RiskEvidenceKind.CASH_BALANCE), content=_Hostile()  # type: ignore[arg-type]
        )


def _envelope_bytes(**overrides) -> bytes:
    envelope = json.loads(_content())
    envelope.update(overrides)
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("kind", ["cash_balance", "planned_contribution", "INVESTMENT_GOAL"])
def test_goal_binding_over_other_envelope_kind_fails_at_decoder(kind: str) -> None:
    raw = _envelope_bytes(kind=kind)
    binding = _binding(raw)  # the digest stage accepts these exact bytes
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        resolve_investment_goal_risk_evidence(binding=binding, content=raw)


def test_wrong_schema_version_fails_at_decoder() -> None:
    raw = _envelope_bytes(schema_version=2)
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(raw), content=raw)


def test_noncanonical_bytes_fail_at_decoder_even_when_digest_matches() -> None:
    raw = json.dumps(json.loads(_content()), sort_keys=True).encode("utf-8")
    with pytest.raises(ValueError, match=_CANON_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(raw), content=raw)


def test_oversized_content_fails_at_decoder_even_when_digest_matches() -> None:
    raw = b" " * 4097
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        resolve_investment_goal_risk_evidence(binding=_binding(raw), content=raw)


# --- caller-content substitution ---------------------------------------------

def test_valid_different_amount_fails_digest() -> None:
    binding = _binding(_content("100000"))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_investment_goal_risk_evidence(binding=binding, content=_content("200000"))


def test_valid_different_mode_fails_digest() -> None:
    binding = _binding(_content(mode=PortfolioMode.MY_PORTFOLIO))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_investment_goal_risk_evidence(binding=binding, content=_content(mode=PortfolioMode.SANDBOX))


@pytest.mark.parametrize("other", [GoalStatus.CANCELLED, GoalStatus.COMPLETED, GoalStatus.PAUSED])
def test_valid_different_status_fails_digest(other: GoalStatus) -> None:
    binding = _binding(_content(status=GoalStatus.ACTIVE))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_investment_goal_risk_evidence(binding=binding, content=_content(status=other))


def test_valid_different_priority_fails_digest() -> None:
    binding = _binding(_content(priority=GoalPriority.MEDIUM))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_investment_goal_risk_evidence(binding=binding, content=_content(priority=GoalPriority.CRITICAL))


def test_valid_different_archived_at_fails_digest() -> None:
    binding = _binding(_content(archived_at=None))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_investment_goal_risk_evidence(binding=binding, content=_content(archived_at=_T))


# --- manual wrapper forgery --------------------------------------------------

def test_direct_forged_pair_fails_digest() -> None:
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_binding(_content("100000")), fact=_fact("500000"))


@pytest.mark.parametrize(
    "override",
    [
        {"portfolio_id": UUID(int=9)},
        {"mode": PortfolioMode.SANDBOX},
        {"goal_id": UUID(int=9)},
        {"target_currency": Currency.USD},
        {"target_date": date(2027, 7, 2)},
        {"target_date": None},
        {"priority": GoalPriority.HIGH},
        {"status": GoalStatus.CANCELLED},
        {"archived_at": _T},
    ],
)
def test_every_fact_field_is_bound_to_the_digest(override) -> None:
    binding = _binding(_content())
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=binding, fact=_fact(**override))


def test_target_amount_is_bound_to_the_digest() -> None:
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_binding(_content()), fact=_fact("100000.5"))


def test_decoded_rejects_missing_binding() -> None:
    with pytest.raises(ValueError, match=_PRESENT_REQUIRED_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_missing_binding(), fact=_fact())


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_decoded_rejects_wrong_kind(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_binding(_content(), kind), fact=_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_decoded_rejects_invalid_binding(bad) -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=bad, fact=_fact())  # type: ignore[arg-type]


def test_decoded_rejects_binding_subclass() -> None:
    sub = _BindingSub(kind=GOAL, resolution=_content_match(_content()))
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=sub, fact=_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_decoded_rejects_invalid_fact(bad) -> None:
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_binding(_content()), fact=bad)  # type: ignore[arg-type]


def test_decoded_rejects_fact_subclass() -> None:
    sub = _FactSub(portfolio_id=_P, mode=PortfolioMode.MY_PORTFOLIO, goal_id=_G, target_amount=Decimal("100000"),
                   target_currency=Currency.TRY, target_date=date(2027, 7, 1), priority=GoalPriority.MEDIUM,
                   status=GoalStatus.ACTIVE, archived_at=None)
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_binding(_content()), fact=sub)


def test_decoded_validation_order() -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=None, fact=_Hostile())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=_KIND_MSG):
        DecodedInvestmentGoalRiskEvidence(
            binding=_missing_binding(RiskEvidenceKind.CASH_BALANCE), fact=_Hostile()  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match=_PRESENT_REQUIRED_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_missing_binding(), fact=_Hostile())  # type: ignore[arg-type]


def test_encoder_resource_errors_propagate_unchanged() -> None:
    with pytest.raises(ValueError, match=_SIZE_AMOUNT_MSG):
        DecodedInvestmentGoalRiskEvidence(binding=_binding(_content()), fact=_fact("1E+999999999"))


# --- resolver strictness ------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_resolver_rejects_invalid_binding_before_content(bad) -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        resolve_investment_goal_risk_evidence(binding=bad, content=_Hostile())  # type: ignore[arg-type]


def test_resolver_rejects_binding_subclass() -> None:
    content = _content()
    sub = _BindingSub(kind=GOAL, resolution=_content_match(content))
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        resolve_investment_goal_risk_evidence(binding=sub, content=content)


@pytest.mark.parametrize("resolution", [None, "x", object(), _Hostile(), _HostileMetaInstance()])
def test_resolver_rejects_forged_resolution_type(resolution) -> None:
    forged = _forged_binding(GOAL, resolution)
    with pytest.raises(TypeError, match=_RESOLUTION_MSG):
        resolve_investment_goal_risk_evidence(binding=forged, content=_Hostile())  # type: ignore[arg-type]


def test_digest_error_not_translated() -> None:
    with pytest.raises(ValueError) as info:
        resolve_investment_goal_risk_evidence(binding=_binding(_content("1")), content=_content("2"))
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
    for other in ("cash", "planned_contribution"):
        assert f"backend.engine.private.risk_evidence_{other}_schema" not in imports
        assert f"backend.engine.private.risk_evidence_{other}_resolution" not in imports


def test_no_generic_or_adapter_types_defined() -> None:
    for name in ("DecodedRiskEvidence", "RiskAssessmentFact", "GenericEvidenceResolution", "RiskFactProtocol",
                 "from_investment_goal", "build_investment_goal_risk_fact"):
        assert not hasattr(module_under_test, name)
