"""
backend/engine/private/analysis_horizon.py
==========================================
Explicit analysis-horizon calendar context for the Private Investment Decision Engine (Phase 15A.4).

Architectural Invariants:
    - Pure domain value object / context primitive.
    - Zero clock calls (`date.today()`, `datetime.now()`), zero network, zero persistence.
    - Explicit `Horizon` enum validation (rejects raw strings, `HorizonFamily`, foreign Enums, non-Enums).
    - Strict Python `date` validation (rejects `datetime`, `bool`, `str`, `int`, etc.).
    - Pass-through derived properties (`family`, `months`).
    - Zero goal/target-date inference or implied month arithmetic/end_date properties.
    - Calendar anchor only; does NOT grant market-data publication or ingestion knowledge cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from backend.engine.private.domain import Horizon, HorizonFamily


@dataclass(frozen=True)
class AnalysisHorizonContext:
    """
    Immutable calendar evaluation context binding an explicit Horizon member with an analysis as_of_date.
    """
    horizon: Horizon
    as_of_date: date

    def __post_init__(self) -> None:
        if isinstance(self.horizon, bool) or not isinstance(self.horizon, Horizon):
            raise TypeError(
                f"horizon must be a canonical Horizon enum member, got {type(self.horizon).__name__}: {self.horizon!r}"
            )

        if self.as_of_date is None:
            raise TypeError("as_of_date must be a strict Python date, got None")

        if isinstance(self.as_of_date, datetime):
            raise TypeError(f"as_of_date must be a strict Python date, not datetime: {self.as_of_date!r}")

        if isinstance(self.as_of_date, bool) or not isinstance(self.as_of_date, date):
            raise TypeError(
                f"as_of_date must be a strict Python date, got {type(self.as_of_date).__name__}: {self.as_of_date!r}"
            )

    @property
    def family(self) -> HorizonFamily:
        """Returns the HorizonFamily of the canonical horizon."""
        return self.horizon.family

    @property
    def months(self) -> int:
        """Returns the exact integer month count of the canonical horizon."""
        return self.horizon.months
