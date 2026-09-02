"""
backend/engine/private/portfolio/goal_date.py
=============================================
Pure calendar-date temporal context for InvestmentGoal (Phase 15A.3).

Architectural Invariants:
    - Pure domain value object / context primitive.
    - Zero clock calls (`date.today()`, `datetime.now()`), zero network, zero persistence.
    - Strict Python `date` validation (rejects `datetime`, `bool`, `str`, `int`, etc.).
    - Strict `InvestmentGoal` validation (preserves identity by reference).
    - Exact day offset: `(target_date - as_of_date).days` (None when target_date is missing).
    - Zero Horizon or HorizonFamily inference / rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional

from backend.engine.private.portfolio.models import InvestmentGoal


class GoalDateState(str, Enum):
    """
    Temporal relation between an InvestmentGoal's target_date and a given as_of_date.
    """
    TARGET_DATE_MISSING = "target_date_missing"
    OVERDUE = "overdue"
    DUE_TODAY = "due_today"
    FUTURE = "future"


@dataclass(frozen=True)
class GoalDateContext:
    """
    Immutable temporal evaluation context for an InvestmentGoal relative to an explicit as_of_date.
    """
    goal: InvestmentGoal
    as_of_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.goal, InvestmentGoal):
            raise TypeError(f"goal must be an InvestmentGoal instance, got {type(self.goal).__name__}: {self.goal!r}")

        if self.as_of_date is None:
            raise ValueError("as_of_date is required and cannot be None.")

        if isinstance(self.as_of_date, datetime):
            raise TypeError(f"as_of_date must be a strict date, not datetime: {self.as_of_date!r}")

        if isinstance(self.as_of_date, bool) or not isinstance(self.as_of_date, date):
            raise TypeError(f"as_of_date must be a date, got {type(self.as_of_date).__name__}: {self.as_of_date!r}")

    @property
    def target_date(self) -> Optional[date]:
        """Passes through goal.target_date unchanged."""
        return self.goal.target_date

    @property
    def state(self) -> GoalDateState:
        """Derives GoalDateState relative to as_of_date."""
        target = self.goal.target_date
        if target is None:
            return GoalDateState.TARGET_DATE_MISSING
        if target < self.as_of_date:
            return GoalDateState.OVERDUE
        if target == self.as_of_date:
            return GoalDateState.DUE_TODAY
        return GoalDateState.FUTURE

    @property
    def day_offset(self) -> Optional[int]:
        """
        Exact integer day offset `(target_date - as_of_date).days`.
        Returns None when target_date is missing.
        """
        target = self.goal.target_date
        if target is None:
            return None
        return (target - self.as_of_date).days
