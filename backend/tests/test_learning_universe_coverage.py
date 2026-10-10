"""Phase 28B-0: universe coverage. SOURCE_CLAIMED_COMPLETE needs explicit upstream evidence; a successful, parsed, nonempty list is only OBSERVED_LIST."""

from __future__ import annotations

from datetime import date

import pytest

from backend.engine.learning.temporal_provenance import TemporalProvenance
from backend.engine.learning.universe_coverage import (
    CompletenessAttestation,
    CoverageClaim,
    LearningSubjectRef,
    SubjectIdentityStatus,
    UniverseSnapshotEvidence,
)
from backend.tests.learning_support import HASH_A, HASH_B, UUID_1, UUID_2, provenance_kwargs


def prov() -> TemporalProvenance:
    return TemporalProvenance(**provenance_kwargs())


def members():
    return (LearningSubjectRef(fund_code="AAA"), LearningSubjectRef(fund_code="BBB", canonical_instrument_id=UUID_1))


def snap(**over) -> UniverseSnapshotEvidence:
    base = dict(universe_key="tefas.try.discovered", source_id="tefas.fund-list", coverage=CoverageClaim.OBSERVED_LIST,
                members=members(), provenance=prov())
    base.update(over)
    return UniverseSnapshotEvidence(**base)


def attest(**over) -> CompletenessAttestation:
    base = dict(evidence_source_id="tefas.fund-list", evidence_content_sha256=HASH_A, claim_reference="page section stating the list is exhaustive",
                universe_key="tefas.try.discovered", observed_list_source_id="tefas.fund-list")
    base.update(over)
    return CompletenessAttestation(**base)


ATTEST = attest()


def test_coverage_vocabulary_is_exactly_four_values() -> None:
    assert {c.name for c in CoverageClaim} == {"CURATED_PILOT", "OBSERVED_LIST", "SOURCE_CLAIMED_COMPLETE", "UNKNOWN"}


def test_nonempty_observed_list_is_not_complete() -> None:
    s = snap()
    assert s.coverage is CoverageClaim.OBSERVED_LIST and s.completeness_attestation is None


def test_complete_claim_without_attestation_rejected() -> None:
    with pytest.raises(ValueError):
        snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE)


def test_attestation_on_non_complete_claim_rejected() -> None:
    for cov in (CoverageClaim.OBSERVED_LIST, CoverageClaim.UNKNOWN):
        with pytest.raises(ValueError):
            snap(coverage=cov, completeness_attestation=ATTEST)


def test_complete_claim_with_explicit_attestation_accepted() -> None:
    s = snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE, completeness_attestation=ATTEST)
    assert s.coverage is CoverageClaim.SOURCE_CLAIMED_COMPLETE


def test_complete_claim_cannot_be_empty_list() -> None:
    with pytest.raises(ValueError):
        snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE, completeness_attestation=ATTEST, members=())


def test_attestation_must_reference_a_stored_source_hash() -> None:
    with pytest.raises(ValueError):
        attest(evidence_content_sha256="nope")
    with pytest.raises((TypeError, ValueError)):
        attest(claim_reference="")


def test_attestation_identifies_its_claim_target() -> None:
    a = attest()
    assert (a.universe_key, a.observed_list_source_id, a.evidence_source_id) == ("tefas.try.discovered", "tefas.fund-list", "tefas.fund-list")
    for bad in ("Bad Key", None, 3):
        with pytest.raises((TypeError, ValueError)):
            attest(universe_key=bad)
        with pytest.raises((TypeError, ValueError)):
            attest(observed_list_source_id=bad)


def test_attestation_for_a_different_universe_is_rejected() -> None:
    with pytest.raises(ValueError):
        snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE, completeness_attestation=attest(universe_key="tefas.try.other"))


def test_attestation_for_a_different_observed_list_source_is_rejected() -> None:
    with pytest.raises(ValueError):
        snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE, completeness_attestation=attest(observed_list_source_id="tefas.other-list"))


def test_independent_evidence_document_may_attest_a_different_observed_list_source() -> None:
    s = snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE, completeness_attestation=attest(evidence_source_id="spk.official-fund-register", evidence_content_sha256=HASH_B))
    assert s.completeness_attestation.evidence_source_id != s.source_id


def test_attestation_is_a_caller_supplied_claim_not_verified_evidence() -> None:
    assert "does not independently prove" in (CompletenessAttestation.__doc__ or "")
    assert snap(coverage=CoverageClaim.SOURCE_CLAIMED_COMPLETE, completeness_attestation=ATTEST).completeness_attestation_verified is False


def test_curated_pilot_requires_preregistered_selection_rule_and_members() -> None:
    with pytest.raises(ValueError):
        snap(coverage=CoverageClaim.CURATED_PILOT)
    with pytest.raises(ValueError):
        snap(coverage=CoverageClaim.CURATED_PILOT, selection_rule_ref="pilot.rule.v1", members=())
    assert snap(coverage=CoverageClaim.CURATED_PILOT, selection_rule_ref="pilot.rule.v1").selection_rule_ref == "pilot.rule.v1"
    with pytest.raises(ValueError):
        snap(selection_rule_ref="pilot.rule.v1")          # selection rule only belongs to a pilot


def test_observed_factory_can_never_claim_completeness() -> None:
    s = UniverseSnapshotEvidence.observed(universe_key="tefas.try.discovered", source_id="tefas.fund-list", members=members(), provenance=prov())
    assert s.coverage is CoverageClaim.OBSERVED_LIST
    assert not hasattr(UniverseSnapshotEvidence, "from_http_response")


def test_duplicate_members_rejected() -> None:
    with pytest.raises(ValueError):
        snap(members=(LearningSubjectRef(fund_code="AAA"), LearningSubjectRef(fund_code="AAA")))


def test_fund_code_uuid_ambiguity_rejected() -> None:
    with pytest.raises(ValueError):   # one UUID, two codes
        snap(members=(LearningSubjectRef("AAA", UUID_1), LearningSubjectRef("BBB", UUID_1)))
    with pytest.raises(ValueError):   # one code, two UUIDs
        snap(members=(LearningSubjectRef("AAA", UUID_1), LearningSubjectRef("AAA", UUID_2)))
    with pytest.raises(ValueError):   # same code both unbound and bound
        snap(members=(LearningSubjectRef("AAA"), LearningSubjectRef("AAA", UUID_1)))


def test_subject_identity_status_and_types() -> None:
    assert LearningSubjectRef("AAA").identity_status is SubjectIdentityStatus.FUND_CODE_ONLY
    assert LearningSubjectRef("AAA", UUID_1).identity_status is SubjectIdentityStatus.CANONICAL_BOUND
    for bad in ("aaa", "A A", "", None, 5):
        with pytest.raises((TypeError, ValueError)):
            LearningSubjectRef(fund_code=bad)
    with pytest.raises(TypeError):
        LearningSubjectRef("AAA", "00000000-0000-4000-8000-000000000001")


def test_snapshot_wrong_types_rejected() -> None:
    with pytest.raises(TypeError):
        snap(coverage="observed_list")
    with pytest.raises(TypeError):
        snap(members=[LearningSubjectRef("AAA")])
    with pytest.raises(TypeError):
        snap(members=("AAA",))
    with pytest.raises(TypeError):
        snap(provenance=None)
    with pytest.raises(ValueError):
        snap(universe_key="Bad Key")


def test_snapshot_is_deterministic_and_member_order_is_canonical() -> None:
    a = snap(members=tuple(reversed(members())))
    b = snap()
    assert a.canonical_sha256() == b.canonical_sha256()
    assert a.to_canonical_dict()["members"][0]["fund_code"] == "AAA"
    assert a.economic_scope_note and "not a candidate" in a.economic_scope_note
