"""
Phase 28B-1A: pure evidence-binding contract and verifier. A hash alone never yields VERIFIED: the retained record must exist, match source identity, hold its payload, re-hash to
the declared hash and carry the declared retrieval instant. Content integrity is NOT licensing, completeness, document-assertion or publication-time verification.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from backend.engine.learning.adapters import source_authority_from_raw_snapshot, verify_evidence_binding
from backend.engine.learning.evidence_binding import (
    EvidenceBindingDeclaration,
    EvidenceBindingError,
    VerificationLevel,
    VerifiedEvidenceBinding,
)
from backend.engine.learning.source_authority import AvailabilityStatus, LicensingStatus, SourceAuthorityClass
from backend.engine.learning.temporal_provenance import PublicationBasis, TemporalProvenance
from backend.engine.private.storage_models import RawProviderSnapshotRecord, compute_payload_hash
from backend.tests.learning_support import HASH_A, HASH_B, UUID_2, plus

PAYLOAD = {"funds": [{"code": "AAA", "price": "1.5"}]}


def raw(payload=None, **over) -> RawProviderSnapshotRecord:
    return RawProviderSnapshotRecord.create(
        provider=over.pop("provider", "TEFAS"), endpoint=over.pop("endpoint", "/api/funds/fonFiyatBilgiGetir"), request_params={"fonKodu": "AAA"},
        raw_payload=PAYLOAD if payload is None else payload, retrieved_at=over.pop("retrieved_at", plus(5)), **over,
    )


def declaration(record: RawProviderSnapshotRecord, *, economic_date=date(2026, 10, 9), **over) -> EvidenceBindingDeclaration:
    src = source_authority_from_raw_snapshot(
        record, source_id="tefas.fund-price", source_reference="https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir",
        authority_class=SourceAuthorityClass.OFFICIAL_PUBLIC_PROVIDER_SURFACE, known_limitations=("undocumented endpoint",),
        availability=AvailabilityStatus.PUBLICLY_AVAILABLE, licensing=LicensingStatus.UNRESOLVED,
    )
    base = dict(
        source_authority=src, provenance=TemporalProvenance(economic_date=economic_date, retrieved_at=record.retrieved_at, capture_attempted_at=plus(0)),
        expected_provider="TEFAS", expected_endpoint="/api/funds/fonFiyatBilgiGetir", revision=1,
    )
    base.update(over)
    return EvidenceBindingDeclaration(**base)


# --- declaration shape ---------------------------------------------------------------------------------------------------------------
def test_declaration_requires_a_raw_snapshot_reference() -> None:
    r = raw()
    d = declaration(r)
    with pytest.raises(ValueError):
        replace(d, source_authority=replace(d.source_authority, raw_snapshot_id=None))


def test_declaration_retrieval_instants_must_agree() -> None:
    r = raw()
    with pytest.raises(ValueError):
        declaration(r, provenance=TemporalProvenance(economic_date=date(2026, 10, 9), retrieved_at=plus(6), capture_attempted_at=plus(0)))


@pytest.mark.parametrize("field,bad", [("revision", 0), ("revision", True), ("revision", "1"), ("expected_provider", ""), ("expected_endpoint", None), ("source_authority", None), ("provenance", None)])
def test_declaration_type_and_range_strictness(field, bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        declaration(raw(), **{field: bad})


def test_declaration_is_frozen_and_deterministic() -> None:
    r = raw()
    a, b = declaration(r), declaration(r)
    assert a == b and a.canonical_sha256() == b.canonical_sha256()
    with pytest.raises(Exception):
        a.revision = 2  # type: ignore[misc]


# --- verification: success ----------------------------------------------------------------------------------------------------------
def test_valid_binding_verifies_exactly_the_integrity_levels_and_nothing_more() -> None:
    r = raw()
    v = verify_evidence_binding(declaration(r), r)
    lv = v.levels
    assert (lv.stored_content_integrity, lv.record_reference_existence, lv.source_identity_binding) == (VerificationLevel.VERIFIED,) * 3
    for other in (lv.document_assertion_verification, lv.licensing_access_verification, lv.economic_completeness_verification, lv.publication_time_authority):
        assert other is VerificationLevel.UNVERIFIED
    assert v.capture_authorized is False


def test_retrieval_time_comes_from_the_retained_record_not_the_economic_date() -> None:
    r = raw(retrieved_at=plus(5))
    v = verify_evidence_binding(declaration(r, economic_date=date(2019, 3, 4)), r)
    assert v.declaration.provenance.economic_date == date(2019, 3, 4)
    assert v.snapshot_retrieved_at == r.retrieved_at and v.snapshot_retrieved_at.date() != date(2019, 3, 4)


def test_independently_reconstructed_identical_payload_verifies() -> None:
    r1, r2 = raw(), raw(payload=dict(PAYLOAD))
    r2.id = r1.id
    assert verify_evidence_binding(declaration(r1), r2).snapshot_retrieved_at == r1.retrieved_at


# --- verification: fail closed -------------------------------------------------------------------------------------------------------
def test_missing_record_is_rejected() -> None:
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(declaration(raw()), None)  # type: ignore[arg-type]


def test_record_of_the_wrong_type_or_subclass_is_rejected() -> None:
    class Sub(RawProviderSnapshotRecord):
        pass
    r = raw()
    for bad in ({"id": r.id}, "x", Sub(**r.__dict__)):
        with pytest.raises((EvidenceBindingError, TypeError)):
            verify_evidence_binding(declaration(r), bad)  # type: ignore[arg-type]


def test_conflicting_snapshot_identity_is_rejected() -> None:
    r = raw()
    other = raw()
    assert other.id != r.id
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(declaration(r), other)


def test_wrong_source_identity_is_rejected() -> None:
    r = raw()
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(declaration(r, expected_provider="KAP"), r)
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(declaration(r, expected_endpoint="/other"), r)


def test_unavailable_retained_payload_is_rejected() -> None:
    r = raw()
    d = declaration(r)
    r.raw_payload = None
    r.storage_ref = "s3://elsewhere/blob"          # an external reference is not a retained payload this verifier can re-hash
    with pytest.raises(EvidenceBindingError, match="no payload"):
        verify_evidence_binding(d, r)


def test_valid_looking_but_forged_hash_never_verifies() -> None:
    r = raw()
    forged = "f" * 64
    r.payload_hash = forged                           # the retained record itself carries a forged hash
    d = declaration(raw(), expected_provider="TEFAS")  # declaration for a different record is irrelevant; build a matching one:
    src = replace(d.source_authority, raw_snapshot_id=r.id, content_sha256=forged)
    d = replace(d, source_authority=src, provenance=replace(d.provenance, retrieved_at=r.retrieved_at))
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(d, r)


def test_declared_hash_that_is_not_the_records_hash_is_rejected() -> None:
    r = raw()
    d = declaration(r)
    forged = replace(d, source_authority=replace(d.source_authority, content_sha256=HASH_A))
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(forged, r)


def test_mutated_payload_is_rejected() -> None:
    r = raw()
    d = declaration(r)
    r.raw_payload = {"funds": []}
    with pytest.raises(EvidenceBindingError):
        verify_evidence_binding(d, r)


def test_conflicting_retrieval_instant_is_rejected_including_economic_date_substitution() -> None:
    r = raw()
    d = declaration(r)
    for fake in (plus(999), datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)):          # second one: the economic date used as if it were a retrieval time
        src = replace(d.source_authority, retrieved_at=fake)
        bad = replace(d, source_authority=src, provenance=replace(d.provenance, retrieved_at=fake, capture_attempted_at=min(fake, plus(0))))
        with pytest.raises(EvidenceBindingError):
            verify_evidence_binding(bad, r)


# --- no self-made VERIFIED -----------------------------------------------------------------------------------------------------------
def test_a_verified_binding_cannot_be_constructed_directly() -> None:
    r = raw()
    v = verify_evidence_binding(declaration(r), r)
    with pytest.raises(TypeError):
        VerifiedEvidenceBinding(declaration=v.declaration, snapshot_retrieved_at=v.snapshot_retrieved_at, levels=v.levels, _issuer=object())
    with pytest.raises(TypeError):
        VerifiedEvidenceBinding(declaration=v.declaration, snapshot_retrieved_at=v.snapshot_retrieved_at, levels=v.levels)  # type: ignore[call-arg]


def test_only_three_integrity_levels_can_ever_be_verified() -> None:
    from backend.engine.learning.evidence_binding import BindingVerificationLevels
    for name in ("document_assertion_verification", "licensing_access_verification", "economic_completeness_verification", "publication_time_authority"):
        with pytest.raises(ValueError):
            BindingVerificationLevels(stored_content_integrity=VerificationLevel.VERIFIED, record_reference_existence=VerificationLevel.VERIFIED,
                                      source_identity_binding=VerificationLevel.VERIFIED, **{name: VerificationLevel.VERIFIED})
    with pytest.raises(ValueError):                    # integrity levels cannot be partially verified either: all three come from one successful verification
        BindingVerificationLevels(stored_content_integrity=VerificationLevel.VERIFIED, record_reference_existence=VerificationLevel.UNVERIFIED,
                                  source_identity_binding=VerificationLevel.VERIFIED)


def test_declared_licensing_and_publication_claims_stay_unverified_after_content_verification() -> None:
    r = raw()
    d = declaration(r)
    d = replace(d, source_authority=replace(d.source_authority, licensing=LicensingStatus.PERMISSION_DECLARED, licensing_evidence_sha256s=(HASH_B,)),
                provenance=replace(d.provenance, publication_basis=PublicationBasis.SOURCE_DOCUMENT_STATED, publication_time=plus(1), publication_evidence_sha256=HASH_A))
    v = verify_evidence_binding(d, r)
    assert v.levels.licensing_access_verification is VerificationLevel.UNVERIFIED and v.levels.publication_time_authority is VerificationLevel.UNVERIFIED
    assert v.capture_authorized is False


# --- RPC payload ------------------------------------------------------------------------------------------------------------------------
def test_rpc_payload_is_deterministic_and_carries_only_declared_content() -> None:
    r = raw()
    v = verify_evidence_binding(declaration(r, economic_date=date(2019, 3, 4)), r)
    p = v.to_rpc_dict()
    assert p == verify_evidence_binding(declaration(r, economic_date=date(2019, 3, 4)), r).to_rpc_dict()
    assert p["raw_snapshot_id"] == str(r.id) and p["economic_date"] == "2019-03-04" and p["snapshot_retrieved_at"] == "2026-10-12T09:00:05+00:00"
    assert p["content_sha256"] == compute_payload_hash(PAYLOAD) and p["revision"] == 1
    forbidden = {"recorded_at", "capture_authorized", "stored_content_integrity", "licensing_access_verification", "prev_revision"}
    assert not forbidden & set(p)
