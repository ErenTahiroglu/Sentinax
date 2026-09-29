"""
backend/engine/private/risk_evidence_cash_resolution.py
=======================================================
Decoded CASH_BALANCE risk-evidence resolution for the Private Investment Decision Engine (Phase 15C.4).

Composes, for CASH_BALANCE only:
    - Phase 15C.2 declared kind binding (`RiskEvidenceKindBinding`),
    - Phase 15C.3 canonical CASH_BALANCE v1 schema (`CashBalanceRiskFact`, encode/decode),
    - Phase 15B.8 digest validation (`RiskEvidenceContentMatch`), the sole digest authority.

Architectural Invariants:
    - Pure domain value object + resolver. Zero clock, network, persistence, UUID generation, entropy,
      filesystem, database, or cache access. NO hashing here (no hashlib/hmac); digest validation is delegated
      to `RiskEvidenceContentMatch`. No portfolio/persistence/repository imports.
    - `DecodedCashBalanceRiskEvidence` stores exactly `binding` and `fact`. It retains no raw bytes and no
      digest/context/kind/schema copies. Its constructor is self-validating, so a caller cannot pair an
      unrelated fact with a binding by constructing the dataclass directly. Validation order:
      1. `type(binding) is RiskEvidenceKindBinding`
      2. `binding.kind is RiskEvidenceKind.CASH_BALANCE`
      3. `type(binding.resolution) is RiskEvidenceContentMatch`
      4. `type(fact) is CashBalanceRiskFact`
      5. canonical-encode the fact (15C.3 resource ceiling errors propagate unchanged)
      6. revalidate those canonical bytes against the binding's existing PIT provenance digest via a
         transient `RiskEvidenceContentMatch` (never retained; the original binding is never replaced).
    - `resolve_cash_balance_risk_evidence(*, binding, content)`:
      * missing branch (`MissingRiskEvidence` + `content is None`) returns the same binding object;
      * present branch (`RiskEvidenceContentMatch` + non-None content) decodes canonical CASH_BALANCE v1 bytes
        and builds the wrapper, which re-checks the canonical fact bytes against the bound digest.
      Therefore supplied content must be canonical CASH_BALANCE v1 bytes AND match the originally bound digest.
    - 15B.8 and 15C.3 errors propagate unchanged. Composition-owned errors are static strings.
    - Missing remains typed absence; `b""` is content (not missing); a zero balance is present known cash;
      `PortfolioMode.SANDBOX` is preserved, not interpreted.
    - No scoring, level, threshold, suitability, aggregation, required risk, overall risk, or owner binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.domain import RiskEvidenceKind
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_cash_schema import (
    CashBalanceRiskFact,
    decode_cash_balance_risk_fact,
    encode_cash_balance_risk_fact,
)
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding


def _require_cash_binding(binding: object) -> None:
    if type(binding) is not RiskEvidenceKindBinding:
        raise TypeError("binding must be an exact RiskEvidenceKindBinding instance")
    if binding.kind is not RiskEvidenceKind.CASH_BALANCE:
        raise ValueError("binding kind must be CASH_BALANCE")


@dataclass(frozen=True)
class DecodedCashBalanceRiskEvidence:
    """
    A content-matched CASH_BALANCE binding together with its decoded, digest-consistent typed fact.
    """
    binding: RiskEvidenceKindBinding
    fact: CashBalanceRiskFact

    def __post_init__(self) -> None:
        _require_cash_binding(self.binding)

        if type(self.binding.resolution) is not RiskEvidenceContentMatch:
            raise ValueError("decoded cash balance evidence requires content-matched evidence")

        if type(self.fact) is not CashBalanceRiskFact:
            raise TypeError("fact must be an exact CashBalanceRiskFact instance")

        canonical_content = encode_cash_balance_risk_fact(self.fact)
        # Phase 15B.8 remains the digest authority; the transient result is intentionally discarded.
        RiskEvidenceContentMatch(
            pit_binding=self.binding.resolution.pit_binding,
            content=canonical_content,
        )


def resolve_cash_balance_risk_evidence(
    *,
    binding: RiskEvidenceKindBinding,
    content: bytes | None,
) -> RiskEvidenceKindBinding | DecodedCashBalanceRiskEvidence:
    """
    Resolve a CASH_BALANCE kind binding into either the unchanged missing binding or decoded present evidence.
    """
    _require_cash_binding(binding)

    resolution_type = type(binding.resolution)

    if resolution_type is MissingRiskEvidence:
        if content is not None:
            raise ValueError("content must be supplied exactly for the content-matched cash balance branch")
        return binding

    if resolution_type is RiskEvidenceContentMatch:
        if content is None:
            raise ValueError("content must be supplied exactly for the content-matched cash balance branch")
        fact = decode_cash_balance_risk_fact(content)
        return DecodedCashBalanceRiskEvidence(binding=binding, fact=fact)

    raise TypeError(
        "binding resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance"
    )
