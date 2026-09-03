"""
backend/tests/test_risk_evidence_availability.py
================================================
TDD unit test suite for RiskEvidenceAvailabilityRef (Phase 15B.5).

Covers:
- RiskEvidenceAvailabilityRef frozen dataclass invariants with exactly two stored fields:
  (provenance_ref, available_at)
- Concrete type validation:
  * `type(provenance_ref) is RiskEvidenceProvenanceRef`
  * `type(available_at) is datetime`
- Strict timezone-aware validation:
  * Rejects naive datetime
  * Rejects wrong offset returns (None, str, int, float, bool, object)
  * Rejects offsets equal to or beyond +-24h (>= 24h or <= -24h)
  * Rejects tzinfo raising TypeError, ValueError, RuntimeError, etc. during utcoffset or astimezone(timezone.utc)
- Preserves both supplied objects by identity (`is`) without normalization or caching
- Static callback-safe error messages with anchored regex matching:
  * r"^provenance_ref must be an exact RiskEvidenceProvenanceRef instance$"
  * r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"
- Adversarial test doubles (exploding __repr__, __str__, metaclass __name__, failing tzinfo)
- Purity invariants:
  * No context, axis, value, score, level, status, effective_at, recorded_at, owner_id, suitability, present, verified, PIT-eligibility properties
"""

from dataclasses import fields
from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef


VALID_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _make_provenance_ref() -> RiskEvidenceProvenanceRef:
    return RiskEvidenceProvenanceRef(source_key="survey_v1", content_sha256=VALID_SHA256)


class SubclassedProvenanceRef(RiskEvidenceProvenanceRef):
    pass


class SubclassedDatetime(datetime):
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


class NullOffsetTz(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NullTz"


class CustomValOffsetTz(tzinfo):
    def __init__(self, offset_val):
        self._offset_val = offset_val

    def utcoffset(self, dt):
        return self._offset_val

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "CustomValOffsetTz"


class ErrorOffsetTz(tzinfo):
    def __init__(self, exc: Exception):
        self._exc = exc

    def utcoffset(self, dt):
        raise self._exc

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "ErrorTz"


class ConversionFailingTz(tzinfo):
    def __init__(self):
        self._count = 0

    def utcoffset(self, dt):
        self._count += 1
        if self._count > 1:
            raise RuntimeError("Failed during UTC conversion")
        return timedelta(hours=2)

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "ConversionFailingTz"


class TestRiskEvidenceAvailabilityRefContract:
    def test_context_is_frozen_dataclass_with_exact_fields(self):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        ref = RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

        f_names = [f.name for f in fields(ref)]
        assert f_names == ["provenance_ref", "available_at"]

        with pytest.raises(AttributeError):
            ref.provenance_ref = pref  # type: ignore

        with pytest.raises(AttributeError):
            ref.available_at = dt  # type: ignore

    def test_preserves_supplied_objects_by_identity(self):
        pref = _make_provenance_ref()
        tz_plus_3 = timezone(timedelta(hours=3, minutes=30))
        dt = datetime(2026, 9, 3, 11, 45, 12, 345678, tzinfo=tz_plus_3, fold=1)
        ref = RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

        assert ref.provenance_ref is pref
        assert ref.available_at is dt
        assert ref.available_at.tzinfo is tz_plus_3
        assert ref.available_at.microsecond == 345678
        assert ref.available_at.fold == 1

    @pytest.mark.parametrize(
        "offset",
        [
            timedelta(0),
            timedelta(hours=5),
            timedelta(hours=-7),
            timedelta(hours=5, minutes=45),
            timedelta(hours=-9, minutes=-30),
            timedelta(hours=23, minutes=59),
            timedelta(hours=-23, minutes=-59),
        ],
    )
    def test_supports_various_valid_timezone_offsets(self, offset):
        pref = _make_provenance_ref()
        tz = timezone(offset)
        dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=tz)
        ref = RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)
        assert ref.available_at is dt

    def test_no_convenience_or_decision_properties(self):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        ref = RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

        prohibited_attrs = [
            "context",
            "axis",
            "value",
            "score",
            "level",
            "status",
            "effective_at",
            "effective_date",
            "recorded_at",
            "owner_id",
            "owner",
            "suitability",
            "present",
            "verified",
            "eligible",
            "is_eligible",
            "knowledge_cutoff",
            "as_of_date",
            "source_key",
            "content_sha256",
        ]
        for attr in prohibited_attrs:
            assert not hasattr(ref, attr), f"RiskEvidenceAvailabilityRef must not have property '{attr}'"


class TestRiskEvidenceAvailabilityRefValidation:
    def test_rejects_subclass_provenance_ref(self):
        sub_pref = SubclassedProvenanceRef(source_key="survey_v1", content_sha256=VALID_SHA256)
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match=r"^provenance_ref must be an exact RiskEvidenceProvenanceRef instance$"):
            RiskEvidenceAvailabilityRef(provenance_ref=sub_pref, available_at=dt)

    def test_rejects_subclass_datetime(self):
        pref = _make_provenance_ref()
        sub_dt = SubclassedDatetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=sub_dt)

    @pytest.mark.parametrize(
        "bad_pref",
        [
            None,
            True,
            False,
            "survey_v1",
            123,
            {"source_key": "survey_v1"},
            object(),
        ],
    )
    def test_rejects_invalid_provenance_ref_types(self, bad_pref):
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match=r"^provenance_ref must be an exact RiskEvidenceProvenanceRef instance$"):
            RiskEvidenceAvailabilityRef(provenance_ref=bad_pref, available_at=dt)  # type: ignore

    @pytest.mark.parametrize(
        "bad_dt",
        [
            None,
            True,
            False,
            "2026-09-03T08:30:00Z",
            date(2026, 9, 3),
            1725352200,
            {"available_at": datetime.now(timezone.utc)},
            object(),
        ],
    )
    def test_rejects_invalid_available_at_types(self, bad_dt):
        pref = _make_provenance_ref()
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=bad_dt)  # type: ignore

    def test_rejects_naive_datetime(self):
        pref = _make_provenance_ref()
        naive_dt = datetime(2026, 9, 3, 8, 30, 0)
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=naive_dt)

    def test_rejects_null_offset_tzinfo(self):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=NullOffsetTz())
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

    @pytest.mark.parametrize(
        "bad_offset",
        [
            "UTC+3",
            10800,
            3.5,
            True,
            False,
            object(),
        ],
    )
    def test_rejects_wrong_offset_types(self, bad_offset):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=CustomValOffsetTz(bad_offset))
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

    @pytest.mark.parametrize(
        "out_of_range_offset",
        [
            timedelta(hours=24),
            timedelta(hours=-24),
            timedelta(hours=25),
            timedelta(hours=-25),
            timedelta(days=2),
        ],
    )
    def test_rejects_out_of_range_offsets(self, out_of_range_offset):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=CustomValOffsetTz(out_of_range_offset))
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("Custom utcoffset type error"),
            ValueError("Custom utcoffset value error"),
            RuntimeError("Custom utcoffset runtime error"),
        ],
    )
    def test_rejects_tzinfo_raising_exceptions(self, exc):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=ErrorOffsetTz(exc))
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

    def test_rejects_tzinfo_failing_during_utc_conversion(self):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=ConversionFailingTz())
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=dt)

    def test_rejects_swapped_arguments(self):
        pref = _make_provenance_ref()
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match=r"^provenance_ref must be an exact RiskEvidenceProvenanceRef instance$"):
            RiskEvidenceAvailabilityRef(provenance_ref=dt, available_at=pref)  # type: ignore

    def test_adversarial_repr_provenance_ref(self):
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match=r"^provenance_ref must be an exact RiskEvidenceProvenanceRef instance$"):
            RiskEvidenceAvailabilityRef(provenance_ref=AdversarialReprObject(), available_at=dt)  # type: ignore

    def test_adversarial_repr_available_at(self):
        pref = _make_provenance_ref()
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=AdversarialReprObject())  # type: ignore

    def test_adversarial_metaclass_provenance_ref(self):
        dt = datetime(2026, 9, 3, 8, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match=r"^provenance_ref must be an exact RiskEvidenceProvenanceRef instance$"):
            RiskEvidenceAvailabilityRef(provenance_ref=AdversarialMetaClass(), available_at=dt)  # type: ignore

    def test_adversarial_metaclass_available_at(self):
        pref = _make_provenance_ref()
        with pytest.raises(TypeError, match=r"^available_at must be an exact timezone-aware datetime with a valid UTC offset$"):
            RiskEvidenceAvailabilityRef(provenance_ref=pref, available_at=AdversarialMetaClass())  # type: ignore
