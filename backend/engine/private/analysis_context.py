"""
backend/engine/private/analysis_context.py
==========================================
Immutable analysis temporal composition envelope for the Private Investment Decision Engine (Phase 15A.6).

Architectural Invariants:
    - Pure domain value object / composition primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Strict concrete-type validation (`type(x) is T`; rejects subclasses, raw enums, strings, mappings).
    - Preserves both canonical contexts (`AnalysisHorizonContext`, `AnalysisPITContext`) by identity (`is`).
    - Explicit axis separation: does NOT compare dates, infer ordering, or fallback between modes.
    - Zero convenience aliases or derived decision properties (consumers traverse explicit canonical contexts).
    - No data availability claim, risk metric, recommendation, optimization, or execution logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext


@dataclass(frozen=True)
class AnalysisTemporalContext:
    """
    Immutable envelope composing an explicit AnalysisHorizonContext and AnalysisPITContext.
    """
    horizon_context: AnalysisHorizonContext
    pit_context: AnalysisPITContext

    def __post_init__(self) -> None:
        if type(self.horizon_context) is not AnalysisHorizonContext:
            raise TypeError(
                f"horizon_context must be an exact AnalysisHorizonContext instance, "
                f"got {type(self.horizon_context).__name__}: {self.horizon_context!r}"
            )

        if type(self.pit_context) is not AnalysisPITContext:
            raise TypeError(
                f"pit_context must be an exact AnalysisPITContext instance, "
                f"got {type(self.pit_context).__name__}: {self.pit_context!r}"
            )
