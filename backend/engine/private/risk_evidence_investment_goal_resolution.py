"""
backend/engine/private/risk_evidence_investment_goal_resolution.py
==================================================================
Decoded INVESTMENT_GOAL risk-evidence resolution for the Private Investment Decision Engine (Phase 15C.7).

Composes, for INVESTMENT_GOAL only:
    - Phase 15C.2 declared kind binding (`RiskEvidenceKindBinding`),
    - Phase 15C.7 canonical INVESTMENT_GOAL v1 schema (`InvestmentGoalRiskFact`, encode/decode),
    - Phase 15B.8 digest validation (`RiskEvidenceContentMatch`), the sole digest authority.

Architectural Invariants:
    - Pure domain value object + resolver. Zero clock, network, persistence, UUID generation, entropy,
      filesystem, database, or cache access. NO hashing here (no hashlib/hmac). No portfolio/persistence/
      repository imports, and no dependence on the CASH_BALANCE or PLANNED_CONTRIBUTION modules.
    - `DecodedInvestmentGoalRiskEvidence` stores exactly `binding` and `fact`; no raw bytes, digest, context,
      kind, schema, owner, Portfolio, or InvestmentGoal copies. Its constructor is self-validating:
      1. `type(binding) is RiskEvidenceKindBinding`
      2. `binding.kind is RiskEvidenceKind.INVESTMENT_GOAL`
      3. `type(binding.resolution) is RiskEvidenceContentMatch`
      4. `type(fact) is InvestmentGoalRiskFact`
      5. canonical-encode the fact (schema resource errors propagate unchanged)
      6. revalidate the canonical bytes against the binding's existing PIT provenance digest through a
         transient `RiskEvidenceContentMatch` (discarded; the original binding is never replaced).
    - `resolve_investment_goal_risk_evidence(*, binding, content)`: the missing branch returns the same binding
      object (`content` must be None); the present branch decodes canonical v1 bytes and builds the wrapper, so
      supplied content must be canonical INVESTMENT_GOAL v1 bytes AND match the bound digest.
    - 15B.8 and schema errors propagate unchanged; composition-owned errors are static strings.
    - Missing remains typed absence; `b""` is content; every GoalStatus, GoalPriority, and PortfolioMode is
      preserved and NOT interpreted.
    - Proves ONLY canonical investment-goal bytes + declared kind + PIT/digest integrity. It does NOT prove that
      `mode` came from the authoritative Portfolio, that `portfolio_id` or `goal_id` exists or is
      owner-accessible, or authenticity beyond the digest provenance. A later owner-bound source-construction
      boundary must establish those facts.
    - No scoring, capacity formula, required return/risk, aggregation, suitability, or owner binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.domain import RiskEvidenceKind
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_investment_goal_schema import (
    InvestmentGoalRiskFact,
    decode_investment_goal_risk_fact,
    encode_investment_goal_risk_fact,
)
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding


def _require_investment_goal_binding(binding: object) -> None:
    if type(binding) is not RiskEvidenceKindBinding:
        raise TypeError("binding must be an exact RiskEvidenceKindBinding instance")
    if binding.kind is not RiskEvidenceKind.INVESTMENT_GOAL:
        raise ValueError("binding kind must be INVESTMENT_GOAL")


@dataclass(frozen=True)
class DecodedInvestmentGoalRiskEvidence:
    """
    A content-matched INVESTMENT_GOAL binding together with its decoded, digest-consistent typed fact.
    """
    binding: RiskEvidenceKindBinding
    fact: InvestmentGoalRiskFact

    def __post_init__(self) -> None:
        _require_investment_goal_binding(self.binding)

        if type(self.binding.resolution) is not RiskEvidenceContentMatch:
            raise ValueError("decoded investment goal evidence requires content-matched evidence")

        if type(self.fact) is not InvestmentGoalRiskFact:
            raise TypeError("fact must be an exact InvestmentGoalRiskFact instance")

        canonical_content = encode_investment_goal_risk_fact(self.fact)
        # Phase 15B.8 remains the digest authority; the transient result is intentionally discarded.
        RiskEvidenceContentMatch(
            pit_binding=self.binding.resolution.pit_binding,
            content=canonical_content,
        )


def resolve_investment_goal_risk_evidence(
    *,
    binding: RiskEvidenceKindBinding,
    content: bytes | None,
) -> RiskEvidenceKindBinding | DecodedInvestmentGoalRiskEvidence:
    """
    Resolve an INVESTMENT_GOAL kind binding into either the unchanged missing binding or decoded present evidence.
    """
    _require_investment_goal_binding(binding)

    resolution_type = type(binding.resolution)

    if resolution_type is MissingRiskEvidence:
        if content is not None:
            raise ValueError("content must be supplied exactly for the content-matched investment goal branch")
        return binding

    if resolution_type is RiskEvidenceContentMatch:
        if content is None:
            raise ValueError("content must be supplied exactly for the content-matched investment goal branch")
        fact = decode_investment_goal_risk_fact(content)
        return DecodedInvestmentGoalRiskEvidence(binding=binding, fact=fact)

    raise TypeError(
        "binding resolution must be an exact MissingRiskEvidence or RiskEvidenceContentMatch instance"
    )
