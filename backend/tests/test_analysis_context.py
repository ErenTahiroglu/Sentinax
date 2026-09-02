"""
backend/tests/test_analysis_context.py
======================================
TDD unit test suite for AnalysisTemporalContext (Phase 15A.6).

Covers:
- AnalysisTemporalContext frozen dataclass invariants with exactly two stored fields
- Valid canonical contexts accepted and preserved by identity (`is`)
- Composition across all 7 canonical Horizon members
- Composition across both canonical AsOfMode members
- Calendar anchor before, equal to, and after the knowledge-cutoff UTC date accepted without inference
- Offset timezone cases where local cutoff date differs from UTC date preserved unchanged
- Strict concrete-type validation: subclasses of AnalysisHorizonContext and AnalysisPITContext rejected
- Rejects None, bool, str, mapping, arbitrary object, raw Horizon, HorizonFamily, AsOfMode, date, datetime, and swapped contexts
- Mutation raises AttributeError
- Prohibited convenience/decision properties do not exist
- Pure metadata object: zero clocks, resolvers, repositories, or network access
"""

from dataclasses import fields
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, HorizonFamily


class SubclassedHorizonContext(AnalysisHorizonContext):
    pass


class SubclassedPITContext(AnalysisPITContext):
    pass


class AdversarialReprObject:
    def __repr__(self):
        raise RuntimeError("Adversarial repr explosion")


class AdversarialMeta(type):
    @property
    def __name__(cls):
        raise RuntimeError("Adversarial metaclass __name__ explosion")


class AdversarialMetaClass(metaclass=AdversarialMeta):
    pass


class TestAnalysisTemporalContextContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        temp_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        f_names = [f.name for f in fields(temp_ctx)]
        assert f_names == ["horizon_context", "pit_context"]

        with pytest.raises(AttributeError):
            temp_ctx.horizon_context = h_ctx  # type: ignore

        with pytest.raises(AttributeError):
            temp_ctx.pit_context = pit_ctx  # type: ignore

    def test_preserves_context_identity(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.ALLOCATION_6M, as_of_date=date(2026, 9, 2))
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SYSTEM_AS_OF,
            knowledge_cutoff=datetime(2026, 9, 2, 14, 0, 0, tzinfo=timezone.utc),
        )
        temp_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        assert temp_ctx.horizon_context is h_ctx
        assert temp_ctx.pit_context is pit_ctx

    @pytest.mark.parametrize("horizon", list(Horizon))
    @pytest.mark.parametrize("mode", list(AsOfMode))
    def test_all_seven_horizons_and_both_modes_composable(self, horizon, mode):
        h_ctx = AnalysisHorizonContext(horizon=horizon, as_of_date=date(2026, 9, 2))
        pit_ctx = AnalysisPITContext(
            mode=mode,
            knowledge_cutoff=datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        )
        temp_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        assert temp_ctx.horizon_context.horizon is horizon
        assert temp_ctx.pit_context.mode is mode

    @pytest.mark.parametrize(
        "anchor_date,cutoff_dt",
        [
            # Anchor before UTC cutoff date
            (date(2026, 8, 1), datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)),
            # Anchor equal to UTC cutoff date
            (date(2026, 9, 1), datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)),
            # Anchor after UTC cutoff date
            (date(2026, 10, 1), datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)),
            # Distant history & future
            (date(1950, 1, 1), datetime(2100, 1, 1, 0, 0, 0, tzinfo=timezone.utc)),
            (date(2100, 1, 1), datetime(1950, 1, 1, 0, 0, 0, tzinfo=timezone.utc)),
        ],
    )
    def test_independent_temporal_axes_permitted(self, anchor_date, cutoff_dt):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_3M, as_of_date=anchor_date)
        pit_ctx = AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=cutoff_dt)
        temp_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        assert temp_ctx.horizon_context.as_of_date == anchor_date
        assert temp_ctx.pit_context.knowledge_cutoff == cutoff_dt

    def test_offset_timezone_date_boundary_preserved(self):
        # 2026-09-02 01:00 UTC+3 is 2026-09-01 22:00 UTC
        tz_plus_3 = timezone(timedelta(hours=3))
        cutoff = datetime(2026, 9, 2, 1, 0, 0, tzinfo=tz_plus_3)
        h_ctx = AnalysisHorizonContext(horizon=Horizon.ALLOCATION_12M, as_of_date=date(2026, 9, 2))
        pit_ctx = AnalysisPITContext(mode=AsOfMode.SYSTEM_AS_OF, knowledge_cutoff=cutoff)
        temp_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        assert temp_ctx.horizon_context.as_of_date == date(2026, 9, 2)
        assert temp_ctx.pit_context.knowledge_cutoff == cutoff
        assert temp_ctx.pit_context.knowledge_cutoff_utc.date() == date(2026, 9, 1)

    def test_no_convenience_or_derived_decision_properties(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        temp_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        prohibited_attrs = [
            "horizon",
            "family",
            "months",
            "as_of_date",
            "mode",
            "knowledge_cutoff",
            "knowledge_cutoff_utc",
            "effective_date",
            "target_date",
            "available",
            "eligible",
            "status",
            "result",
            "observation",
            "end_date",
            "day_offset",
            "goal",
        ]
        for attr in prohibited_attrs:
            assert not hasattr(temp_ctx, attr), f"AnalysisTemporalContext must not have property '{attr}'"


class TestAnalysisTemporalContextTypeValidation:
    def test_rejects_subclass_horizon_context(self):
        h_sub = SubclassedHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError, match="horizon_context must be an exact AnalysisHorizonContext instance"):
            AnalysisTemporalContext(horizon_context=h_sub, pit_context=pit_ctx)

    def test_rejects_subclass_pit_context(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        pit_sub = SubclassedPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError, match="pit_context must be an exact AnalysisPITContext instance"):
            AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_sub)

    @pytest.mark.parametrize(
        "bad_horizon_ctx",
        [
            None,
            True,
            False,
            "1M",
            Horizon.TACTICAL_1M,
            HorizonFamily.TACTICAL,
            date(2026, 8, 28),
            datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            {"horizon": Horizon.TACTICAL_1M, "as_of_date": date(2026, 8, 28)},
            object(),
        ],
    )
    def test_rejects_invalid_horizon_context_types(self, bad_horizon_ctx):
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError, match="horizon_context must be an exact AnalysisHorizonContext instance"):
            AnalysisTemporalContext(horizon_context=bad_horizon_ctx, pit_context=pit_ctx)  # type: ignore

    @pytest.mark.parametrize(
        "bad_pit_ctx",
        [
            None,
            True,
            False,
            "source_as_of",
            AsOfMode.SOURCE_AS_OF,
            date(2026, 8, 28),
            datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            {"mode": AsOfMode.SOURCE_AS_OF, "knowledge_cutoff": datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)},
            object(),
        ],
    )
    def test_rejects_invalid_pit_context_types(self, bad_pit_ctx):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        with pytest.raises(TypeError, match="pit_context must be an exact AnalysisPITContext instance"):
            AnalysisTemporalContext(horizon_context=h_ctx, pit_context=bad_pit_ctx)  # type: ignore

    def test_rejects_swapped_contexts(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError):
            AnalysisTemporalContext(horizon_context=pit_ctx, pit_context=h_ctx)  # type: ignore

    def test_adversarial_repr_horizon_context(self):
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError):
            AnalysisTemporalContext(horizon_context=AdversarialReprObject(), pit_context=pit_ctx)  # type: ignore

    def test_adversarial_repr_pit_context(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        with pytest.raises(TypeError):
            AnalysisTemporalContext(horizon_context=h_ctx, pit_context=AdversarialReprObject())  # type: ignore

    def test_adversarial_metaclass_horizon_context(self):
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError):
            AnalysisTemporalContext(horizon_context=AdversarialMetaClass(), pit_context=pit_ctx)  # type: ignore

    def test_adversarial_metaclass_pit_context(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        with pytest.raises(TypeError):
            AnalysisTemporalContext(horizon_context=h_ctx, pit_context=AdversarialMetaClass())  # type: ignore
