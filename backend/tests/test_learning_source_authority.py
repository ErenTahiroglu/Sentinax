"""Phase 28B-0: source authority. A SHA-256 proves stored-content identity only; unknown licensing/availability stays unresolved."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from backend.engine.learning.source_authority import (
    AvailabilityStatus,
    LicensingStatus,
    SourceAuthorityClass,
    SourceAuthorityRecord,
)
from backend.tests.learning_support import HASH_A, HASH_B, UUID_1, plus


def record(**over) -> SourceAuthorityRecord:
    base = dict(
        source_id="spk.iii-52-1", source_reference="https://mevzuat.spk.gov.tr/api/Mevzuat/File/167", document_version="III-52.1.e",
        retrieved_at=plus(5), content_sha256=HASH_A, authority_class=SourceAuthorityClass.PRIMARY_OFFICIAL_DOCUMENT,
        known_limitations=("consolidated text lists amendments only up to III-52.1.e",),
        availability=AvailabilityStatus.PUBLICLY_AVAILABLE, licensing=LicensingStatus.UNRESOLVED,
    )
    base.update(over)
    return SourceAuthorityRecord(**base)


def test_valid_record_defaults_do_not_resolve_capture_permission() -> None:
    r = record()
    assert r.capture_permission_resolved is False
    assert r.raw_snapshot_id is None


def test_unknown_licensing_or_availability_is_never_resolved() -> None:
    assert record(licensing=LicensingStatus.UNRESOLVED, availability=AvailabilityStatus.PUBLICLY_AVAILABLE).capture_permission_resolved is False
    assert record(licensing=LicensingStatus.PERMISSION_DOCUMENTED, availability=AvailabilityStatus.UNKNOWN).capture_permission_resolved is False
    assert record(licensing=LicensingStatus.PERMISSION_DOCUMENTED, availability=AvailabilityStatus.PUBLICLY_AVAILABLE).capture_permission_resolved is True


@pytest.mark.parametrize("field,bad", [
    ("source_id", "SPK"), ("source_id", "spk iii"), ("source_id", ""), ("source_id", None),
    ("content_sha256", "xyz"), ("content_sha256", "A" * 64),
    ("source_reference", ""), ("source_reference", " x"), ("source_reference", "a\nb"),
    ("document_version", ""), ("document_version", 3),
    ("authority_class", "primary"), ("availability", "unknown"), ("licensing", None),
    ("known_limitations", ["a"]), ("known_limitations", (1,)), ("known_limitations", ("",)),
    ("retrieved_at", datetime(2026, 1, 1)), ("raw_snapshot_id", "00000000-0000-4000-8000-000000000001"),
])
def test_invalid_fields_rejected(field, bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        record(**{field: bad})


def test_duplicate_limitations_rejected() -> None:
    with pytest.raises(ValueError):
        record(known_limitations=("a", "a"))


def test_raw_snapshot_link_must_be_exact_uuid() -> None:
    assert record(raw_snapshot_id=UUID_1).raw_snapshot_id == UUID_1


def test_record_is_frozen_hashable_and_deterministic() -> None:
    a, b = record(), record()
    assert a == b and hash(a) == hash(b) and a.canonical_sha256() == b.canonical_sha256()
    assert a.canonical_sha256() != replace(a, content_sha256=HASH_B).canonical_sha256()
    with pytest.raises(Exception):
        a.source_id = "x"  # type: ignore[misc]


def test_hash_semantics_are_documented_on_the_type() -> None:
    doc = SourceAuthorityRecord.__doc__ or ""
    assert "stored-content identity" in doc and "not" in doc and "publisher authenticity" in doc
