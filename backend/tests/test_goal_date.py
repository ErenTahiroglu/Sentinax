"""
backend/tests/test_goal_date.py
===============================
TDD unit test suite for GoalDateContext domain primitive (Phase 15A.3).

Covers:
- GoalDateState enum members and string values
- GoalDateContext frozen dataclass invariants
- Strict date validation for as_of_date (rejecting datetime, bool, str, int, float, None, arbitrary objects)
- Strict goal validation (rejecting non-InvestmentGoal instances)
- Derived properties: target_date, state, day_offset
- None target_date -> TARGET_DATE_MISSING, day_offset is None (not 0)
- target_date == as_of_date -> DUE_TODAY, day_offset is 0 (distinct from missing)
- target_date < as_of_date -> OVERDUE, negative day_offset
- target_date > as_of_date -> FUTURE, positive day_offset
- Month-end and leap-day arithmetic
- Arbitrary historical/future as_of_date without system clock access
- Preservation of goal object identity
- Lifecycle status independence (ACTIVE, PAUSED, COMPLETED, CANCELLED)
- Zero Horizon inference
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from backend.engine.private.domain import Currency, GoalPriority, GoalStatus
from backend.engine.private.portfolio.goal_date import GoalDateContext, GoalDateState
from backend.engine.private.portfolio.models import InvestmentGoal


def _make_goal(
    target_date: date | None = date(2030, 1, 1),
    status: GoalStatus = GoalStatus.ACTIVE,
) -> InvestmentGoal:
    return InvestmentGoal(
        portfolio_id=uuid4(),
        name="Test Goal",
        target_amount=Decimal("10000.00"),
        target_currency=Currency.USD,
        created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        target_date=target_date,
        status=status,
    )


class TestGoalDateStateEnum:
    def test_state_enum_members_and_values(self):
        expected = {
            "TARGET_DATE_MISSING": "target_date_missing",
            "OVERDUE": "overdue",
            "DUE_TODAY": "due_today",
            "FUTURE": "future",
        }
        assert {s.name: s.value for s in GoalDateState} == expected
        assert len(GoalDateState) == 4


class TestGoalDateContextSemantics:
    def test_missing_target_date_semantics(self):
        """Missing target date produces TARGET_DATE_MISSING state and None day_offset (never 0)."""
        goal = _make_goal(target_date=None)
        ctx = GoalDateContext(goal=goal, as_of_date=date(2026, 8, 28))

        assert ctx.target_date is None
        assert ctx.state == GoalDateState.TARGET_DATE_MISSING
        assert ctx.day_offset is None
        assert ctx.day_offset != 0

    def test_due_today_semantics(self):
        """Due today produces DUE_TODAY state and exact 0 day_offset (distinct from missing)."""
        d = date(2026, 8, 28)
        goal = _make_goal(target_date=d)
        ctx = GoalDateContext(goal=goal, as_of_date=d)

        assert ctx.target_date == d
        assert ctx.state == GoalDateState.DUE_TODAY
        assert ctx.day_offset == 0
        assert isinstance(ctx.day_offset, int)
        assert not isinstance(ctx.day_offset, bool)

    def test_overdue_semantics(self):
        """Overdue produces OVERDUE state and exact negative day_offset."""
        goal = _make_goal(target_date=date(2026, 8, 20))
        ctx = GoalDateContext(goal=goal, as_of_date=date(2026, 8, 28))

        assert ctx.state == GoalDateState.OVERDUE
        assert ctx.day_offset == -8

    def test_future_semantics(self):
        """Future produces FUTURE state and exact positive day_offset."""
        goal = _make_goal(target_date=date(2026, 9, 5))
        ctx = GoalDateContext(goal=goal, as_of_date=date(2026, 8, 28))

        assert ctx.state == GoalDateState.FUTURE
        assert ctx.day_offset == 8

    def test_month_end_and_leap_year_arithmetic(self):
        """Calculates exact calendar days across month boundaries and leap days."""
        # Across leap day 2028-02-29
        goal = _make_goal(target_date=date(2028, 3, 1))
        ctx = GoalDateContext(goal=goal, as_of_date=date(2028, 2, 28))
        assert ctx.day_offset == 2  # Feb 28 -> Feb 29 (1) -> Mar 1 (2)

        # Non-leap year 2027
        goal_non_leap = _make_goal(target_date=date(2027, 3, 1))
        ctx_non_leap = GoalDateContext(goal=goal_non_leap, as_of_date=date(2027, 2, 28))
        assert ctx_non_leap.day_offset == 1

    def test_arbitrary_historical_and_future_as_of_date_without_clock(self):
        """Operates on arbitrary dates (e.g. 1950 or 2100) without accessing clock or environment."""
        # Ancient history
        goal_hist = _make_goal(target_date=date(1950, 6, 1))
        ctx_hist = GoalDateContext(goal=goal_hist, as_of_date=date(1950, 1, 1))
        assert ctx_hist.state == GoalDateState.FUTURE
        assert ctx_hist.day_offset == 151

        # Distant future
        goal_fut = _make_goal(target_date=date(2099, 12, 31))
        ctx_fut = GoalDateContext(goal=goal_fut, as_of_date=date(2100, 1, 1))
        assert ctx_fut.state == GoalDateState.OVERDUE
        assert ctx_fut.day_offset == -1

    def test_preserves_goal_identity(self):
        """Context retains the exact same InvestmentGoal instance by reference."""
        goal = _make_goal()
        ctx = GoalDateContext(goal=goal, as_of_date=date(2026, 8, 28))
        assert ctx.goal is goal

    def test_context_is_frozen_immutable(self):
        """GoalDateContext is frozen and cannot be mutated."""
        goal = _make_goal()
        ctx = GoalDateContext(goal=goal, as_of_date=date(2026, 8, 28))

        with pytest.raises((AttributeError, TypeError)):
            ctx.as_of_date = date(2026, 8, 29)  # type: ignore

        with pytest.raises((AttributeError, TypeError)):
            ctx.goal = _make_goal()  # type: ignore

    @pytest.mark.parametrize("status", [GoalStatus.ACTIVE, GoalStatus.PAUSED, GoalStatus.COMPLETED, GoalStatus.CANCELLED])
    def test_independent_of_goal_status(self, status):
        """GoalDateContext calculations depend strictly on calendar dates, not lifecycle status."""
        goal = _make_goal(target_date=date(2026, 9, 1), status=status)
        ctx = GoalDateContext(goal=goal, as_of_date=date(2026, 8, 28))
        assert ctx.state == GoalDateState.FUTURE
        assert ctx.day_offset == 4


class TestGoalDateContextValidation:
    @pytest.mark.parametrize(
        "bad_as_of",
        [
            datetime(2026, 8, 28, 12, 0, 0),  # Naive datetime
            datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc),  # Aware datetime
            None,
            True,
            False,
            "2026-08-28",
            12345678,
            3.14,
            object(),
        ],
    )
    def test_rejects_invalid_as_of_date_types(self, bad_as_of):
        """Rejects non-date as_of_date types with TypeError or ValueError."""
        goal = _make_goal()
        with pytest.raises((TypeError, ValueError)):
            GoalDateContext(goal=goal, as_of_date=bad_as_of)  # type: ignore

    @pytest.mark.parametrize(
        "bad_goal",
        [
            None,
            "not a goal",
            123,
            True,
            object(),
            {"target_date": date(2026, 8, 28)},
        ],
    )
    def test_rejects_invalid_goal_types(self, bad_goal):
        """Rejects non-InvestmentGoal goal parameters."""
        with pytest.raises((TypeError, ValueError)):
            GoalDateContext(goal=bad_goal, as_of_date=date(2026, 8, 28))  # type: ignore
