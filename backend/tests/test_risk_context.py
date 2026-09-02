"""
backend/tests/test_risk_context.py
==================================
TDD unit test suite for RiskAxisContext (Phase 15B.2).

Covers:
- RiskAxisContext frozen dataclass invariants with exactly two stored fields:
  (axis, temporal_context)
- Accepts both canonical RiskAxis members (TOLERANCE, CAPACITY)
- Identity preservation for temporal_context (`is`)
- Full 2 x 7 x 2 axis/horizon/mode composition matrix
- Strict concrete-type validation:
  * `type(axis) is RiskAxis`
  * `type(temporal_context) is AnalysisTemporalContext`
- Rejects:
  * raw strings ("tolerance", "capacity", "low", "medium", "high", "Orta")
  * Horizon, HorizonFamily, AsOfMode
  * AnalysisHorizonContext, AnalysisPITContext
  * None, bool, date, datetime, mapping, arbitrary object
  * AnalysisTemporalContext subclasses
- Adversarial test doubles (exploding __repr__ and metaclass __name__)
- Anchored static error messages:
  * r"^axis must be an exact RiskAxis instance$"
  * r"^temporal_context must be an exact AnalysisTemporalContext instance$"
- Mutation raises AttributeError
- Prohibited convenience/decision attributes do not exist
"""

from dataclasses import fields
from datetime import date, datetime, timezone
from enum import Enum

import pytest

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, HorizonFamily, RiskAxis
from backend.engine.private.risk_context import RiskAxisContext


class SubclassedTemporalContext(AnalysisTemporalContext):
    pass


class ForeignRiskEnum(Enum):
    TOLERANCE = "tolerance"
    CAPACITY = "capacity"


class AdversarialReprObject:
    def __repr__(self):
        raise RuntimeError("Adversarial repr explosion")


class AdversarialMeta(type):
    @property
    def __name__(cls):
        raise RuntimeError("Adversarial metaclass __name__ explosion")


class AdversarialMetaClass(metaclass=AdversarialMeta):
    pass


def _make_temporal_context(
    horizon: Horizon = Horizon.TACTICAL_1M,
    as_of_date: date = date(2026, 8, 28),
    mode: AsOfMode = AsOfMode.SOURCE_AS_OF,
    cutoff_dt: datetime = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
) -> AnalysisTemporalContext:
    h_ctx = AnalysisHorizonContext(horizon=horizon, as_of_date=as_of_date)
    pit_ctx = AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff_dt)
    return AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)


class TestRiskAxisContextContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        temp_ctx = _make_temporal_context()
        ctx = RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=temp_ctx)

        f_names = [f.name for f in fields(ctx)]
        assert f_names == ["axis", "temporal_context"]

        with pytest.raises(AttributeError):
            ctx.axis = RiskAxis.CAPACITY  # type: ignore

        with pytest.raises(AttributeError):
            ctx.temporal_context = temp_ctx  # type: ignore

    @pytest.mark.parametrize("axis", list(RiskAxis))
    def test_preserves_context_identity(self, axis):
        temp_ctx = _make_temporal_context()
        ctx = RiskAxisContext(axis=axis, temporal_context=temp_ctx)

        assert ctx.axis is axis
        assert ctx.temporal_context is temp_ctx

    @pytest.mark.parametrize("axis", list(RiskAxis))
    @pytest.mark.parametrize("horizon", list(Horizon))
    @pytest.mark.parametrize("mode", list(AsOfMode))
    def test_full_2x7x2_composition_matrix(self, axis, horizon, mode):
        temp_ctx = _make_temporal_context(horizon=horizon, mode=mode)
        ctx = RiskAxisContext(axis=axis, temporal_context=temp_ctx)

        assert ctx.axis is axis
        assert ctx.temporal_context.horizon_context.horizon is horizon
        assert ctx.temporal_context.pit_context.mode is mode

    @pytest.mark.parametrize(
        "anchor_date,cutoff_dt",
        [
            (date(2026, 8, 1), datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)),
            (date(2026, 9, 1), datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)),
            (date(2026, 10, 1), datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)),
        ],
    )
    def test_arbitrary_calendar_relationships_preserved(self, anchor_date, cutoff_dt):
        temp_ctx = _make_temporal_context(as_of_date=anchor_date, cutoff_dt=cutoff_dt)
        ctx = RiskAxisContext(axis=RiskAxis.CAPACITY, temporal_context=temp_ctx)

        assert ctx.temporal_context.horizon_context.as_of_date == anchor_date
        assert ctx.temporal_context.pit_context.knowledge_cutoff == cutoff_dt

    def test_no_convenience_or_decision_properties(self):
        temp_ctx = _make_temporal_context()
        ctx = RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=temp_ctx)

        prohibited_attrs = [
            "tolerance",
            "capacity",
            "horizon",
            "family",
            "months",
            "as_of_date",
            "mode",
            "knowledge_cutoff",
            "knowledge_cutoff_utc",
            "score",
            "level",
            "value",
            "status",
            "available",
            "complete",
            "suitability",
            "overall_risk",
            "required_risk",
            "owner_id",
        ]
        for attr in prohibited_attrs:
            assert not hasattr(ctx, attr), f"RiskAxisContext must not have property '{attr}'"


class TestRiskAxisContextTypeValidation:
    def test_rejects_subclass_temporal_context(self):
        h_ctx = AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28))
        pit_ctx = AnalysisPITContext(
            mode=AsOfMode.SOURCE_AS_OF,
            knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        sub_temp = SubclassedTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)

        with pytest.raises(TypeError, match=r"^temporal_context must be an exact AnalysisTemporalContext instance$"):
            RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=sub_temp)

    @pytest.mark.parametrize(
        "bad_axis",
        [
            None,
            True,
            False,
            "tolerance",
            "capacity",
            "TOLERANCE",
            "CAPACITY",
            "low",
            "medium",
            "high",
            "Orta",
            Horizon.TACTICAL_1M,
            HorizonFamily.TACTICAL,
            AsOfMode.SOURCE_AS_OF,
            ForeignRiskEnum.TOLERANCE,
            date(2026, 8, 28),
            datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            1,
            0,
            {"axis": RiskAxis.TOLERANCE},
            object(),
        ],
    )
    def test_rejects_invalid_axis_types(self, bad_axis):
        temp_ctx = _make_temporal_context()
        with pytest.raises(TypeError, match=r"^axis must be an exact RiskAxis instance$"):
            RiskAxisContext(axis=bad_axis, temporal_context=temp_ctx)  # type: ignore

    @pytest.mark.parametrize(
        "bad_temp_ctx",
        [
            None,
            True,
            False,
            "temporal_context",
            RiskAxis.TOLERANCE,
            AnalysisHorizonContext(horizon=Horizon.TACTICAL_1M, as_of_date=date(2026, 8, 28)),
            AnalysisPITContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)),
            date(2026, 8, 28),
            datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            {"temporal_context": _make_temporal_context()},
            object(),
        ],
    )
    def test_rejects_invalid_temporal_context_types(self, bad_temp_ctx):
        with pytest.raises(TypeError, match=r"^temporal_context must be an exact AnalysisTemporalContext instance$"):
            RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=bad_temp_ctx)  # type: ignore

    def test_rejects_swapped_arguments(self):
        temp_ctx = _make_temporal_context()
        with pytest.raises(TypeError, match=r"^axis must be an exact RiskAxis instance$"):
            RiskAxisContext(axis=temp_ctx, temporal_context=RiskAxis.TOLERANCE)  # type: ignore

    def test_adversarial_repr_axis(self):
        temp_ctx = _make_temporal_context()
        with pytest.raises(TypeError, match=r"^axis must be an exact RiskAxis instance$"):
            RiskAxisContext(axis=AdversarialReprObject(), temporal_context=temp_ctx)  # type: ignore

    def test_adversarial_repr_temporal_context(self):
        with pytest.raises(TypeError, match=r"^temporal_context must be an exact AnalysisTemporalContext instance$"):
            RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=AdversarialReprObject())  # type: ignore

    def test_adversarial_metaclass_axis(self):
        temp_ctx = _make_temporal_context()
        with pytest.raises(TypeError, match=r"^axis must be an exact RiskAxis instance$"):
            RiskAxisContext(axis=AdversarialMetaClass(), temporal_context=temp_ctx)  # type: ignore

    def test_adversarial_metaclass_temporal_context(self):
        with pytest.raises(TypeError, match=r"^temporal_context must be an exact AnalysisTemporalContext instance$"):
            RiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=AdversarialMetaClass())  # type: ignore
