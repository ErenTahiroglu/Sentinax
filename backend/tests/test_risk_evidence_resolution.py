"""
backend/tests/test_risk_evidence_resolution.py
==============================================
TDD unit test suite for resolve_risk_evidence_reference (Phase 15B.7).

Covers:
- RiskEvidenceReferenceResolution type alias union members:
  MissingRiskEvidence | RiskEvidencePITBinding
- Function signature:
  * Keyword-only parameters: context, availability_ref, missing_inputs
  * No defaults
- Branch selection truth table:
  * availability_ref is set, missing_inputs is None -> RiskEvidencePITBinding
  * availability_ref is None, missing_inputs is non-None -> MissingRiskEvidence
  * Both set -> ValueError("exactly one risk evidence reference resolution branch must be supplied")
  * Both None -> ValueError("exactly one risk evidence reference resolution branch must be supplied")
- Concrete type validation:
  * context must be exact RiskAxisContext instance
  * availability_ref must be None or exact RiskEvidenceAvailabilityRef instance
- Error propagation without translation:
  * Lookahead ValueError propagates unchanged from RiskEvidencePITBinding
  * Late timezone conversion TypeError propagates unchanged from RiskEvidencePITBinding
  * MissingRiskEvidence tuple/str/duplicate errors propagate unchanged
- Identity preservation for supplied objects
- Static callback-safe error messages with anchored regex matching
- Purity invariants (no clock, no scores, no conversion of missing to zero, no flags)
"""

import dataclasses
import hashlib
import inspect
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import get_args

import pytest

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, Horizon, RiskAxis
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_availability import RiskEvidenceAvailabilityRef
from backend.engine.private.risk_evidence_content_match import RiskEvidenceContentMatch
from backend.engine.private.risk_evidence_pit_binding import RiskEvidencePITBinding
from backend.engine.private.risk_evidence_provenance import RiskEvidenceProvenanceRef
from backend.engine.private.risk_evidence_resolution import (
    RiskEvidenceContentResolution,
    RiskEvidenceReferenceResolution,
    resolve_risk_evidence_content,
    resolve_risk_evidence_reference,
)


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
    def __init__(self):
        self._count = 0

    def utcoffset(self, dt):
        self._count += 1
        if self._count >= 3:
            raise RuntimeError("Failing during late UTC conversion")
        return timedelta(hours=2)

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "LateFailingTz"


class TestRiskEvidenceResolutionContract:
    def test_type_alias_union_members(self):
        union_args = get_args(RiskEvidenceReferenceResolution)
        assert set(union_args) == {MissingRiskEvidence, RiskEvidencePITBinding}

    def test_parameters_are_keyword_only_without_defaults(self):
        sig = inspect.signature(resolve_risk_evidence_reference)
        params = sig.parameters
        assert list(params.keys()) == ["context", "availability_ref", "missing_inputs"]

        for param in params.values():
            assert param.kind == inspect.Parameter.KEYWORD_ONLY
            assert param.default == inspect.Parameter.empty

    def test_missing_branch_success_and_identity_preservation(self):
        ctx = _make_risk_axis_context()
        missing = ("annual_income", "liquid_net_worth")

        res = resolve_risk_evidence_reference(
            context=ctx,
            availability_ref=None,
            missing_inputs=missing,
        )

        assert type(res) is MissingRiskEvidence
        assert res.context is ctx
        assert res.missing_inputs is missing

    def test_reference_branch_success_and_identity_preservation(self):
        ctx = _make_risk_axis_context()
        avail = _make_availability_ref()

        res = resolve_risk_evidence_reference(
            context=ctx,
            availability_ref=avail,
            missing_inputs=None,
        )

        assert type(res) is RiskEvidencePITBinding
        assert res.context is ctx
        assert res.availability_ref is avail

    @pytest.mark.parametrize("axis", list(RiskAxis))
    @pytest.mark.parametrize("mode", list(AsOfMode))
    def test_supports_all_axes_and_modes_in_both_branches(self, axis, mode):
        cutoff = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)

        ctx = _make_risk_axis_context(axis=axis, mode=mode, cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)
        missing = ("annual_income",)

        # Missing branch
        res_missing = resolve_risk_evidence_reference(
            context=ctx,
            availability_ref=None,
            missing_inputs=missing,
        )
        assert type(res_missing) is MissingRiskEvidence
        assert res_missing.context.axis is axis
        assert res_missing.context.temporal_context.pit_context.mode is mode

        # Reference branch
        res_ref = resolve_risk_evidence_reference(
            context=ctx,
            availability_ref=avail,
            missing_inputs=None,
        )
        assert type(res_ref) is RiskEvidencePITBinding
        assert res_ref.context.axis is axis
        assert res_ref.context.temporal_context.pit_context.mode is mode


class TestRiskEvidenceResolutionValidation:
    def test_rejects_subclass_context(self):
        t_ctx = _make_risk_axis_context().temporal_context
        sub_ctx = SubclassedRiskAxisContext(axis=RiskAxis.TOLERANCE, temporal_context=t_ctx)
        avail = _make_availability_ref()

        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            resolve_risk_evidence_reference(
                context=sub_ctx,
                availability_ref=avail,
                missing_inputs=None,
            )

    def test_rejects_subclass_availability_ref(self):
        ctx = _make_risk_axis_context()
        sub_avail = SubclassedAvailabilityRef(
            provenance_ref=_make_provenance_ref(),
            available_at=datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(
            TypeError,
            match=r"^availability_ref must be None or an exact RiskEvidenceAvailabilityRef instance$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=sub_avail,
                missing_inputs=None,
            )

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
            resolve_risk_evidence_reference(
                context=bad_ctx,  # type: ignore
                availability_ref=avail,
                missing_inputs=None,
            )

    @pytest.mark.parametrize(
        "bad_avail",
        [
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
        with pytest.raises(
            TypeError,
            match=r"^availability_ref must be None or an exact RiskEvidenceAvailabilityRef instance$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=bad_avail,  # type: ignore
                missing_inputs=None,
            )

    def test_rejects_both_branches_supplied(self):
        ctx = _make_risk_axis_context()
        avail = _make_availability_ref()
        missing = ("annual_income",)

        with pytest.raises(
            ValueError,
            match=r"^exactly one risk evidence reference resolution branch must be supplied$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=avail,
                missing_inputs=missing,
            )

    def test_rejects_neither_branch_supplied(self):
        ctx = _make_risk_axis_context()

        with pytest.raises(
            ValueError,
            match=r"^exactly one risk evidence reference resolution branch must be supplied$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=None,
                missing_inputs=None,
            )

    def test_propagates_lookahead_value_error_unchanged(self):
        cutoff = datetime(2026, 8, 28, 10, 0, 0, 0, tzinfo=timezone.utc)
        available_at = datetime(2026, 8, 28, 10, 0, 0, 1, tzinfo=timezone.utc)

        ctx = _make_risk_axis_context(cutoff_dt=cutoff)
        avail = _make_availability_ref(available_at=available_at)

        with pytest.raises(
            ValueError,
            match=r"^risk evidence availability exceeds the analysis knowledge cutoff$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=avail,
                missing_inputs=None,
            )

    def test_propagates_late_timezone_type_error_unchanged(self):
        failing_tz = LateFailingTz()
        dt = datetime(2026, 8, 28, 8, 0, 0, tzinfo=failing_tz)
        avail = RiskEvidenceAvailabilityRef(provenance_ref=_make_provenance_ref(), available_at=dt)
        ctx = _make_risk_axis_context()

        with pytest.raises(
            TypeError,
            match=r"^risk evidence PIT instants must remain valid for UTC comparison$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=avail,
                missing_inputs=None,
            )

    @pytest.mark.parametrize(
        "bad_missing, match_regex",
        [
            ([], r"^missing_inputs must be an exact non-empty tuple$"),
            ((), r"^missing_inputs must be an exact non-empty tuple$"),
            (("income", ""), r"^each missing input must be an exact non-empty canonical string$"),
            (("income", "  "), r"^each missing input must be an exact non-empty canonical string$"),
            (("income", " income"), r"^each missing input must be an exact non-empty canonical string$"),
            (("income", "income "), r"^each missing input must be an exact non-empty canonical string$"),
            (("income", "income"), r"^missing_inputs must contain unique entries$"),
            (object(), r"^missing_inputs must be an exact non-empty tuple$"),
        ],
    )
    def test_propagates_missing_risk_evidence_validation_errors_unchanged(self, bad_missing, match_regex):
        ctx = _make_risk_axis_context()

        with pytest.raises(TypeError, match=match_regex):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=None,
                missing_inputs=bad_missing,  # type: ignore
            )

    def test_adversarial_repr_context(self):
        avail = _make_availability_ref()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            resolve_risk_evidence_reference(
                context=AdversarialReprObject(),  # type: ignore
                availability_ref=avail,
                missing_inputs=None,
            )

    def test_adversarial_repr_availability_ref(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(
            TypeError,
            match=r"^availability_ref must be None or an exact RiskEvidenceAvailabilityRef instance$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=AdversarialReprObject(),  # type: ignore
                missing_inputs=None,
            )

    def test_adversarial_metaclass_context(self):
        avail = _make_availability_ref()
        with pytest.raises(TypeError, match=r"^context must be an exact RiskAxisContext instance$"):
            resolve_risk_evidence_reference(
                context=AdversarialMetaClass(),  # type: ignore
                availability_ref=avail,
                missing_inputs=None,
            )

    def test_adversarial_metaclass_availability_ref(self):
        ctx = _make_risk_axis_context()
        with pytest.raises(
            TypeError,
            match=r"^availability_ref must be None or an exact RiskEvidenceAvailabilityRef instance$",
        ):
            resolve_risk_evidence_reference(
                context=ctx,
                availability_ref=AdversarialMetaClass(),  # type: ignore
                missing_inputs=None,
            )


# ---------------------------------------------------------------------------
# Phase 15B.9 — content-qualified second-stage resolution
# ---------------------------------------------------------------------------

_INVALID_RESOLUTION_MSG = r"^reference_resolution must be an exact MissingRiskEvidence or RiskEvidencePITBinding instance$"
_BRANCH_MSG = r"^content must be supplied exactly for the PIT-admissible reference branch$"


class SubclassedMissingRiskEvidence(MissingRiskEvidence):
    pass


class SubclassedPITBinding(RiskEvidencePITBinding):
    pass


class BytesSubclass(bytes):
    pass


class HostileValue:
    def __repr__(self):
        raise RuntimeError("hostile repr")

    def __str__(self):
        raise RuntimeError("hostile str")

    def __len__(self):
        raise RuntimeError("hostile len")


def _binding_for(content: bytes) -> RiskEvidencePITBinding:
    availability = RiskEvidenceAvailabilityRef(
        provenance_ref=RiskEvidenceProvenanceRef(
            source_key="survey_v1", content_sha256=hashlib.sha256(content).hexdigest()
        ),
        available_at=datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc),
    )
    return RiskEvidencePITBinding(context=_make_risk_axis_context(), availability_ref=availability)


def _missing() -> MissingRiskEvidence:
    return MissingRiskEvidence(context=_make_risk_axis_context(), missing_inputs=("questionnaire",))


class TestContentQualifiedResolution:
    def test_alias_members(self):
        assert set(get_args(RiskEvidenceContentResolution)) == {MissingRiskEvidence, RiskEvidenceContentMatch}
        assert len(get_args(RiskEvidenceContentResolution)) == 2

    def test_signature_keyword_only_no_defaults(self):
        params = inspect.signature(resolve_risk_evidence_content).parameters
        assert list(params) == ["reference_resolution", "content"]
        for p in params.values():
            assert p.kind is inspect.Parameter.KEYWORD_ONLY
            assert p.default is inspect.Parameter.empty

    # --- missing branch ---
    def test_missing_with_none_returns_same_object(self):
        missing = _missing()
        result = resolve_risk_evidence_content(reference_resolution=missing, content=None)
        assert result is missing

    @pytest.mark.parametrize("content", [b"", b"abc", 0, "", HostileValue(), bytearray(b"x")])
    def test_missing_with_non_none_rejects(self, content):
        with pytest.raises(ValueError, match=_BRANCH_MSG):
            resolve_risk_evidence_content(reference_resolution=_missing(), content=content)

    # --- reference branch ---
    def test_binding_with_matching_bytes(self):
        content = b"risk evidence bytes"
        binding = _binding_for(content)
        result = resolve_risk_evidence_content(reference_resolution=binding, content=content)
        assert type(result) is RiskEvidenceContentMatch
        assert result.pit_binding is binding
        assert result.content_length == len(content)

    def test_binding_with_empty_bytes_matches_empty_digest(self):
        binding = _binding_for(b"")
        assert binding.availability_ref.provenance_ref.content_sha256 == VALID_SHA256
        result = resolve_risk_evidence_content(reference_resolution=binding, content=b"")
        assert type(result) is RiskEvidenceContentMatch
        assert result.content_length == 0

    def test_binding_with_none_rejects(self):
        with pytest.raises(ValueError, match=_BRANCH_MSG):
            resolve_risk_evidence_content(reference_resolution=_binding_for(b"x"), content=None)

    # --- delegation / propagation unchanged from 15B.8 ---
    def test_digest_mismatch_propagates(self):
        with pytest.raises(ValueError, match=r"^content digest mismatch$"):
            resolve_risk_evidence_content(reference_resolution=_binding_for(b"a"), content=b"b")

    @pytest.mark.parametrize(
        "content",
        [bytearray(b"a"), memoryview(b"a"), "a", True, 1, BytesSubclass(b"a"), HostileValue()],
    )
    def test_invalid_content_types_propagate(self, content):
        with pytest.raises(TypeError, match=r"^content must be exact bytes$"):
            resolve_risk_evidence_content(reference_resolution=_binding_for(b"a"), content=content)

    # --- invalid resolution objects ---
    @pytest.mark.parametrize(
        "bad",
        [
            None, True, 0, "x", {}, (),
            _make_availability_ref(),
            object(),
        ],
    )
    def test_invalid_resolution_rejected(self, bad):
        with pytest.raises(TypeError, match=_INVALID_RESOLUTION_MSG):
            resolve_risk_evidence_content(reference_resolution=bad, content=None)

    def test_content_match_is_not_a_valid_resolution(self):
        match = resolve_risk_evidence_content(reference_resolution=_binding_for(b"a"), content=b"a")
        with pytest.raises(TypeError, match=_INVALID_RESOLUTION_MSG):
            resolve_risk_evidence_content(reference_resolution=match, content=b"a")

    def test_missing_subclass_rejected(self):
        sub = SubclassedMissingRiskEvidence(context=_make_risk_axis_context(), missing_inputs=("q",))
        with pytest.raises(TypeError, match=_INVALID_RESOLUTION_MSG):
            resolve_risk_evidence_content(reference_resolution=sub, content=None)

    def test_binding_subclass_rejected(self):
        b = _binding_for(b"a")
        sub = SubclassedPITBinding(context=b.context, availability_ref=b.availability_ref)
        with pytest.raises(TypeError, match=_INVALID_RESOLUTION_MSG):
            resolve_risk_evidence_content(reference_resolution=sub, content=b"a")

    # --- callback safety ---
    def test_hostile_repr_and_str_not_invoked(self):
        with pytest.raises(TypeError, match=_INVALID_RESOLUTION_MSG):
            resolve_risk_evidence_content(reference_resolution=HostileValue(), content=HostileValue())

    def test_hostile_metaclass_name_not_invoked(self):
        with pytest.raises(TypeError, match=_INVALID_RESOLUTION_MSG):
            resolve_risk_evidence_content(
                reference_resolution=AdversarialMetaClass(), content=AdversarialReprObject()
            )

    def test_hostile_content_on_missing_branch_not_invoked(self):
        with pytest.raises(ValueError, match=_BRANCH_MSG):
            resolve_risk_evidence_content(reference_resolution=_missing(), content=HostileValue())

    # --- semantic boundary ---
    def test_no_raw_content_retention(self):
        content = b"secret-evidence-bytes"
        result = resolve_risk_evidence_content(reference_resolution=_binding_for(content), content=content)
        assert [f.name for f in dataclasses.fields(result)] == ["pit_binding", "content_length"]
        assert content not in repr(result).encode()
        assert content not in repr(vars(result)).encode()

    def test_no_score_level_or_suitability(self):
        result = resolve_risk_evidence_content(reference_resolution=_binding_for(b"a"), content=b"a")
        for attr in ("score", "level", "suitability", "risk", "default", "value"):
            assert not hasattr(result, attr)

    def test_missing_never_becomes_zero_or_default(self):
        missing = _missing()
        result = resolve_risk_evidence_content(reference_resolution=missing, content=None)
        assert type(result) is MissingRiskEvidence
        for falsy in (0, 0.0, False, b"", "", None):
            assert result is not falsy
