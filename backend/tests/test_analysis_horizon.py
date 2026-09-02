"""
backend/tests/test_analysis_horizon.py
======================================
TDD unit test suite for AnalysisHorizonContext (Phase 15A.4).

Covers:
- AnalysisHorizonContext frozen dataclass invariants
- Accepts all 7 canonical Horizon enum members
- Horizon identity preserved with `is`
- Exact derived properties: family and months matching canonical Horizon
- Rejects raw strings (e.g. "1M", "3M", "6M", "12M", "24M", "3Y", "5Y") with TypeError
- Rejects None, bool, int, float, arbitrary objects, HorizonFamily, and foreign Enum members as horizon
- Preserves arbitrary historical, present, leap-day, and distant-future dates without clock access
- Rejects naive datetime, aware datetime, None, bool, str, int, float, and arbitrary objects as as_of_date
- Strictly no goal/target-date inference or implied end_date/day_offset properties
"""

from dataclasses import fields
from datetime import date, datetime, timezone
from enum import Enum

import pytest

from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.domain import Horizon, HorizonFamily


class ForeignEnum(Enum):
    ONE_MONTH = "1M"
    THREE_MONTHS = "3M"


class TestAnalysisHorizonContextContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))

        # Check fields
        f_names = [f.name for f in fields(ctx)]
        assert f_names == ["horizon", "as_of_date"]

        # Check immutability
        with pytest.raises(AttributeError):
            ctx.horizon = Horizon.TACTICAL_3M  # type: ignore

        with pytest.raises(AttributeError):
            ctx.as_of_date = date(2026, 8, 29)  # type: ignore

    @pytest.mark.parametrize(
        "horizon,expected_family,expected_months",
        [
            (Horizon.TACTICAL_1M, HorizonFamily.TACTICAL, 1),
            (Horizon.TACTICAL_3M, HorizonFamily.TACTICAL, 3),
            (Horizon.ALLOCATION_6M, HorizonFamily.ALLOCATION, 6),
            (Horizon.ALLOCATION_12M, HorizonFamily.ALLOCATION, 12),
            (Horizon.ALLOCATION_24M, HorizonFamily.ALLOCATION, 24),
            (Horizon.STRATEGIC_3Y, HorizonFamily.STRATEGIC, 36),
            (Horizon.STRATEGIC_5Y, HorizonFamily.STRATEGIC, 60),
        ],
    )
    def test_all_seven_canonical_horizons_accepted_with_properties(
        self, horizon, expected_family, expected_months
    ):
        d = date(2026, 9, 2)
        ctx = AnalysisHorizonContext(horizon=horizon, as_of_date=d)

        assert ctx.horizon is horizon
        assert ctx.as_of_date == d
        assert ctx.family == expected_family
        assert ctx.months == expected_months
        assert isinstance(ctx.months, int)
        assert not isinstance(ctx.months, bool)

    @pytest.mark.parametrize(
        "test_date",
        [
            date(1900, 1, 1),
            date(2024, 2, 29),  # Leap day
            date(2026, 9, 2),
            date(2099, 12, 31),
            date(3000, 6, 15),
        ],
    )
    def test_arbitrary_dates_preserved_without_clock(self, test_date):
        ctx = AnalysisHorizonContext(horizon=Horizon.ALLOCATION_12M, as_of_date=test_date)
        assert ctx.as_of_date == test_date
        assert type(ctx.as_of_date) is date

    def test_no_implied_temporal_properties(self):
        """Context must NOT have end_date, target_date, day_offset, or date-window properties."""
        ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        assert not hasattr(ctx, "end_date")
        assert not hasattr(ctx, "target_date")
        assert not hasattr(ctx, "day_offset")
        assert not hasattr(ctx, "date_window")
        assert not hasattr(ctx, "goal")


class TestAnalysisHorizonContextHorizonValidation:
    @pytest.mark.parametrize(
        "raw_str",
        ["1M", "3M", "6M", "12M", "24M", "3Y", "5Y", "short", "medium", "long", "tactical"],
    )
    def test_rejects_raw_strings(self, raw_str):
        """Raw strings must fail closed with TypeError even if matching valid Horizon values."""
        with pytest.raises(TypeError, match="horizon must be a canonical Horizon enum member"):
            AnalysisHorizonContext(horizon=raw_str, as_of_date=date(2026, 8, 28))  # type: ignore

    @pytest.mark.parametrize(
        "bad_horizon",
        [
            None,
            True,
            False,
            1,
            12,
            3.0,
            object(),
            HorizonFamily.TACTICAL,
            HorizonFamily.ALLOCATION,
            HorizonFamily.STRATEGIC,
            ForeignEnum.ONE_MONTH,
            ForeignEnum.THREE_MONTHS,
        ],
    )
    def test_rejects_invalid_horizon_types(self, bad_horizon):
        """Rejects non-Horizon types with TypeError."""
        with pytest.raises(TypeError, match="horizon must be a canonical Horizon enum member"):
            AnalysisHorizonContext(horizon=bad_horizon, as_of_date=date(2026, 8, 28))  # type: ignore


class TestAnalysisHorizonContextAsOfDateValidation:
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
        """Rejects non-date as_of_date types with TypeError."""
        with pytest.raises(TypeError, match="as_of_date must be a strict Python date"):
            AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=bad_as_of)  # type: ignore
