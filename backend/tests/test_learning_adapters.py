"""Phase 28B-0: reuse of existing storage/TEFAS contracts without altering them. Publication time is never imported or inferred from a modern retrieval."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backend.engine.learning.adapters import captured_observation_from_tefas_price, source_authority_from_raw_snapshot
from backend.engine.learning.observation_status import ObservationOrigin, ObservationStatus
from backend.engine.learning.source_authority import AvailabilityStatus, LicensingStatus, SourceAuthorityClass
from backend.engine.learning.temporal_provenance import PublicationBasis
from backend.engine.private.market_data.tefas_models import TefasFundPriceObservation, TefasObservationStatus
from backend.engine.private.storage_models import RawProviderSnapshotRecord
from backend.tests.learning_support import UUID_1, plus


def raw(**over) -> RawProviderSnapshotRecord:
    return RawProviderSnapshotRecord.create(provider="TEFAS", endpoint="/api/funds/fonFiyatBilgiGetir", request_params={"fonKodu": "AAA"},
                                            raw_payload={"x": 1}, retrieved_at=over.pop("retrieved_at", plus(5)), **over)


def src_kwargs(**over):
    base = dict(source_id="tefas.fund-price", source_reference="https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir",
                authority_class=SourceAuthorityClass.OFFICIAL_PUBLIC_PROVIDER_SURFACE, known_limitations=("no SLA; undocumented endpoint",),
                availability=AvailabilityStatus.PUBLICLY_AVAILABLE, licensing=LicensingStatus.UNRESOLVED)
    base.update(over)
    return base


def price(**over) -> TefasFundPriceObservation:
    base = dict(provider_symbol="AAA", trade_date=date(2026, 10, 9), unit_price=Decimal("2.5"), instrument_id=UUID_1, retrieved_at=plus(60))
    base.update(over)
    return TefasFundPriceObservation(**base)


def test_raw_snapshot_adapter_reuses_identity_hash_and_retrieval_instant() -> None:
    r = raw()
    s = source_authority_from_raw_snapshot(r, **src_kwargs())
    assert s.content_sha256 == r.payload_hash and s.retrieved_at == r.retrieved_at and s.raw_snapshot_id == r.id
    assert s.capture_authorized is False and s.licensing_verified is False


def test_raw_snapshot_adapter_rejects_non_records_and_subclasses() -> None:
    class Sub(RawProviderSnapshotRecord):
        pass
    for bad in (None, {"payload_hash": "a" * 64}, "x"):
        with pytest.raises(TypeError):
            source_authority_from_raw_snapshot(bad, **src_kwargs())
    sub = Sub(**{**raw().__dict__})
    with pytest.raises(TypeError):
        source_authority_from_raw_snapshot(sub, **src_kwargs())


def test_raw_snapshot_adapter_rejects_malformed_stored_hash() -> None:
    for bad in ("A" * 64, "a" * 63, "zz", ""):
        r = raw()
        r.payload_hash = bad
        with pytest.raises(ValueError):
            source_authority_from_raw_snapshot(r, **src_kwargs())


def test_raw_snapshot_adapter_rejects_stale_but_well_formed_hash() -> None:
    r = raw()
    r.raw_payload = {"x": 2}                       # payload no longer matches the retained hash
    assert len(r.payload_hash) == 64
    with pytest.raises(ValueError):
        source_authority_from_raw_snapshot(r, **src_kwargs())
    r2 = raw()
    r2.payload_hash = "f" * 64                     # well-formed hash that was never this payload's
    with pytest.raises(ValueError):
        source_authority_from_raw_snapshot(r2, **src_kwargs())


def test_raw_snapshot_adapter_accepts_independently_reconstructed_identical_payload() -> None:
    a = raw()
    b = RawProviderSnapshotRecord.create(provider="TEFAS", endpoint="/api/funds/fonFiyatBilgiGetir", request_params={"fonKodu": "AAA"}, raw_payload=dict(reversed(list({"x": 1}.items()))), retrieved_at=plus(5))
    assert source_authority_from_raw_snapshot(a, **src_kwargs()).content_sha256 == source_authority_from_raw_snapshot(b, **src_kwargs()).content_sha256
    c = raw(); c.raw_payload = {"x": 1}            # rebuilt equal payload
    assert source_authority_from_raw_snapshot(c, **src_kwargs()).content_sha256 == a.payload_hash


def test_raw_snapshot_adapter_uses_the_closed_storage_hash_semantics_for_every_payload_kind() -> None:
    from backend.engine.private.storage_models import compute_payload_hash
    for payload in ({"b": 2, "a": [1, 2]}, [1, 2, 3], "text", b"bytes"):
        r = RawProviderSnapshotRecord.create(provider="TEFAS", endpoint="/e", request_params={}, raw_payload=payload, retrieved_at=plus(5))
        s = source_authority_from_raw_snapshot(r, **src_kwargs())
        assert s.content_sha256 == compute_payload_hash(payload)


def test_raw_snapshot_adapter_is_deterministic() -> None:
    r = raw()
    assert source_authority_from_raw_snapshot(r, **src_kwargs()).canonical_sha256() == source_authority_from_raw_snapshot(r, **src_kwargs()).canonical_sha256()


def test_adapter_module_defines_no_second_hash_algorithm() -> None:
    import inspect
    from backend.engine.learning import adapters
    src = inspect.getsource(adapters)
    assert "hashlib" not in src and "compute_payload_hash" in src


def test_tefas_adapter_never_imports_publication_time() -> None:
    o = price(published_at=datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc))
    c = captured_observation_from_tefas_price(o, capture_attempted_at=plus(55), origin=ObservationOrigin.FORWARD_CAPTURE)
    assert c.provenance.publication_basis is PublicationBasis.UNKNOWN and c.provenance.publication_time is None
    assert c.provenance.retrieved_at == plus(60) and c.provenance.economic_date == date(2026, 10, 9)
    assert c.status is ObservationStatus.OBSERVED_IN_RESPONSE and c.observed_unit_price == Decimal("2.5")
    assert c.subject.fund_code == "AAA" and c.subject.canonical_instrument_id == UUID_1


def test_tefas_adapter_requires_explicit_origin_and_retrieval_time() -> None:
    with pytest.raises(TypeError):
        captured_observation_from_tefas_price(price(), capture_attempted_at=plus(55), origin="forward")
    with pytest.raises(ValueError):
        captured_observation_from_tefas_price(price(retrieved_at=None), capture_attempted_at=plus(55), origin=ObservationOrigin.UNKNOWN)


def test_tefas_adapter_refuses_non_valid_or_priceless_observations() -> None:
    with pytest.raises(ValueError):
        captured_observation_from_tefas_price(price(status=TefasObservationStatus.INVALID_OBSERVATION), capture_attempted_at=plus(55), origin=ObservationOrigin.UNKNOWN)
    with pytest.raises(ValueError):
        captured_observation_from_tefas_price(price(unit_price=None), capture_attempted_at=plus(55), origin=ObservationOrigin.UNKNOWN)


def test_old_trade_date_in_modern_retrieval_is_not_relabelled() -> None:
    c = captured_observation_from_tefas_price(price(trade_date=date(2021, 3, 4)), capture_attempted_at=plus(55), origin=ObservationOrigin.RETROSPECTIVE_IN_RESPONSE)
    assert c.provenance.economic_date == date(2021, 3, 4) and c.provenance.system_known_at_utc == plus(60)


def test_adapter_passes_licensing_declaration_only_with_evidence_and_never_authorizes_capture() -> None:
    from backend.tests.learning_support import HASH_B
    with pytest.raises(ValueError):
        source_authority_from_raw_snapshot(raw(), **src_kwargs(licensing=LicensingStatus.PERMISSION_DECLARED))
    s = source_authority_from_raw_snapshot(raw(), **src_kwargs(licensing=LicensingStatus.PERMISSION_DECLARED, licensing_evidence_sha256s=(HASH_B,)))
    assert s.licensing_evidence_sha256s == (HASH_B,) and s.capture_authorized is False


def test_direct_constructor_cannot_verify_payload_integrity_so_adapter_is_the_checked_path() -> None:
    from backend.engine.learning.source_authority import SourceAuthorityRecord
    from backend.tests.learning_support import HASH_C
    # No payload is available to a direct constructor: it validates format only. Only the adapter re-hashes the retained payload.
    direct = SourceAuthorityRecord(source_id="tefas.fund-price", source_reference="r", document_version=None, retrieved_at=plus(5), content_sha256=HASH_C,
                                   authority_class=SourceAuthorityClass.UNCLASSIFIED, known_limitations=("hash is not verified against a payload",),
                                   availability=AvailabilityStatus.UNKNOWN, licensing=LicensingStatus.UNRESOLVED)
    assert direct.content_sha256 == HASH_C
