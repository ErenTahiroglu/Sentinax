"""
backend/tests/test_risk_evidence_pit_binding.py
===============================================
TDD unit test suite for RiskEvidencePITBinding (Phase 15B.6).

Covers:
- RiskEvidencePITBinding frozen dataclass invariants with exactly two stored fields:
  (context, availability_ref)
- Concrete type validation:
  * `type(context) is RiskAxisContext`
  * `type(availability_ref) is RiskEvidenceAvailabilityRef`
- Fail-closed UTC instant comparison:
  * available_at_utc <= knowledge_cutoff_utc is accepted
  * Exact equality accepted
  * Different timezone representations of the exact same instant accepted
  * available_at_utc > knowledge_cutoff_utc rejected with ValueError:
    "risk evidence availability exceeds the analysis knowledge cutoff"
  * One-microsecond lookahead rejected
  * Cases where local wall-clock appears earlier but UTC instant is later rejected
  * Ordinary exceptions during conversion/comparison raise static TypeError:
    "risk evidence PIT instants must remain valid for UTC comparison"
- Preserves both supplied objects by identity (`is`) without storing normalized UTC
- Static callback-safe error messages with anchored regex matching:
  * r"^context must be an exact RiskAxisContext instance$"
  * r"^availability_ref must be an exact RiskEvidenceAvailabilityRef instance$"
  * r"^risk evidence PIT instants must remain valid for UTC comparison$"
  * r"^risk evidence availability exceeds the analysis knowledge cutoff$"
- Adversarial test doubles (exploding __repr__, __str__, metaclass __name__, failing comparison tzinfo)
- Purity invariants:
  * No current-clock calls, UUID generation, hashing, raw storage, persistence
  * No value, score, level, status, present, verified, authentic, complete, sufficient,
    owner_id, suitability, overall_risk, required_risk, eligible, is_eligible properties
"""

from dataclasses import fields
from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, RiskAxis
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef


VALID_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _make_provenance_ref() -> RiskEvidenceProvenanceRef:
    return RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256=VALID_SHA256)


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


def _make_availability_ref(
    available_at: datetime = datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc),
) -> RiskEvidenceAvailabilityRef:
    return RiskEvidenceAvailabilityRef(
        provenance_ref=_make_provenance_ref(),
        available_at=available_at,
    )


class SubclassedRiskAxisContext(RiskAxisContext):
    pass


class SubclassedAvailabilityRef(RiskEvidenceAvailabilityRef):
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


class LateFailingTz(tzinfo):
    """Passes initial construction checks (2 calls), but raises on subsequent conversion."""
    def __init__(self):
        self._count = 0

    def utcoffset(self, dt):
        self._count += 1
        if self._count >= 3:
            raise RuntimeError("Failing on subsequent conversion")
        return timedelta(hours=2)

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "LateFailingTz"


class TestRiskEvidencePITBindingContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        ctx = _make_risk_axis_context()
        avail = _make_availability_ref()
        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)

        f_names = [f.name for f in fields(binding)]
        assert f_names == ["context", "availability_ref"]

        with pytest.raises(AttributeError):
            binding.context = ctx  # type: ignore

        with pytest.raises(AttributeError):
            binding.availability_ref = avail  # type: ignore

    def test_preserves_supplied_objects_by_identity(self):
        ctx = _make_risk_axis_context()
        avail = _make_availability_ref()
        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)

        assert binding.context is ctx
        assert binding.availability_ref is avail

    @pytest.mark.parametrize("axis", list(RiskAxis))
    @pytest.mark.parametrize("mode", list(AsOfMode))
    def test_supports_all_axes_and_modes(self, axis, mode):
        cutoff = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)

        ctx = _make_risk_axis_context(axis=axis, mode=mode, cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)
        assert binding.context.axis is axis
        assert binding.context.temporal_context.pit_context.mode is mode

    def test_accepts_earlier_instant(self):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 9, 59, 59, 999999, tzinfo=timezone.utc)
        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)
        assert binding.availability_ref.available_at == available_at

    def test_accepts_exact_equality(self):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, 123456, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 10, 0, 0, 123456, tzinfo=timezone.utc)
        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)
        assert binding.availability_ref.available_at == cutoff

    def test_accepts_same_instant_different_offsets(self):
        # 13:00 UTC+3 is exactly 10:00 UTC
        tz_plus_3 = timezone(timedelta(hours=3))
        tz_minus_5 = timezone(timedelta(hours=-5))

        cutoff = datetime(2026, 8, 28, 5, 0, 0, tzinfo=tz_minus_5)  # 10:00 UTC
        available_at = datetime(2026, 8, 28, 13, 0, 0, tzinfo=tz_plus_3)  # 10:00 UTC

        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)
        assert binding.context is ctx
        assert binding.availability_ref is avail

    def test_no_convenience_or_decision_properties(self):
        ctx = _make_risk_axis_context()
        avail = _make_availability_ref()
        binding = RiskEvidencePITBinding(context=ctx, availability_ref=avail)

        prohibited_attrs = [
            "value",
            "score",
            "level",
            "status",
            "present",
            "verified",
            "authentic",
            "complete",
            "sufficient",
            "owner_id",
            "owner",
            "suitability",
            "overall_risk",
            "required_risk",
            "eligible",
            "is_eligible",
            "available_at_utc",
            "knowledge_cutoff_utc",
        ]
        for attr in prohibited_attrs:
            assert not hasattr(binding, attr), f"RiskEvidencePITBinding must not have property '{attr}'"


class TestRiskEvidencePITBindingValidation:
    def test_rejects_subclass_context(self):
        t_ctx = _make_risk_axis_context().temporal_context
        sub_ctx = SubclassedRiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=t_ctx)
        avail = _make_availability_ref()

        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            RiskEvidencePITBinding(context=sub_ctx, availability_ref=avail)

    def test_rejects_subclass_availability_ref(self):
        ctx = _make_risk_axis_context()
        sub_avail = SubclassedAvailabilityRef(
            provenance_ref=_make_provenance_ref(),
            available_at=datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(TypeError, match=r"^availability_ref must be an exact RiskEvidenceAvailabilityRef instance$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=sub_avail)

    @pytest.mark.parametrize(
        "bad_ctx",
        [
            None,
            True,
            False,
            "context",
            123,
            RiskAxis.TOLERANCE,
            _make_risk_axis_context().temporal_context,
            {"context": _make_risk_axis_context()},
            object(),
        ],
    )
    def test_rejects_invalid_context_types(self, bad_ctx):
        avail = _make_availability_ref()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            RiskEvidencePITBinding(context=bad_ctx, availability_ref=avail)  # type: ignore

    @pytest.mark.parametrize(
        "bad_avail",
        [
            None,
            True,
            False,
            "availability_ref",
            123,
            _make_provenance_ref(),
            datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc),
            {"availability_ref": _make_availability_ref()},
            object(),
        ],
    )
    def test_rejects_invalid_availability_ref_types(self, bad_avail):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^availability_ref must be an exact RiskEvidenceAvailabilityRef instance$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=bad_avail)  # type: ignore

    def test_rejects_swapped_arguments(self):
        ctx = _make_risk_axis_context()
        avail = _make_availability_ref()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            RiskEvidencePITBinding(context=avail, availability_ref=ctx)  # type: ignore

    def test_rejects_one_microsecond_lookahead(self):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, 0, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 10, 0, 0, 1, tzinfo=timezone.utc)

        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        with pytest.raises(ValueError, match=r"^risk evidence availability exceeds the analysis knowledge cutoff$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=avail)

    def test_rejects_larger_lookahead(self):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 11, 0, 0, tzinfo=timezone.utc)

        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        with pytest.raises(ValueError, match=r"^risk evidence availability exceeds the analysis knowledge cutoff$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=avail)

    def test_rejects_deceptive_local_wall_time_lookahead(self):
        # available_at has local wall time 09:00 (UTC+0) -> 09:00 UTC
        # cutoff has local wall time 10:00 (UTC+3) -> 07:00 UTC
        # Local 09:00 appears earlier than local 10:00, but UTC 09:00 > UTC 07:00 (Lookahead!)
        tz_plus_3 = timezone(timedelta(hours=3))
        cutoff = datetime(2026, 8, 28, 10, 0, 0, tzinfo=tz_plus_3)  # 07:00 UTC
        available_at = datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc)  # 09:00 UTC

        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        with pytest.raises(ValueError, match=r"^risk evidence availability exceeds the analysis knowledge cutoff$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=avail)

    def test_adversarial_repr_context(self):
        avail = _make_availability_ref()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            RiskEvidencePITBinding(context=AdversarialReprObject(), availability_ref=avail)  # type: ignore

    def test_adversarial_repr_availability_ref(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^availability_ref must be an exact RiskEvidenceAvailabilityRef instance$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=AdversarialReprObject())  # type: ignore

    def test_adversarial_metaclass_context(self):
        avail = _make_availability_ref()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            RiskEvidencePITBinding(context=AdversarialMetaClass(), availability_ref=avail)  # type: ignore

    def test_adversarial_metaclass_availability_ref(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(TypeError, match=r"^availability_ref must be an exact RiskEvidenceAvailabilityRef instance$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=AdversarialMetaClass())  # type: ignore

    def test_late_failing_conversion_raises_static_type_error(self):
        # Create availability_ref with LateFailingTz that passes construction but fails later
        failing_tz = LateFailingTz()
        dt = datetime(2026, 8, 28, 8, 0, 0, tzinfo=failing_tz)
        avail = RiskEvidenceAvailabilityRef(provenance_ref=_make_provenance_ref(), available_at=dt)
        ctx = _make_risk_axis_context()

        with pytest.raises(TypeError, match=r"^risk evidence PIT instants must remain valid for UTC comparison$"):
            RiskEvidencePITBinding(context=ctx, availability_ref=avail)
