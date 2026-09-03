"""
backend/engine/private/risk_evidence.py
=======================================
Explicit missing-risk-evidence primitive for the Private Investment Decision Engine (Phase 15B.3).

Architectural Invariants:
    - Pure domain value object / context primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Strict concrete-type validation:
      * `type(context) is RiskAxisContext`
      * `type(missing_inputs) is tuple`
      * `type(item) is str` for all items in `missing_inputs` (rejects empty/whitespace-only/padded strings).
    - Entries in `missing_inputs` must be unique and non-empty; preserves caller order without sorting or modification.
    - Preserves `RiskAxisContext` by identity (`is`).
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - Models absence only: missing is NEVER converted into 0, Decimal("0"), LOW/MEDIUM/HIGH, default, or inferred risk.
    - No evidence availability claim, risk scoring, risk levels, suitability verdict, or owner binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.risk_context import RiskAxisContext


@dataclass(frozen=True)
class MissingRiskEvidence:
    """
    Immutable domain primitive explicitly representing missing evidence for a RiskAxisContext.
    """
    context: RiskAxisContext
    missing_inputs: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.context) is not RiskAxisContext:
            raise TypeError("context must be an exact RiskAxisContext instance")

        if type(self.missing_inputs) is not tuple or len(self.missing_inputs) == 0:
            raise TypeError("missing_inputs must be an exact non-empty tuple")

        seen: set[str] = set()
        for item in self.missing_inputs:
            if type(item) is not str or len(item) == 0 or item.strip() != item or len(item.strip()) == 0:
                raise TypeError("each missing input must be an exact non-empty canonical string")
            if item in seen:
                raise TypeError("missing_inputs must contain unique entries")
            seen.add(item)
