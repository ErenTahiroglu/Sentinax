"""
backend/engine/private/analysis_pit.py
======================================
Explicit analysis PIT knowledge-cutoff context for the Private Investment Decision Engine (Phase 15A.5).

Architectural Invariants:
    - Pure domain value object / context primitive.
    - Zero clock calls (`datetime.now()`, `date.today()`), zero network, zero persistence.
    - Explicit `AsOfMode` enum validation (rejects raw strings, foreign Enums, non-Enums).
    - Strict timezone-aware `datetime` validation (rejects naive datetime, non-datetime, or malformed tzinfo).
    - Preserves caller-supplied datetime object representation while exposing exact `knowledge_cutoff_utc` instant.
    - Does NOT claim data availability, query, filter, or fallback between modes.
    - Strictly separate from calendar date anchors / horizon buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.engine.private.domain import AsOfMode


@dataclass(frozen=True)
class AnalysisPITContext:
    """
    Immutable PIT evaluation context binding an explicit AsOfMode with a timezone-aware knowledge cutoff.
    """
    mode: AsOfMode
    knowledge_cutoff: datetime

    def __post_init__(self) -> None:
        if isinstance(self.mode, bool) or not isinstance(self.mode, AsOfMode):
            raise TypeError(
                f"mode must be a canonical AsOfMode enum member, got {type(self.mode).__name__}: {self.mode!r}"
            )

        if isinstance(self.knowledge_cutoff, bool) or not isinstance(self.knowledge_cutoff, datetime):
            raise TypeError(
                f"knowledge_cutoff must be a timezone-aware datetime, got {type(self.knowledge_cutoff).__name__}: {self.knowledge_cutoff!r}"
            )

        if self.knowledge_cutoff.tzinfo is None:
            raise TypeError(f"knowledge_cutoff must be a timezone-aware datetime, got naive: {self.knowledge_cutoff!r}")

        try:
            offset = self.knowledge_cutoff.tzinfo.utcoffset(self.knowledge_cutoff)
            if offset is None:
                raise TypeError(
                    f"knowledge_cutoff must be a timezone-aware datetime with a valid UTC offset, got: {self.knowledge_cutoff!r}"
                )
        except TypeError:
            raise
        except Exception as e:
            raise TypeError(
                f"knowledge_cutoff must be a timezone-aware datetime: {self.knowledge_cutoff!r}"
            ) from e

    @property
    def knowledge_cutoff_utc(self) -> datetime:
        """Exact same instant converted to UTC."""
        return self.knowledge_cutoff.astimezone(timezone.utc)
