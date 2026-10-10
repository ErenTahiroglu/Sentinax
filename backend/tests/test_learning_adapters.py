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
    assert s.capture_permission_resolved is False


def test_raw_snapshot_adapter_rejects_non_records_and_subclasses() -> None:
    class Sub(RawProviderSnapshotRecord):
        pass
    for bad in (None, {"payload_hash": "a" * 64}, "x"):
        with pytest.raises(TypeError):
            source_authority_from_raw_snapshot(bad, **src_kwargs())
    sub = Sub(**{**raw().__dict__})
    with pytest.raises(TypeError):
        source_authority_from_raw_snapshot(sub, **src_kwargs())


def test_raw_snapshot_adapter_rejects_inconsistent_stored_hash() -> None:
    r = raw()
    r.payload_hash = "A" * 64
    with pytest.raises(ValueError):
        source_authority_from_raw_snapshot(r, **src_kwargs())


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
