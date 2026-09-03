"""
backend/tests/test_risk_evidence.py
===================================
TDD unit test suite for MissingRiskEvidence (Phase 15B.3).

Covers:
- MissingRiskEvidence frozen dataclass invariants with exactly two stored fields:
  (context, missing_inputs)
- Strict concrete-type validation:
  * `type(context) is RiskAxisContext`
  * `type(missing_inputs) is tuple`
  * Every item in missing_inputs must be exact `str` (no leading/trailing whitespace, non-empty, unique)
- Preserves context by identity (`is`) and preserves caller tuple/item order exactly
- Static callback-safe error messages with anchored regex matching:
  * r"^context must be an exact RiskAxisContext instance$"
  * r"^missing_inputs must be an exact non-empty tuple$"
  * r"^each missing input must be an exact non-empty canonical string$"
  * r"^missing_inputs must contain unique entries$"
- Adversarial test doubles (exploding __repr__, __str__, metaclass __name__, __iter__)
- Invariants: missing is absence only, no numeric zero or default risk representation, no convenience/decision properties
"""

from dataclasses import fields
from datetime import date, datetime, timezone

import pytest

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, RiskAxis
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence


class SubclassedRiskAxisContext(RiskAxisContext):
    pass


class SubclassedTuple(tuple):
    pass


class AdversarialReprObject:
    def __repr__(self):
        raise RuntimeError("Adversarial repr explosion")

    def __str__(self):
        raise RuntimeError("Adversarial str explosion")


class AdversarialMeta(type):
    @property
    def __name__(cls):
        raise RuntimeError("Adversarial metaclass __name__ explosion")


class AdversarialMetaClass(metaclass=AdversarialMeta):
    pass


class AdversarialIterObject:
    def __iter__(self):
        raise RuntimeError("Adversarial iter explosion")


def _make_risk_axis_context(
    axis: RiskAxis = RiskAxis.TOLERANCE,
    horizon: Horizon = Horizon.TACTICAL_1M,
    as_of_date: date = date(2026, 8, 28),
    mode: AsOfMode = AsOfMode.SOURCE_AS_OF,
    cutoff_dt: datetime = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
) -> RiskAxisContext:
    h_ctx = AnalysisHorizonContext(horizon=horizon, as_of_date=as_of_date)
    pit_ctx = AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff_dt)
    t_ctx = AnalysisTemporalContext(horizon_context=h_ctx, pit_context=pit_ctx)
    return RiskAxisContext(axis=axis, temporal_context=t_ctx)


class TestMissingRiskEvidenceContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        ctx = _make_risk_axis_context()
        inputs = ("self_reported_tolerance_evidence",)
        ev = MissingRiskEvidence(context=ctx, missing_inputs=inputs)

        f_names = [f.name for f in fields(ev)]
        assert f_names == ["context", "missing_inputs"]

        with pytest.raises(AttributeError):
            ev.context = ctx  # type: ignore

        with pytest.raises(AttributeError):
            ev.missing_inputs = inputs  # type: ignore

    def test_preserves_context_identity_and_tuple_order(self):
        ctx = _make_risk_axis_context(axis=RiskAxis.CAPACITY)
        inputs = ("liquidity_capacity_evidence", "liability_capacity_evidence")
        ev = MissingRiskEvidence(context=ctx, missing_inputs=inputs)

        assert ev.context is ctx
        assert ev.missing_inputs == inputs
        assert ev.missing_inputs is inputs
        assert ev.missing_inputs[0] == "liquidity_capacity_evidence"
        assert ev.missing_inputs[1] == "liability_capacity_evidence"

    @pytest.mark.parametrize("axis", list(RiskAxis))
    def test_both_risk_axes_accepted(self, axis):
        ctx = _make_risk_axis_context(axis=axis)
        inputs = (f"{axis.value}_evidence",)
        ev = MissingRiskEvidence(context=ctx, missing_inputs=inputs)
        assert ev.context.axis is axis
        assert ev.missing_inputs == inputs

    def test_no_convenience_or_decision_properties(self):
        ctx = _make_risk_axis_context()
        ev = MissingRiskEvidence(context=ctx, missing_inputs=("missing_input",))

        prohibited_attrs = [
            "value",
            "score",
            "level",
            "status",
            "available",
            "present",
            "complete",
            "sufficient",
            "verified",
            "suitability",
            "overall_risk",
            "required_risk",
            "owner_id",
            "tolerance",
            "capacity",
        ]
        for attr in prohibited_attrs:
            assert not hasattr(ev, attr), f"MissingRiskEvidence must not have property '{attr}'"

    def test_missing_is_absence_not_numeric_zero(self):
        ctx = _make_risk_axis_context()
        ev = MissingRiskEvidence(context=ctx, missing_inputs=("missing_input",))
        assert not hasattr(ev, "score")
        assert not hasattr(ev, "value")
        assert not hasattr(ev, "level")


class TestMissingRiskEvidenceValidation:
    def test_rejects_subclass_context(self):
        t_ctx = _make_risk_axis_context().temporal_context
        sub_ctx = SubclassedRiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=t_ctx)
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            MissingRiskEvidence(context=sub_ctx, missing_inputs=("tolerance_evidence",))

    @pytest.mark.parametrize(
        "bad_ctx",
        [
            None,
            True,
            False,
            "tolerance",
            RiskAxis.TOLERANCE,
            _make_risk_axis_context().temporal_context,
            date(2026, 8, 28),
            datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            1,
            {"context": _make_risk_axis_context()},
            object(),
        ],
    )
    def test_rejects_invalid_context_types(self, bad_ctx):
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            MissingRiskEvidence(context=bad_ctx, missing_inputs=("tolerance_evidence",))  # type: ignore

    @pytest.mark.parametrize(
        "bad_inputs",
        [
            (),
            [],
            ["tolerance_evidence"],
            {"tolerance_evidence"},
            frozenset(["tolerance_evidence"]),
            "tolerance_evidence",
            None,
            1,
            True,
            False,
            SubclassedTuple(("tolerance_evidence",)),
            {"missing_inputs": ("tolerance_evidence",)},
            object(),
        ],
    )
    def test_rejects_non_tuple_or_empty_missing_inputs(self, bad_inputs):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^missing_inputs must be an exact non-empty tuple$"):
            MissingRiskEvidence(context=ctx, missing_inputs=bad_inputs)  # type: ignore

    @pytest.mark.parametrize(
        "bad_item",
        [
            None,
            True,
            False,
            1,
            0,
            3.14,
            b"tolerance_evidence",
            RiskAxis.TOLERANCE,
            object(),
            "",
            "   ",
            "\t\n",
            " leading_space",
            "trailing_space ",
            " padded ",
        ],
    )
    def test_rejects_invalid_missing_input_items(self, bad_item):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^each missing input must be an exact non-empty canonical string$"):
            MissingRiskEvidence(context=ctx, missing_inputs=(bad_item,))  # type: ignore

    def test_rejects_mixed_invalid_items(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^each missing input must be an exact non-empty canonical string$"):
            MissingRiskEvidence(context=ctx, missing_inputs=("valid_evidence", " trailing_space"))

    def test_rejects_duplicate_items(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^missing_inputs must contain unique entries$"):
            MissingRiskEvidence(context=ctx, missing_inputs=("evidence_a", "evidence_a"))

    def test_rejects_swapped_arguments(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            MissingRiskEvidence(context=("evidence_a",), missing_inputs=ctx)  # type: ignore

    def test_adversarial_repr_context(self):
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            MissingRiskEvidence(context=AdversarialReprObject(), missing_inputs=("evidence_a",))  # type: ignore

    def test_adversarial_repr_missing_inputs(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^missing_inputs must be an exact non-empty tuple$"):
            MissingRiskEvidence(context=ctx, missing_inputs=AdversarialReprObject())  # type: ignore

    def test_adversarial_metaclass_context(self):
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            MissingRiskEvidence(context=AdversarialMetaClass(), missing_inputs=("evidence_a",))  # type: ignore

    def test_adversarial_metaclass_missing_inputs(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^missing_inputs must be an exact non-empty tuple$"):
            MissingRiskEvidence(context=ctx, missing_inputs=AdversarialMetaClass())  # type: ignore

    def test_adversarial_iter_missing_inputs(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^missing_inputs must be an exact non-empty tuple$"):
            MissingRiskEvidence(context=ctx, missing_inputs=AdversarialIterObject())  # type: ignore
