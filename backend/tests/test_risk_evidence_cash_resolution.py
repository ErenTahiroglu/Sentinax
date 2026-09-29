"""
backend/tests/test_risk_evidence_cash_resolution.py
===================================================
Tests for decoded CASH_BALANCE evidence resolution (Phase 15C.4).

Composition of: declared kind (15C.2 binding) + canonical CASH_BALANCE v1 schema (15C.3)
+ provenance digest revalidation (15B.8). Missing stays typed absence; present output keeps
the original binding and a typed fact, never raw bytes.
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

from backend.engine.private import risk_evidence_cash_resolution as module_under_test
from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Currency, Horizon, PortfolioMode, RiskAxis, RiskEvidenceKind
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_cash_resolution import (
    DecodedCashBalanceRiskEvidence,
    resolve_cash_balance_risk_evidence,
)
from backend.engine.private.risk_evidence_cash_schema import (
    CashBalanceRiskFact,
    decode_cash_balance_risk_fact,
    encode_cash_balance_risk_fact,
)
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef

_BINDING_TYPE_MSG = r"^binding must be an exact RiskEvidenceKindBinding instance$"
_KIND_MSG = r"^binding kind must be CASH_BALANCE$"
_PRESENT_REQUIRED_MSG = r"^decoded cash balance evidence requires content-matched evidence$"
_FACT_TYPE_MSG = r"^fact must be an exact CashBalanceRiskFact instance$"
_BRANCH_MSG = r"^content must be supplied exactly for the content-matched cash balance branch$"
_RESOLUTION_MSG = (
    r"^binding resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance$"
)
_DIGEST_MSG = r"^content digest mismatch$"
_ENVELOPE_MSG = r"^cash balance risk evidence envelope is invalid$"
_CANON_MSG = r"^cash balance risk evidence content is not canonical$"
_JSON_MSG = r"^cash balance risk evidence content must be valid canonical UTF-8 JSON$"
_SIZE_CONTENT_MSG = r"^cash balance risk evidence content exceeds maximum supported size$"
_BYTES_MSG = r"^content must be exact bytes$"

_P = UUID("11111111-1111-4111-8111-111111111111")
_A = UUID("22222222-2222-4222-8222-222222222222")
_T = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)


def _fact(balance: str = "100", **overrides) -> CashBalanceRiskFact:
    kwargs = dict(
        portfolio_id=_P,
        account_id=_A,
        currency=Currency.TRY,
        balance=Decimal(balance),
        mode=PortfolioMode.MY_PORTFOLIO,
        as_of_recorded_at=_T,
    )
    kwargs.update(overrides)
    return CashBalanceRiskFact(**kwargs)


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
            source_key="test.cash", content_sha256=hashlib.sha256(content).hexdigest()
        ),
        available_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc),
    )
    pit = RiskEvidencePITBinding(context=_context(), availability_ref=availability)
    return RiskEvidenceContentMatch(pit_binding=pit, content=content)


def _binding(content: bytes, kind: RiskEvidenceKind = RiskEvidenceKind.CASH_BALANCE) -> RiskEvidenceKindBinding:
    return RiskEvidenceKindBinding(kind=kind, resolution=_content_match(content))


def _missing_binding(kind: RiskEvidenceKind = RiskEvidenceKind.CASH_BALANCE) -> RiskEvidenceKindBinding:
    missing = MissingRiskEvidence(context=_context(), missing_inputs=("cash_snapshot",))
    return RiskEvidenceKindBinding(kind=kind, resolution=missing)


def _content(balance: str = "100", **overrides) -> bytes:
    return encode_cash_balance_risk_fact(_fact(balance, **overrides))


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


class _FactSub(CashBalanceRiskFact):
    pass


def _forged_binding(kind, resolution) -> RiskEvidenceKindBinding:
    forged = object.__new__(RiskEvidenceKindBinding)
    object.__setattr__(forged, "kind", kind)
    object.__setattr__(forged, "resolution", resolution)
    return forged


# --- structure ---------------------------------------------------------------

def test_decoded_fields_are_exactly_binding_and_fact() -> None:
    assert [f.name for f in dataclasses.fields(DecodedCashBalanceRiskEvidence)] == ["binding", "fact"]


def test_decoded_is_frozen() -> None:
    content = _content()
    decoded = DecodedCashBalanceRiskEvidence(binding=_binding(content), fact=_fact())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decoded.fact = _fact("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "attr",
    ["content", "payload", "digest", "context", "kind", "schema_version", "owner_id", "score",
     "level", "weight", "threshold", "suitability", "overall_risk", "required_risk"],
)
def test_no_extra_surface(attr: str) -> None:
    decoded = DecodedCashBalanceRiskEvidence(binding=_binding(_content()), fact=_fact())
    assert not hasattr(decoded, attr)


def test_no_raw_content_retained() -> None:
    content = _content()
    decoded = resolve_cash_balance_risk_evidence(binding=_binding(content), content=content)
    assert content not in repr(decoded).encode()
    assert content not in repr(vars(decoded)).encode()
    assert not any(isinstance(v, (bytes, bytearray, memoryview)) for v in vars(decoded).values())


def test_resolver_signature_is_keyword_only_without_defaults() -> None:
    params = inspect.signature(resolve_cash_balance_risk_evidence).parameters
    assert list(params) == ["binding", "content"]
    for p in params.values():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty


# --- happy path --------------------------------------------------------------

def test_full_chain_resolves_and_preserves_identity() -> None:
    content = _content()
    binding = _binding(content)
    result = resolve_cash_balance_risk_evidence(binding=binding, content=content)
    assert type(result) is DecodedCashBalanceRiskEvidence
    assert result.binding is binding
    assert result.binding.resolution is binding.resolution
    assert result.binding.resolution.pit_binding is binding.resolution.pit_binding
    assert result.fact == decode_cash_balance_risk_fact(content)
    assert result.fact == _fact()


def test_zero_balance_is_present_evidence() -> None:
    content = _content("0")
    result = resolve_cash_balance_risk_evidence(binding=_binding(content), content=content)
    assert type(result) is DecodedCashBalanceRiskEvidence
    assert result.fact.balance == Decimal("0")


def test_sandbox_mode_preserved_not_interpreted() -> None:
    content = _content(mode=PortfolioMode.SANDBOX, as_of_recorded_at=None)
    result = resolve_cash_balance_risk_evidence(binding=_binding(content), content=content)
    assert result.fact.mode is PortfolioMode.SANDBOX
    assert result.fact.as_of_recorded_at is None


def test_equivalent_decimal_spelling_in_fact_still_matches_canonical_digest() -> None:
    binding = _binding(_content("100"))
    decoded = DecodedCashBalanceRiskEvidence(binding=binding, fact=_fact("100.00"))
    assert decoded.fact.balance == Decimal("100")


# --- missing branch ----------------------------------------------------------

def test_missing_binding_returned_by_identity() -> None:
    binding = _missing_binding()
    result = resolve_cash_balance_risk_evidence(binding=binding, content=None)
    assert result is binding
    assert type(result.resolution) is MissingRiskEvidence


@pytest.mark.parametrize("content", [b"", b"abc", _content(), 0, "", _Hostile(), bytearray(b"x")])
def test_missing_binding_with_content_rejected(content) -> None:
    with pytest.raises(ValueError, match=_BRANCH_MSG):
        resolve_cash_balance_risk_evidence(binding=_missing_binding(), content=content)


# --- present branch ----------------------------------------------------------

def test_present_binding_with_none_rejected() -> None:
    with pytest.raises(ValueError, match=_BRANCH_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(_content()), content=None)


def test_empty_bytes_is_content_not_missing() -> None:
    with pytest.raises(ValueError, match=_JSON_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(_content()), content=b"")


@pytest.mark.parametrize("bad", [bytearray(b"x"), memoryview(b"x"), "x", 1, object()])
def test_present_binding_invalid_content_type_propagates(bad) -> None:
    with pytest.raises(TypeError, match=_BYTES_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(_content()), content=bad)


# --- same-axis mislabeling closure -------------------------------------------

@pytest.mark.parametrize("kind", [RiskEvidenceKind.INVESTMENT_GOAL, RiskEvidenceKind.PLANNED_CONTRIBUTION])
def test_wrong_declared_kind_rejected_for_content_branch(kind: RiskEvidenceKind) -> None:
    content = _content()
    assert kind.axis is RiskEvidenceKind.CASH_BALANCE.axis
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(content, kind), content=content)


@pytest.mark.parametrize("kind", [RiskEvidenceKind.INVESTMENT_GOAL, RiskEvidenceKind.PLANNED_CONTRIBUTION])
def test_wrong_declared_kind_rejected_for_missing_branch(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_cash_balance_risk_evidence(binding=_missing_binding(kind), content=None)


def test_wrong_kind_fails_before_content_inspection() -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        resolve_cash_balance_risk_evidence(
            binding=_binding(_content(), RiskEvidenceKind.INVESTMENT_GOAL), content=_Hostile()  # type: ignore[arg-type]
        )


def _envelope_bytes(**overrides) -> bytes:
    envelope = json.loads(_content())
    envelope.update(overrides)
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_cash_binding_over_non_cash_envelope_fails_at_decoder() -> None:
    goal_like = _envelope_bytes(kind="investment_goal")
    binding = _binding(goal_like)  # digest stage accepts these exact bytes
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        resolve_cash_balance_risk_evidence(binding=binding, content=goal_like)


def test_wrong_schema_version_bytes_fail_at_decoder() -> None:
    raw = _envelope_bytes(schema_version=2)
    with pytest.raises(ValueError, match=_ENVELOPE_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(raw), content=raw)


def test_noncanonical_bytes_fail_at_decoder_even_when_digest_matches() -> None:
    raw = json.dumps(json.loads(_content()), sort_keys=True).encode("utf-8")  # extra whitespace
    with pytest.raises(ValueError, match=_CANON_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(raw), content=raw)


def test_oversized_content_fails_at_decoder_even_when_digest_matches() -> None:
    raw = b" " * 4097
    with pytest.raises(ValueError, match=_SIZE_CONTENT_MSG):
        resolve_cash_balance_risk_evidence(binding=_binding(raw), content=raw)


# --- caller-content substitution ---------------------------------------------

def test_valid_but_different_canonical_content_fails_digest() -> None:
    binding = _binding(_content("100"))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        resolve_cash_balance_risk_evidence(binding=binding, content=_content("200"))


# --- manual wrapper forgery --------------------------------------------------

def test_direct_forged_pairing_fails_digest() -> None:
    binding = _binding(_content("100"))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedCashBalanceRiskEvidence(binding=binding, fact=_fact("200"))


@pytest.mark.parametrize("field,value", [("currency", Currency.USD), ("mode", PortfolioMode.SANDBOX),
                                         ("as_of_recorded_at", None), ("account_id", UUID(int=7))])
def test_any_fact_field_difference_fails_digest(field: str, value) -> None:
    binding = _binding(_content("100"))
    with pytest.raises(ValueError, match=_DIGEST_MSG):
        DecodedCashBalanceRiskEvidence(binding=binding, fact=_fact("100", **{field: value}))


def test_decoded_rejects_missing_binding() -> None:
    with pytest.raises(ValueError, match=_PRESENT_REQUIRED_MSG):
        DecodedCashBalanceRiskEvidence(binding=_missing_binding(), fact=_fact())


@pytest.mark.parametrize("kind", [RiskEvidenceKind.INVESTMENT_GOAL, RiskEvidenceKind.PLANNED_CONTRIBUTION])
def test_decoded_rejects_wrong_kind(kind: RiskEvidenceKind) -> None:
    with pytest.raises(ValueError, match=_KIND_MSG):
        DecodedCashBalanceRiskEvidence(binding=_binding(_content(), kind), fact=_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_decoded_rejects_invalid_binding(bad) -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedCashBalanceRiskEvidence(binding=bad, fact=_fact())  # type: ignore[arg-type]


def test_decoded_rejects_binding_subclass() -> None:
    content = _content()
    sub = _BindingSub(kind=RiskEvidenceKind.CASH_BALANCE, resolution=_content_match(content))
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedCashBalanceRiskEvidence(binding=sub, fact=_fact())


@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_decoded_rejects_invalid_fact(bad) -> None:
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        DecodedCashBalanceRiskEvidence(binding=_binding(_content()), fact=bad)  # type: ignore[arg-type]


def test_decoded_rejects_fact_subclass() -> None:
    sub = _FactSub(portfolio_id=_P, account_id=_A, currency=Currency.TRY, balance=Decimal("100"),
                   mode=PortfolioMode.MY_PORTFOLIO, as_of_recorded_at=_T)
    with pytest.raises(TypeError, match=_FACT_TYPE_MSG):
        DecodedCashBalanceRiskEvidence(binding=_binding(_content()), fact=sub)


def test_decoded_validation_order() -> None:
    # binding type before everything
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        DecodedCashBalanceRiskEvidence(binding=None, fact=_Hostile())  # type: ignore[arg-type]
    # kind before resolution/fact
    with pytest.raises(ValueError, match=_KIND_MSG):
        DecodedCashBalanceRiskEvidence(
            binding=_missing_binding(RiskEvidenceKind.INVESTMENT_GOAL), fact=_Hostile()  # type: ignore[arg-type]
        )
    # resolution branch before fact
    with pytest.raises(ValueError, match=_PRESENT_REQUIRED_MSG):
        DecodedCashBalanceRiskEvidence(binding=_missing_binding(), fact=_Hostile())  # type: ignore[arg-type]


def test_encoder_resource_errors_propagate_unchanged() -> None:
    huge = _fact("1E+999999999")
    with pytest.raises(ValueError, match=r"^balance canonical representation exceeds maximum supported size$"):
        DecodedCashBalanceRiskEvidence(binding=_binding(_content()), fact=huge)


# --- resolver strictness ------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "x", {}, 1, object(), _Hostile(), _HostileMetaInstance()])
def test_resolver_rejects_invalid_binding_before_content(bad) -> None:
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        resolve_cash_balance_risk_evidence(binding=bad, content=_Hostile())  # type: ignore[arg-type]


def test_resolver_rejects_binding_subclass() -> None:
    content = _content()
    sub = _BindingSub(kind=RiskEvidenceKind.CASH_BALANCE, resolution=_content_match(content))
    with pytest.raises(TypeError, match=_BINDING_TYPE_MSG):
        resolve_cash_balance_risk_evidence(binding=sub, content=content)


@pytest.mark.parametrize("resolution", [None, "x", object(), _Hostile(), _HostileMetaInstance()])
def test_resolver_rejects_forged_resolution_type(resolution) -> None:
    forged = _forged_binding(RiskEvidenceKind.CASH_BALANCE, resolution)
    with pytest.raises(TypeError, match=_RESOLUTION_MSG):
        resolve_cash_balance_risk_evidence(binding=forged, content=_Hostile())  # type: ignore[arg-type]


def test_digest_error_not_translated() -> None:
    with pytest.raises(ValueError) as info:
        resolve_cash_balance_risk_evidence(binding=_binding(_content("1")), content=_content("2"))
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


def test_no_generic_fact_types_defined() -> None:
    for name in ("RiskAssessmentFact", "GenericRiskFact", "DecodedRiskEvidence"):
        assert not hasattr(module_under_test, name)
