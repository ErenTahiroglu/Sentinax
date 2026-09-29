"""
backend/engine/private/risk_evidence_planned_contribution_resolution.py
=======================================================================
Decoded PLANNED_CONTRIBUTION risk-evidence resolution for the Private Investment Decision Engine
(Phase 15C.6).

Composes, for PLANNED_CONTRIBUTION only:
    - Phase 15C.2 declared kind binding (`RiskEvidenceKindBinding`),
    - Phase 15C.5 canonical PLANNED_CONTRIBUTION v1 schema (`PlannedContributionRiskFact`, encode/decode),
    - Phase 15B.8 digest validation (`RiskEvidenceContentMatch`), the sole digest authority.

Architectural Invariants:
    - Pure domain value object + resolver. Zero clock, network, persistence, UUID generation, entropy,
      filesystem, database, or cache access. NO hashing here (no hashlib/hmac). No portfolio/persistence/
      repository imports, and no dependence on the CASH_BALANCE modules.
    - `DecodedPlannedContributionRiskEvidence` stores exactly `binding` and `fact`; no raw bytes, digest,
      context, kind, schema, owner, Portfolio, or PlannedContribution copies. Its constructor is self-validating:
      1. `type(binding) is RiskEvidenceKindBinding`
      2. `binding.kind is RiskEvidenceKind.PLANNED_CONTRIBUTION`
      3. `type(binding.resolution) is RiskEvidenceContentMatch`
      4. `type(fact) is PlannedContributionRiskFact`
      5. canonical-encode the fact (15C.5 resource errors propagate unchanged)
      6. revalidate the canonical bytes against the binding's existing PIT provenance digest through a
         transient `RiskEvidenceContentMatch` (discarded; the original binding is never replaced).
    - `resolve_planned_contribution_risk_evidence(*, binding, content)`: the missing branch returns the same
      binding object (`content` must be None); the present branch decodes canonical v1 bytes and builds the
      wrapper, so supplied content must be canonical PLANNED_CONTRIBUTION v1 bytes AND match the bound digest.
    - 15B.8 and 15C.5 errors propagate unchanged; composition-owned errors are static strings.
    - Missing remains typed absence; `b""` is content; every ContributionStatus and PortfolioMode is preserved
      and NOT interpreted (no capacity meaning, no sandbox policy).
    - Proves ONLY canonical planned-contribution bytes + declared kind + PIT/digest integrity. It does NOT prove
      that `mode` came from the authoritative Portfolio, that `portfolio_id` exists or is owner-accessible, that
      `contribution_id` exists, or that goal/bucket links exist, share a portfolio/owner, match currencies, or
      are active. A later owner-bound source-construction boundary must establish those facts.
    - No scoring, capacity formula, aggregation, suitability, required risk, or overall risk.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.domain import RiskEvidenceKind
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.engine.private.risk_evidence_planned_contribution_schema import (
    PlannedContributionRiskFact,
    decode_planned_contribution_risk_fact,
    encode_planned_contribution_risk_fact,
)


def _require_planned_contribution_binding(binding: object) -> None:
    if type(binding) is not RiskEvidenceKindBinding:
        raise TypeError("binding must be an exact RiskEvidenceKindBinding instance")
    if binding.kind is not RiskEvidenceKind.PLANNED_CONTRIBUTION:
        raise ValueError("binding kind must be PLANNED_CONTRIBUTION")


@dataclass(frozen=True)
class DecodedPlannedContributionRiskEvidence:
    """
    A content-matched PLANNED_CONTRIBUTION binding together with its decoded, digest-consistent typed fact.
    """
    binding: RiskEvidenceKindBinding
    fact: PlannedContributionRiskFact

    def __post_init__(self) -> None:
        _require_planned_contribution_binding(self.binding)

        if type(self.binding.resolution) is not RiskEvidenceContentMatch:
            raise ValueError("decoded planned contribution evidence requires content-matched evidence")

        if type(self.fact) is not PlannedContributionRiskFact:
            raise TypeError("fact must be an exact PlannedContributionRiskFact instance")

        canonical_content = encode_planned_contribution_risk_fact(self.fact)
        # Phase 15B.8 remains the digest authority; the transient result is intentionally discarded.
        RiskEvidenceContentMatch(
            pit_binding=self.binding.resolution.pit_binding,
            content=canonical_content,
        )


def resolve_planned_contribution_risk_evidence(
    *,
    binding: RiskEvidenceKindBinding,
    content: bytes | None,
) -> RiskEvidenceKindBinding | DecodedPlannedContributionRiskEvidence:
    """
    Resolve a PLANNED_CONTRIBUTION kind binding into either the unchanged missing binding or decoded present
    evidence.
    """
    _require_planned_contribution_binding(binding)

    resolution_type = type(binding.resolution)

    if resolution_type is MissingRiskEvidence:
        if content is not None:
            raise ValueError(
                "content must be supplied exactly for the content-matched planned contribution branch"
            )
        return binding

    if resolution_type is RiskEvidenceContentMatch:
        if content is None:
            raise ValueError(
                "content must be supplied exactly for the content-matched planned contribution branch"
            )
        fact = decode_planned_contribution_risk_fact(content)
        return DecodedPlannedContributionRiskEvidence(binding=binding, fact=fact)

    raise TypeError(
        "binding resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance"
    )
