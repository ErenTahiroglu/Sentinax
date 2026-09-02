"""
backend/tests/test_analysis_pit.py
==================================
TDD unit test suite for AnalysisPITContext (Phase 15A.5).

Covers:
- AnalysisPITContext frozen dataclass invariants
- Accepts both canonical AsOfMode enum members (SOURCE_AS_OF, SYSTEM_AS_OF)
- Mode identity preserved with `is`
- Rejects raw strings ("source_as_of", "system_as_of") with TypeError
- Rejects None, bool, int, float, arbitrary objects, Horizon, HorizonFamily, and foreign Enum members as mode
- Accepts UTC, positive-offset, and negative-offset aware datetimes
- Preserves original datetime object, timezone offset, fold, and microseconds
- Derived property `knowledge_cutoff_utc` matches exact same instant in UTC
- Rejects naive datetime, date, None, bool, str, int, float, arbitrary objects, tzinfo returning None, and tzinfo raising error
- Arbitrary historical and future aware datetimes work without clock access
- Zero as_of_date, horizon, target_date, effective_date, observation, resolver, or fallback properties
- No implicit SOURCE_AS_OF <-> SYSTEM_AS_OF conversion
"""

from dataclasses import fields
from datetime import date, datetime, timedelta, timezone, tzinfo
from enum import Enum

import pytest

from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, HorizonFamily


class ForeignEnum(Enum):
    SOURCE = "source_as_of"
    SYSTEM = "system_as_of"


class NullOffsetTz(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NullTz"


class ErrorOffsetTz(tzinfo):
    def utcoffset(self, dt):
        raise RuntimeError("Malformed timezone implementation")

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "ErrorTz"


class TestAnalysisPITContextContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        cutoff = datetime(2026, 8, 28, 10, 30, 0, tzinfo=timezone.utc)
        ctx = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=cutoff)

        f_names = [f.name for f in fields(ctx)]
        assert f_names == ["mode", "knowledge_cutoff"]

        with pytest.raises(AttributeError):
            ctx.mode = AsOfMode.SYSTEM_AS_OF  # type: ignore

        with pytest.raises(AttributeError):
            ctx.knowledge_cutoff = datetime(2026, 8, 29, 10, 30, 0, tzinfo=timezone.utc)  # type: ignore

    @pytest.mark.parametrize(
        "mode",
        [
            AsOfMode.SOURCE_AS_OF,
            AsOfMode.SYSTEM_AS_OF,
        ],
    )
    def test_both_canonical_modes_accepted_and_identity_preserved(self, mode):
        cutoff = datetime(2026, 9, 2, 14, 0, 0, tzinfo=timezone.utc)
        ctx = AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff)

        assert ctx.mode is mode
        assert ctx.knowledge_cutoff is cutoff

    def test_preserves_original_representation_and_derives_utc(self):
        tz_plus_3 = timezone(timedelta(hours=3))
        cutoff = datetime(2026, 9, 2, 17, 30, 45, 123456, tzinfo=tz_plus_3, fold=1)
        ctx = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=cutoff)

        # Preserved exact object and attributes
        assert ctx.knowledge_cutoff is cutoff
        assert ctx.knowledge_cutoff.tzinfo == tz_plus_3
        assert ctx.knowledge_cutoff.microsecond == 123456
        assert ctx.knowledge_cutoff.fold == 1

        # Derived UTC instant
        expected_utc = datetime(2026, 9, 2, 14, 30, 45, 123456, tzinfo=timezone.utc, fold=1)
        assert ctx.knowledge_cutoff_utc == expected_utc
        assert ctx.knowledge_cutoff_utc.tzinfo == timezone.utc
        assert ctx.knowledge_cutoff_utc.microsecond == 123456

    def test_negative_offset_timezone(self):
        tz_minus_5 = timezone(timedelta(hours=-5))
        cutoff = datetime(2026, 9, 2, 9, 0, 0, tzinfo=tz_minus_5)
        ctx = AnalysisPITContext(mode=AsOfMode.SYSTEM_AS_OF, knowledge_cutoff=cutoff)

        assert ctx.knowledge_cutoff is cutoff
        expected_utc = datetime(2026, 9, 2, 14, 0, 0, tzinfo=timezone.utc)
        assert ctx.knowledge_cutoff_utc == expected_utc

    @pytest.mark.parametrize(
        "test_dt",
        [
            datetime(1900, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=timezone.utc),
            datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
            datetime(3000, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        ],
    )
    def test_arbitrary_historical_and_future_datetimes_without_clock(self, test_dt):
        ctx = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=test_dt)
        assert ctx.knowledge_cutoff == test_dt

    def test_no_implied_temporal_or_resolver_properties(self):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        ctx = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=cutoff)

        assert not hasattr(ctx, "as_of_date")
        assert not hasattr(ctx, "horizon")
        assert not hasattr(ctx, "target_date")
        assert not hasattr(ctx, "effective_date")
        assert not hasattr(ctx, "observation")
        assert not hasattr(ctx, "resolver")
        assert not hasattr(ctx, "fallback")


class TestAnalysisPITContextModeValidation:
    @pytest.mark.parametrize(
        "raw_str",
        ["source_as_of", "system_as_of", "SOURCE_AS_OF", "SYSTEM_AS_OF"],
    )
    def test_rejects_raw_strings(self, raw_str):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match="mode must be a canonical AsOfMode enum member"):
            AnalysisPITContext(mode=raw_str, knowledge_cutoff=cutoff)  # type: ignore

    @pytest.mark.parametrize(
        "bad_mode",
        [
            None,
            True,
            False,
            1,
            0,
            3.14,
            object(),
            Horizon.TACTICAL_1M,
            HorizonFamily.TACTICAL,
            ForeignEnum.SOURCE,
            ForeignEnum.SYSTEM,
        ],
    )
    def test_rejects_invalid_mode_types(self, bad_mode):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match="mode must be a canonical AsOfMode enum member"):
            AnalysisPITContext(mode=bad_mode, knowledge_cutoff=cutoff)  # type: ignore


class TestAnalysisPITContextKnowledgeCutoffValidation:
    def test_rejects_naive_datetime(self):
        naive_dt = datetime(2026, 8, 28, 10, 0, 0)
        with pytest.raises(TypeError, match="knowledge_cutoff must be a timezone-aware datetime"):
            AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=naive_dt)

    def test_rejects_null_offset_tzinfo(self):
        dt = datetime(2026, 8, 28, 10, 0, 0, tzinfo=NullOffsetTz())
        with pytest.raises(TypeError, match="knowledge_cutoff must be a timezone-aware datetime"):
            AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=dt)

    def test_rejects_error_offset_tzinfo(self):
        dt = datetime(2026, 8, 28, 10, 0, 0, tzinfo=ErrorOffsetTz())
        with pytest.raises(TypeError, match="knowledge_cutoff must be a timezone-aware datetime"):
            AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=dt)

    @pytest.mark.parametrize(
        "bad_cutoff",
        [
            date(2026, 8, 28),
            None,
            True,
            False,
            "2026-08-28T10:00:00Z",
            "2026-08-28",
            12345678,
            3.14,
            object(),
        ],
    )
    def test_rejects_non_datetime_types(self, bad_cutoff):
        with pytest.raises(TypeError, match="knowledge_cutoff must be a timezone-aware datetime"):
            AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=bad_cutoff)  # type: ignore
