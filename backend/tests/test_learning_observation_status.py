"""Phase 28B-0: observation status. Absence from a captured response is not a missing economic observation and never zero."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backend.engine.learning.observation_status import (
    CapturedObservation,
    CapturedObservationSet,
    ObservationOrigin,
    ObservationStatus,
)
from backend.engine.learning.temporal_provenance import TemporalProvenance
from backend.engine.learning.universe_coverage import LearningSubjectRef
from backend.tests.learning_support import UUID_1, UUID_2, plus, provenance_kwargs


def obs(**over) -> CapturedObservation:
    base = dict(subject=LearningSubjectRef("AAA", UUID_1), economic_date=date(2026, 10, 9), status=ObservationStatus.OBSERVED_IN_RESPONSE,
                origin=ObservationOrigin.FORWARD_CAPTURE, provenance=TemporalProvenance(**provenance_kwargs()), observed_unit_price=Decimal("1.234567"))
    base.update(over)
    return CapturedObservation(**base)


def not_observed(**over):
    base = dict(status=ObservationStatus.NOT_OBSERVED_IN_RESPONSE, observed_unit_price=None)
    base.update(over)
    return obs(**base)


def test_vocabulary_has_no_missing_or_zero_member() -> None:
    names = {s.name for s in ObservationStatus}
    assert names == {"OBSERVED_IN_RESPONSE", "NOT_OBSERVED_IN_RESPONSE", "EXPLICITLY_UNAVAILABLE_BY_SOURCE", "UNKNOWN", "RETRIEVAL_FAILED"}
    assert not any("MISSING" in n or "ZERO" in n or "ABSENT" in n for n in names)


def test_observed_requires_exact_positive_finite_decimal() -> None:
    for bad in (None, 1.5, 1, "1.2", Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises((TypeError, ValueError)):
            obs(observed_unit_price=bad)


def test_non_observed_statuses_carry_no_value_not_even_zero() -> None:
    for st in (ObservationStatus.NOT_OBSERVED_IN_RESPONSE, ObservationStatus.UNKNOWN):
        assert obs(status=st, observed_unit_price=None).observed_unit_price is None
        with pytest.raises(ValueError):
            obs(status=st, observed_unit_price=Decimal("0"))
        with pytest.raises(ValueError):
            obs(status=st, observed_unit_price=Decimal("1"))


def test_not_observed_does_not_establish_economic_absence() -> None:
    o = not_observed()
    assert o.establishes_economic_absence is False
    assert o.establishes_lifecycle_event is False
    assert obs().establishes_economic_absence is False


def test_explicit_unavailability_requires_semantics_label_and_evidence_references() -> None:
    from backend.tests.learning_support import HASH_A
    base = dict(status=ObservationStatus.EXPLICITLY_UNAVAILABLE_BY_SOURCE, observed_unit_price=None)
    with pytest.raises(ValueError):
        not_observed(**base)                                                                  # nothing
    with pytest.raises(ValueError):
        not_observed(**base, source_semantics_ref="tefas.docs.price-zero-meaning")           # free-form label alone
    with pytest.raises(ValueError):
        not_observed(**base, source_semantics_evidence_sha256s=(HASH_A,))                    # evidence without the semantics label
    o = not_observed(**base, source_semantics_ref="tefas.docs.price-zero-meaning", source_semantics_evidence_sha256s=(HASH_A,))
    assert o.source_semantics_evidence_sha256s == (HASH_A,)


def test_explicit_unavailability_is_never_verified_absence() -> None:
    from backend.tests.learning_support import HASH_A
    o = not_observed(status=ObservationStatus.EXPLICITLY_UNAVAILABLE_BY_SOURCE, source_semantics_ref="tefas.docs.x", source_semantics_evidence_sha256s=(HASH_A,))
    assert o.source_semantics_verified is False and o.establishes_economic_absence is False and o.establishes_lifecycle_event is False


def test_semantics_fields_only_valid_with_explicit_unavailability() -> None:
    from backend.tests.learning_support import HASH_A
    with pytest.raises(ValueError):
        obs(source_semantics_ref="tefas.docs.x")
    with pytest.raises(ValueError):
        obs(source_semantics_evidence_sha256s=(HASH_A,))


def test_semantics_evidence_hashes_validated_and_unique() -> None:
    from backend.tests.learning_support import HASH_A
    kw = dict(status=ObservationStatus.EXPLICITLY_UNAVAILABLE_BY_SOURCE, observed_unit_price=None, source_semantics_ref="tefas.docs.x")
    with pytest.raises(ValueError):
        not_observed(**kw, source_semantics_evidence_sha256s=(HASH_A, HASH_A))
    with pytest.raises(ValueError):
        not_observed(**kw, source_semantics_evidence_sha256s=("zz",))


def test_retrieval_failure_requires_reason_and_forbids_publication_and_value() -> None:
    with pytest.raises(ValueError):
        not_observed(status=ObservationStatus.RETRIEVAL_FAILED)
    o = not_observed(status=ObservationStatus.RETRIEVAL_FAILED, failure_reason="http-503")
    assert o.failure_reason == "http-503"
    with pytest.raises(ValueError):
        obs(failure_reason="http-503")


def test_enum_and_type_strictness() -> None:
    for field, bad in (("status", "observed"), ("origin", "forward"), ("economic_date", "2026-10-09"), ("subject", "AAA"), ("provenance", None)):
        with pytest.raises(TypeError):
            obs(**{field: bad})


def test_old_economic_date_stays_old_and_retrospective_must_be_declared() -> None:
    o = obs(economic_date=date(2021, 3, 4), origin=ObservationOrigin.RETROSPECTIVE_IN_RESPONSE, provenance=TemporalProvenance(**provenance_kwargs(economic_date=date(2021, 3, 4))))
    assert o.economic_date == date(2021, 3, 4) and o.provenance.system_known_at_utc == plus(60)
    assert o.origin is not ObservationOrigin.FORWARD_CAPTURE


def test_economic_date_must_match_provenance_economic_date() -> None:
    with pytest.raises(ValueError):
        obs(economic_date=date(2026, 10, 8))


def test_duplicate_observations_rejected_in_set() -> None:
    with pytest.raises(ValueError):
        CapturedObservationSet((obs(), obs()))
    with pytest.raises(ValueError):   # same key, conflicting status
        CapturedObservationSet((obs(), not_observed()))


def test_set_rejects_identity_ambiguity_across_observations() -> None:
    with pytest.raises(ValueError):
        CapturedObservationSet((obs(), obs(subject=LearningSubjectRef("BBB", UUID_1), provenance=TemporalProvenance(**provenance_kwargs(retrieved_at=plus(90))))))
    with pytest.raises(ValueError):
        CapturedObservationSet((obs(), obs(subject=LearningSubjectRef("AAA", UUID_2), provenance=TemporalProvenance(**provenance_kwargs(retrieved_at=plus(90))))))


def test_set_requires_tuple_of_observations_and_is_deterministic() -> None:
    with pytest.raises(TypeError):
        CapturedObservationSet([obs()])
    with pytest.raises(TypeError):
        CapturedObservationSet((obs(), "x"))
    later = obs(provenance=TemporalProvenance(**provenance_kwargs(retrieved_at=plus(90))))
    a, b = CapturedObservationSet((obs(), later)), CapturedObservationSet((later, obs()))
    assert a.canonical_sha256() == b.canonical_sha256()


def test_observation_provenance_refs_must_be_unique_hashes() -> None:
    from backend.tests.learning_support import HASH_A
    assert obs(evidence_sha256s=(HASH_A,)).evidence_sha256s == (HASH_A,)
    with pytest.raises(ValueError):
        obs(evidence_sha256s=(HASH_A, HASH_A))
    with pytest.raises(ValueError):
        obs(evidence_sha256s=("zz",))
