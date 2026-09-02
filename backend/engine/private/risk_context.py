"""
backend/engine/private/risk_context.py
======================================
Explicit per-axis temporal context for the Private Investment Decision Engine (Phase 15B.2).

Architectural Invariants:
    - Pure domain value object / composition primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Strict concrete-type validation (`type(x) is T`; rejects subclasses, raw strings, foreign enums, mappings).
    - Preserves `AnalysisTemporalContext` by identity (`is`).
    - Error messages use static string literals to guarantee callback safety under adversarial inputs.
    - No evidence availability claim, risk scoring, risk levels, suitability verdict, or owner binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.domain import RiskAxis


@dataclass(frozen=True)
class RiskAxisContext:
    """
    Immutable evaluation context binding exactly one RiskAxis with an AnalysisTemporalContext.
    """
    axis: RiskAxis
    temporal_context: AnalysisTemporalContext

    def __post_init__(self) -> None:
        if type(self.axis) is not RiskAxis:
            raise TypeError("axis must be an exact RiskAxis instance")

        if type(self.temporal_context) is not AnalysisTemporalContext:
            raise TypeError("temporal_context must be an exact AnalysisTemporalContext instance")
