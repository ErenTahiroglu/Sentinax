"""
backend/engine/private/result.py
==================================
Core result contract for the Private Investment Decision Engine.

The DataResult and AnalysisResult types enforce the fundamental principle:
    MISSING DATA ≠ ZERO

Any analysis component that cannot compute a value MUST return
DataStatus.UNAVAILABLE — never fabricate a number.

Partial analysis is a first-class concept:
    - Some fields UNAVAILABLE → aggregate status = PARTIAL
    - PARTIAL is a valid, publishable result
    - Consumer decides what to do with PARTIAL; engine never hides it

No external dependencies — pure Python 3.10+ stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from backend.engine.private.domain import (
    DataStatus,
    DataConfidenceLevel,
    SourceTier,
)


# ─────────────────────────────────────────────────────────────────────────────
# Atomic Data Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DataResult:
    """
    The atomic result type for any single data field or sub-analysis.

    Attributes:
        value:          The computed value. None iff status == UNAVAILABLE.
                        NEVER substitute 0, 1.0, or any magic number for None.
        status:         Completeness and reliability of this result.
        confidence:     Ordinal confidence signal for the consumer.
        as_of:          Point-in-time the value is valid for (look-ahead safe).
                        None if data is structurally unavailable.
        source_refs:    Identifiers of the data sources used.
                        Empty list is valid (e.g. derived computation).
        warnings:       Non-fatal issues encountered during computation.
                        Consumer should surface these to the user.
        missing_inputs: Names of inputs that were absent or unusable.
                        Populated when status != COMPLETE.

    Invariants enforced by from_value() and unavailable() factory methods:
        - value is None  ↔  status == UNAVAILABLE
        - warnings list is never None (may be empty)
        - missing_inputs list is never None (may be empty)
    """
    value: Any
    status: DataStatus
    confidence: DataConfidenceLevel
    as_of: date | None
    source_refs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)

    # ── Factory Methods ───────────────────────────────────────────────────────

    @classmethod
    def complete(
        cls,
        value: Any,
        as_of: date | None = None,
        source_refs: list[str] | None = None,
        confidence: DataConfidenceLevel = DataConfidenceLevel.HIGH,
    ) -> "DataResult":
        """Create a fully complete result with a known value."""
        if value is None:
            raise ValueError(
                "DataResult.complete() requires a non-None value. "
                "Use DataResult.unavailable() for missing data."
            )
        return cls(
            value=value,
            status=DataStatus.COMPLETE,
            confidence=confidence,
            as_of=as_of,
            source_refs=source_refs or [],
        )

    @classmethod
    def partial(
        cls,
        value: Any,
        missing_inputs: list[str],
        warnings: list[str] | None = None,
        as_of: date | None = None,
        source_refs: list[str] | None = None,
        confidence: DataConfidenceLevel = DataConfidenceLevel.MEDIUM,
    ) -> "DataResult":
        """
        Create a partial result: value computable but with reduced inputs.
        missing_inputs must be non-empty to justify PARTIAL status.
        """
        if not missing_inputs:
            raise ValueError(
                "DataResult.partial() requires at least one missing_input. "
                "Use DataResult.complete() if all inputs are present."
            )
        return cls(
            value=value,
            status=DataStatus.PARTIAL,
            confidence=confidence,
            as_of=as_of,
            source_refs=source_refs or [],
            warnings=warnings or [],
            missing_inputs=missing_inputs,
        )

    @classmethod
    def unavailable(
        cls,
        missing_inputs: list[str],
        warnings: list[str] | None = None,
    ) -> "DataResult":
        """
        Create an unavailable result: value cannot be computed.
        Value is ALWAYS None. Do not pass a default value here.
        """
        return cls(
            value=None,
            status=DataStatus.UNAVAILABLE,
            confidence=DataConfidenceLevel.NONE,
            as_of=None,
            warnings=warnings or [],
            missing_inputs=missing_inputs,
        )

    @classmethod
    def stale(
        cls,
        value: Any,
        as_of: date,
        source_refs: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> "DataResult":
        """Create a result from stale (too-old) data. Value is present but confidence is LOW."""
        return cls(
            value=value,
            status=DataStatus.STALE,
            confidence=DataConfidenceLevel.LOW,
            as_of=as_of,
            source_refs=source_refs or [],
            warnings=warnings or [f"Data as of {as_of} may be stale."],
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True iff value can be used (COMPLETE, PARTIAL, DEGRADED, STALE)."""
        return self.status != DataStatus.UNAVAILABLE

    @property
    def is_complete(self) -> bool:
        return self.status == DataStatus.COMPLETE

    def to_dict(self) -> dict:
        """Serialise to a plain dict for API responses and logging."""
        return {
            "value": self.value,
            "status": self.status.value,
            "confidence": self.confidence.value,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source_refs": self.source_refs,
            "warnings": self.warnings,
            "missing_inputs": self.missing_inputs,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate Analysis Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    """
    Aggregate result for a multi-field analysis (e.g. quality analysis, valuation).

    The aggregate status is derived from component statuses:
        - All COMPLETE          → COMPLETE
        - Any UNAVAILABLE       → PARTIAL (not full crash)
        - All UNAVAILABLE       → UNAVAILABLE
        - Any STALE             → DEGRADED (at minimum)

    Example — Quality analysis with partial inputs:
        quality_score = DataResult.partial(score=72, missing=["ROE"])
        valuation     = DataResult.unavailable(missing=["EV", "EBITDA"])
        
        aggregate = AnalysisResult.from_components(
            components={"quality": quality_score, "valuation": valuation},
            computed_at=datetime.now(UTC),
        )
        # aggregate.status == PARTIAL
        # aggregate.components["quality"].is_available == True
        # aggregate.components["valuation"].is_available == False
    """
    components: dict[str, DataResult]
    status: DataStatus
    computed_at: datetime
    global_warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_components(
        cls,
        components: dict[str, DataResult],
        computed_at: datetime,
        global_warnings: list[str] | None = None,
    ) -> "AnalysisResult":
        """Derive aggregate status from component statuses."""
        statuses = {r.status for r in components.values()}

        if statuses == {DataStatus.COMPLETE}:
            agg_status = DataStatus.COMPLETE
        elif statuses == {DataStatus.UNAVAILABLE}:
            agg_status = DataStatus.UNAVAILABLE
        elif DataStatus.STALE in statuses and DataStatus.UNAVAILABLE not in statuses:
            agg_status = DataStatus.DEGRADED
        else:
            agg_status = DataStatus.PARTIAL

        return cls(
            components=components,
            status=agg_status,
            computed_at=computed_at,
            global_warnings=global_warnings or [],
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "computed_at": self.computed_at.isoformat(),
            "global_warnings": self.global_warnings,
            "components": {k: v.to_dict() for k, v in self.components.items()},
        }
