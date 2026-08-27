"""
backend/tests/test_market_data_resolver.py
==========================================
Comprehensive test suite for Point-in-Time Market Data Observation Resolver (Phase 9B.2 + 9B.2.5).

Verifies:
    - Zero network calls (pytest-socket active).
    - CURRENT_REPORTED selects latest valid successful (HTTP 200) snapshot for target date.
    - SYSTEM_AS_OF enforces strict lookahead protection (retrieved_at <= as_of).
    - Future snapshot conflicts (at 11:00) do NOT contaminate historical SYSTEM_AS_OF (at 10:30).
    - Future observation corruptions do NOT poison historical SYSTEM_AS_OF resolution.
    - Transport failure (HTTP 500) does NOT supersede or erase previous valid full bulletin in CURRENT_REPORTED.
    - SOURCE_AS_OF returns UNAVAILABLE_SOURCE_AS_OF.
    - Full snapshot supersession: no old-snapshot resurrection when target is absent/invalid in latest.
    - Snapshot conflict vs logical duplicate deduplication.
    - Naive datetimes fail closed (INVALID_TEMPORAL_LINEAGE).
    - Canonical instrument_id authority for BIST (ALTIN.S1 preserved as COMMODITY_CERTIFICATE).
    - Fully dimensioned semantic query key for Precious Metals (metal, currency, unit, price type, purity, settlement).
    - Non-finite Decimals rejected.
    - Observation temporal lineage validation (snapshot_id, payload_hash, date mismatch).
    - Same close price with differing attributes (volume/high/etc.) produces OBSERVATION_CONFLICT.
    - Exact logical duplicate observations deduplicate cleanly with input order independence.
    - Deterministic resolution_key is strictly UUID-independent across identical economic datasets.
    - Evaluation snapshot IDs audit includes only eligible snapshots at that temporal boundary, sorted deterministically.
    - Confidence propagation and stale-discovery degradation.
    - Serialization without float types.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.bist.models import (
    BISTBulletinSnapshot,
    BISTEODObservation,
    BISTObservationStatus,
)
from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    InstrumentType,
    SourceTier,
)
from backend.engine.private.market_data import (
    BISTInstrumentQueryKey,
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    PointInTimeMarketDataResolver,
    PreciousMetalSemanticKey,
)
from backend.engine.private.precious_metals.constants import (
    PreciousMetalMarket,
    PreciousMetalPriceType,
    PreciousMetalType,
    PreciousMetalUnit,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
    PreciousMetalSnapshot,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Fixtures for BIST EOD & KMTP Snapshots
# ─────────────────────────────────────────────────────────────────────────────

def create_mock_bist_obs(
    symbol: str,
    instrument_id: Optional[UUID],
    trade_date: date,
    close: Optional[Decimal],
    open_val: Optional[Decimal] = None,
    high_val: Optional[Decimal] = None,
    low_val: Optional[Decimal] = None,
    volume: Optional[Decimal] = None,
    turnover: Optional[Decimal] = None,
    trade_count: Optional[int] = None,
    snapshot_id: Optional[UUID] = None,
    snapshot_hash: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
    status: BISTObservationStatus = BISTObservationStatus.VALID,
    instrument_type: InstrumentType = InstrumentType.BIST_STOCK,
    confidence: DataConfidenceLevel = DataConfidenceLevel.HIGH,
    observation_id: Optional[UUID] = None,
) -> BISTEODObservation:
    return BISTEODObservation(
        id=observation_id or uuid4(),
        symbol=symbol,
        trade_date=trade_date,
        close=close,
        open=open_val if open_val is not None else close,
        high=high_val if high_val is not None else close,
        low=low_val if low_val is not None else close,
        volume=volume,
        turnover=turnover,
        trade_count=trade_count,
        currency=Currency.TRY,
        instrument_id=instrument_id,
        asset_class=AssetClass.EQUITY if instrument_type == InstrumentType.BIST_STOCK else AssetClass.COMMODITY,
        instrument_type=instrument_type,
        status=status,
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        retrieved_at=retrieved_at,
        confidence_level=confidence,
        source_tier=SourceTier.TIER_2_EXCHANGE,
    )


def create_mock_bist_snapshot(
    trade_date: date,
    retrieved_at: datetime,
    payload_hash: str,
    observations: Optional[List[BISTEODObservation]] = None,
    is_stale_discovery: bool = False,
    snapshot_id: Optional[UUID] = None,
    http_status: int = 200,
) -> BISTBulletinSnapshot:
    snap_id = snapshot_id or uuid4()
    obs_list = observations or []
    for obs in obs_list:
        if obs.snapshot_id is None:
            obs.snapshot_id = snap_id
        if obs.snapshot_hash is None:
            obs.snapshot_hash = payload_hash
        if obs.retrieved_at is None:
            obs.retrieved_at = retrieved_at
        if obs.trade_date is None:
            obs.trade_date = trade_date

    return BISTBulletinSnapshot(
        id=snap_id,
        trade_date=trade_date,
        retrieved_at=retrieved_at,
        http_status=http_status,
        payload_hash=payload_hash,
        content_type="text/csv",
        is_stale_discovery=is_stale_discovery,
        observations=obs_list,
    )


def create_mock_pm_obs(
    metal: PreciousMetalType,
    effective_date: date,
    price: Optional[Decimal],
    price_currency: Currency,
    quantity_unit: PreciousMetalUnit,
    price_type: PreciousMetalPriceType,
    snapshot_id: Optional[UUID] = None,
    payload_hash: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
    fineness_per_mille: Optional[Decimal] = None,
    settlement_term: Optional[str] = None,
    raw_value_date_text: Optional[str] = None,
    status: PreciousMetalObservationStatus = PreciousMetalObservationStatus.VALID,
    confidence: DataConfidenceLevel = DataConfidenceLevel.HIGH,
    observation_id: Optional[UUID] = None,
) -> PreciousMetalMarketObservation:
    return PreciousMetalMarketObservation(
        id=observation_id or uuid4(),
        metal=metal,
        market=PreciousMetalMarket.BIST_KMTP,
        effective_date=effective_date,
        price=price,
        price_currency=price_currency,
        quantity_unit=quantity_unit,
        price_type=price_type,
        fineness_per_mille=fineness_per_mille,
        settlement_term=settlement_term,
        raw_value_date_text=raw_value_date_text,
        snapshot_id=snapshot_id,
        payload_hash=payload_hash,
        retrieved_at=retrieved_at,
        status=status,
        confidence=confidence,
    )


def create_mock_pm_snapshot(
    trade_date: date,
    retrieved_at: datetime,
    payload_hash: str,
    observations: Optional[List[PreciousMetalMarketObservation]] = None,
    is_stale_discovery: bool = False,
    snapshot_id: Optional[UUID] = None,
    http_status: int = 200,
) -> PreciousMetalSnapshot:
    snap_id = snapshot_id or uuid4()
    obs_list = observations or []
    for obs in obs_list:
        if obs.snapshot_id is None:
            obs.snapshot_id = snap_id
        if obs.payload_hash is None:
            obs.payload_hash = payload_hash
        if obs.retrieved_at is None:
            obs.retrieved_at = retrieved_at
        if obs.effective_date is None:
            obs.effective_date = trade_date

    return PreciousMetalSnapshot(
        id=snap_id,
        trade_date=trade_date,
        retrieved_at=retrieved_at,
        http_status=http_status,
        payload_hash=payload_hash,
        content_type="application/zip",
        is_stale_discovery=is_stale_discovery,
        observations=obs_list,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Suites
# ─────────────────────────────────────────────────────────────────────────────

class TestBISTMarketDataResolver:

    def test_01_bist_current_reported_selects_latest_snapshot(self):
        """Scenario 1: CURRENT_REPORTED selects from the latest valid snapshot for target date."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_a])

        obs_b = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("101.00"))
        snap_b = create_mock_bist_snapshot(t_date, t_11, "hash_b", [obs_b])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_a, snap_b], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation is not None
        assert res.selected_observation.close == Decimal("101.00")
        assert res.snapshot_hash == "hash_b"

    def test_02_bist_system_as_of_lookahead_protection(self):
        """Scenario 2: SYSTEM_AS_OF isolates knowledge strictly before/after correction time."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_a])

        obs_b = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("101.00"))
        snap_b = create_mock_bist_snapshot(t_date, t_11, "hash_b", [obs_b])

        # As of 10:30 UTC: only snap_a is eligible
        res_early = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a, snap_b], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc)
        )
        assert res_early.status == MarketDataResolutionStatus.SELECTED
        assert res_early.selected_observation.close == Decimal("100.00")
        assert res_early.snapshot_hash == "hash_a"

        # As of 11:30 UTC: snap_b is authoritative
        res_late = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a, snap_b], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 11, 30, tzinfo=timezone.utc)
        )
        assert res_late.status == MarketDataResolutionStatus.SELECTED
        assert res_late.selected_observation.close == Decimal("101.00")
        assert res_late.snapshot_hash == "hash_b"

    def test_03_bist_no_snapshot_as_of(self):
        """Scenario 3: SYSTEM_AS_OF before any snapshot retrieval returns NO_SNAPSHOT_AS_OF."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_a])

        as_of_early = datetime(2024, 10, 1, 9, 59, tzinfo=timezone.utc)
        res = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_early
        )
        assert res.status == MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF
        assert res.selected_observation is None
        assert res.evaluation_snapshot_ids == []

    def test_04_bist_no_old_snapshot_resurrection(self):
        """Scenario 4: When latest full snapshot omits a previously valid instrument, do NOT resurrect."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        other_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        # Snapshot A has THYAO
        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("300.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_a])

        # Corrected Snapshot B omits THYAO, contains only ASELS
        obs_b = create_mock_bist_obs("ASELS", other_id, t_date, Decimal("60.00"))
        snap_b = create_mock_bist_snapshot(t_date, t_11, "hash_b", [obs_b])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_a, snap_b], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
        assert res.selected_observation is None

    def test_05_bist_invalid_latest_observation_no_resurrection(self):
        """Scenario 5: Latest snapshot has invalid observation -> NO_ELIGIBLE_OBSERVATION (no fallback to A)."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("300.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_a])

        # Snapshot B has corrupted/invalid THYAO
        obs_b = create_mock_bist_obs(
            "THYAO", inst_id, t_date, None, status=BISTObservationStatus.INVALID_OBSERVATION
        )
        snap_b = create_mock_bist_snapshot(t_date, t_11, "hash_b", [obs_b])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_a, snap_b], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
        assert res.selected_observation is None

    def test_06_bist_snapshot_conflict(self):
        """Scenario 6: Same retrieved_at with differing payload_hash fails closed as SNAPSHOT_CONFLICT."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)

        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_1", [obs_a])

        obs_b = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("105.00"))
        snap_b = create_mock_bist_snapshot(t_date, t_10, "hash_2", [obs_b])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_a, snap_b], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT

    def test_07_bist_logical_duplicate_snapshot_deduplicates(self):
        """Scenario 7: Duplicate snapshot records with same payload_hash and retrieved_at deduplicate safely."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)

        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a1 = create_mock_bist_snapshot(t_date, t_10, "hash_exact", [obs_a], snapshot_id=uuid4())

        obs_b = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a2 = create_mock_bist_snapshot(t_date, t_10, "hash_exact", [obs_b], snapshot_id=uuid4())

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_a1, snap_a2], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("100.00")

    def test_08_naive_datetime_fails_closed(self):
        """Scenario 8: Naive snapshot retrieved_at or naive as_of timestamp returns INVALID_TEMPORAL_LINEAGE."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date)

        naive_t = datetime(2024, 10, 1, 10, 0)  # Naive!
        obs = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), retrieved_at=naive_t)
        snap = create_mock_bist_snapshot(t_date, naive_t, "hash_a", [obs])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

        # Naive as_of in SYSTEM_AS_OF
        aware_t = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        obs_aware = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_aware = create_mock_bist_snapshot(t_date, aware_t, "hash_a", [obs_aware])

        res_naive_as_of = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_aware], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=naive_t
        )
        assert res_naive_as_of.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    def test_09_source_as_of_unavailable(self):
        """Scenario 9: SOURCE_AS_OF mode returns UNAVAILABLE_SOURCE_AS_OF."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date)

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [], MarketDataResolutionMode.SOURCE_AS_OF)
        assert res.status == MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF

    def test_10_bist_canonical_identity_authority(self):
        """Scenario 10: Observations with UNRESOLVED_IDENTITY or differing instrument_id cannot be selected."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="UNRES_SYM")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        obs = create_mock_bist_obs(
            "UNRES_SYM", None, t_date, Decimal("50.00"), status=BISTObservationStatus.UNRESOLVED_IDENTITY
        )
        snap = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.UNRESOLVED_IDENTITY
        assert res.selected_observation is None

    def test_11_altin_s1_commodity_certificate_resolved(self):
        """Scenario 11: ALTIN.S1 commodity certificate resolves preserving COMMODITY_CERTIFICATE instrument_type."""
        t_date = date(2024, 10, 1)
        altin_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=altin_id, trade_date=t_date, symbol="ALTIN.S1")

        t_18 = datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc)
        obs = create_mock_bist_obs(
            "ALTIN.S1", altin_id, t_date, Decimal("24.50"), instrument_type=InstrumentType.COMMODITY_CERTIFICATE
        )
        snap = create_mock_bist_snapshot(t_date, t_18, "hash_altin", [obs])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation is not None
        assert res.selected_observation.close == Decimal("24.50")
        assert res.selected_observation.instrument_type == InstrumentType.COMMODITY_CERTIFICATE


class TestPreciousMetalsMarketDataResolver:

    def test_12_precious_current_reported_selects_latest(self):
        """Scenario 12: KMTP Gold observation correction resolved via CURRENT_REPORTED."""
        t_date = date(2024, 10, 1)
        sem_key = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD,
            effective_date=t_date,
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0"),
        )

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        obs_a = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7000000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_a = create_mock_pm_snapshot(t_date, t_10, "pm_hash_a", [obs_a])

        obs_b = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7100000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_b = create_mock_pm_snapshot(t_date, t_11, "pm_hash_b", [obs_b])

        res = PointInTimeMarketDataResolver.resolve_precious_metal(sem_key, [snap_a, snap_b], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.price == Decimal("7100000.00")
        assert res.snapshot_hash == "pm_hash_b"

    def test_13_precious_system_as_of(self):
        """Scenario 13: KMTP SYSTEM_AS_OF before and after bulletin correction."""
        t_date = date(2024, 10, 1)
        sem_key = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD,
            effective_date=t_date,
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0"),
        )

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        obs_a = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7000000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_a = create_mock_pm_snapshot(t_date, t_10, "pm_hash_a", [obs_a])

        obs_b = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7100000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_b = create_mock_pm_snapshot(t_date, t_11, "pm_hash_b", [obs_b])

        res_early = PointInTimeMarketDataResolver.resolve_precious_metal(
            sem_key, [snap_a, snap_b], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc)
        )
        assert res_early.status == MarketDataResolutionStatus.SELECTED
        assert res_early.selected_observation.price == Decimal("7000000.00")

        res_late = PointInTimeMarketDataResolver.resolve_precious_metal(
            sem_key, [snap_a, snap_b], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 11, 30, tzinfo=timezone.utc)
        )
        assert res_late.status == MarketDataResolutionStatus.SELECTED
        assert res_late.selected_observation.price == Decimal("7100000.00")

    def test_14_precious_key_dimensions_isolation(self):
        """Scenario 14: TRY/KG vs USD/OZ requests on same metal do not collide."""
        t_date = date(2024, 10, 1)
        t_18 = datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc)

        obs_try = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7140000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        obs_usd = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("4615.96"), Currency.USD, PreciousMetalUnit.TROY_OZ,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap = create_mock_pm_snapshot(t_date, t_18, "pm_hash", [obs_try, obs_usd])

        key_try = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0")
        )
        res_try = PointInTimeMarketDataResolver.resolve_precious_metal(key_try, [snap])
        assert res_try.status == MarketDataResolutionStatus.SELECTED
        assert res_try.selected_observation.price == Decimal("7140000.00")

        key_usd = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.USD,
            quantity_unit=PreciousMetalUnit.TROY_OZ, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0")
        )
        res_usd = PointInTimeMarketDataResolver.resolve_precious_metal(key_usd, [snap])
        assert res_usd.status == MarketDataResolutionStatus.SELECTED
        assert res_usd.selected_observation.price == Decimal("4615.96")

    def test_15_precious_price_type_isolation(self):
        """Scenario 15: REFERENCE vs WEIGHTED_AVERAGE vs CLOSE distinct (no fallback)."""
        t_date = date(2024, 10, 1)
        t_18 = datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc)

        obs_ref = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7135000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.REFERENCE
        )
        snap = create_mock_pm_snapshot(t_date, t_18, "pm_hash", [obs_ref])

        key_wap = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE
        )
        res_wap = PointInTimeMarketDataResolver.resolve_precious_metal(key_wap, [snap])
        assert res_wap.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION

    def test_16_precious_purity_isolation(self):
        """Scenario 16: 995 vs 999 vs None purity keys are strictly isolated."""
        t_date = date(2024, 10, 1)
        t_18 = datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc)

        obs_995 = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7140000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap = create_mock_pm_snapshot(t_date, t_18, "pm_hash", [obs_995])

        # Query 999 fineness
        key_999 = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("999.0")
        )
        res_999 = PointInTimeMarketDataResolver.resolve_precious_metal(key_999, [snap])
        assert res_999.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION

        # Query summary benchmark (fineness=None)
        key_none = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=None
        )
        res_none = PointInTimeMarketDataResolver.resolve_precious_metal(key_none, [snap])
        assert res_none.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION

    def test_17_precious_settlement_isolation(self):
        """Scenario 17: T+0 vs None vs distinct raw settlement strings do not collide."""
        t_date = date(2024, 10, 1)
        t_18 = datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc)

        obs_2608 = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7140000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0"), raw_value_date_text="2608"
        )
        snap = create_mock_pm_snapshot(t_date, t_18, "pm_hash", [obs_2608])

        key_2708 = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0"), raw_value_date_text="2708"
        )
        res_2708 = PointInTimeMarketDataResolver.resolve_precious_metal(key_2708, [snap])
        assert res_2708.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION

    def test_18_precious_duplicate_valid_conflict(self):
        """Scenario 18: Multiple VALID observations with differing prices for identical key -> OBSERVATION_CONFLICT."""
        t_date = date(2024, 10, 1)
        t_18 = datetime(2024, 10, 1, 18, 0, tzinfo=timezone.utc)

        obs_1 = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7140000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        obs_2 = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7150000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap = create_mock_pm_snapshot(t_date, t_18, "pm_hash", [obs_1, obs_2])

        key = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD, effective_date=t_date, price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG, price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0")
        )
        res = PointInTimeMarketDataResolver.resolve_precious_metal(key, [snap])
        assert res.status == MarketDataResolutionStatus.OBSERVATION_CONFLICT

    def test_19_non_finite_decimal_defense(self):
        """Scenario 19: Non-finite Decimals (NaN, Infinity) are rejected."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date)

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        obs_nan = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("NaN"))
        snap_nan = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_nan])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_nan], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION

    def test_20_missing_snapshot_lineage_fails_closed(self):
        """Scenario 20: Observation snapshot_id or payload_hash mismatch -> INVALID_TEMPORAL_LINEAGE."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date)

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        # Explicit snapshot_hash on observation that mismatches snapshot
        obs_bad_hash = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), snapshot_hash="WRONG_HASH")
        snap = create_mock_bist_snapshot(t_date, t_10, "CORRECT_HASH", [obs_bad_hash])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    def test_21_order_independence(self):
        """Scenario 21: Permutations of snapshot order and observation order produce identical results."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date)

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_a])

        obs_b = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("101.00"))
        snap_b = create_mock_bist_snapshot(t_date, t_11, "hash_b", [obs_b])

        res_forward = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_a, snap_b])
        res_reverse = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_b, snap_a])

        assert res_forward.status == res_reverse.status
        assert res_forward.selected_observation.close == res_reverse.selected_observation.close
        assert res_forward.resolution_key == res_reverse.resolution_key

    def test_22_confidence_and_stale_discovery_propagation(self):
        """Scenario 22: Stale discovery degrades HIGH confidence to MEDIUM and adds diagnostic."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date)

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        obs = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), confidence=DataConfidenceLevel.HIGH)
        snap_stale = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs], is_stale_discovery=True)

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_stale], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.confidence == DataConfidenceLevel.MEDIUM
        assert res.is_stale_discovery is True
        assert any("DEGRADED_DISCOVERY" in d for d in res.diagnostics)

    def test_23_result_serialization_no_float(self):
        """Scenario 23: to_dict() serializes cleanly with Decimal string representation and zero floats."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        obs = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.50"))
        snap = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        d = res.to_dict()

        assert d["status"] == "SELECTED"
        assert d["resolution_mode"] == "CURRENT_REPORTED"
        assert d["selected_observation"]["close"] == "100.50"
        assert isinstance(d["selected_observation"]["close"], str)
        assert d["confidence"] == "high"
        assert d["effective_date"] == "2024-10-01"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9B.2.5 Hardening Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase9B25Hardening:

    def test_24_bist_future_conflict_isolated_in_system_as_of(self):
        """Scenario 24: BIST future snapshot conflict at 11:00 does not fail closed SYSTEM_AS_OF at 10:30."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        # 10:00 Valid Snapshot A
        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_valid_a", [obs_a])

        # 11:00 Conflicting Snapshots B1 and B2 (same retrieved_at, differing hashes)
        obs_b1 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("102.00"))
        snap_b1 = create_mock_bist_snapshot(t_date, t_11, "hash_b1", [obs_b1])

        obs_b2 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("103.00"))
        snap_b2 = create_mock_bist_snapshot(t_date, t_11, "hash_b2", [obs_b2])

        # As of 10:30 UTC: snap_a is selected cleanly; future 11:00 conflict is ignored
        res_pit = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a, snap_b1, snap_b2], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc)
        )
        assert res_pit.status == MarketDataResolutionStatus.SELECTED
        assert res_pit.selected_observation.close == Decimal("100.00")
        assert res_pit.snapshot_hash == "hash_valid_a"
        assert res_pit.evaluation_snapshot_ids == [str(snap_a.id)]

        # CURRENT_REPORTED: Evaluates all snapshots up to latest; 11:00 conflict fails closed
        res_curr = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a, snap_b1, snap_b2], MarketDataResolutionMode.CURRENT_REPORTED
        )
        assert res_curr.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT

    def test_25_precious_future_conflict_isolated_in_system_as_of(self):
        """Scenario 25: KMTP future snapshot conflict at 11:00 does not fail closed SYSTEM_AS_OF at 10:30."""
        t_date = date(2024, 10, 1)
        sem_key = PreciousMetalSemanticKey(
            metal=PreciousMetalType.GOLD,
            effective_date=t_date,
            price_currency=Currency.TRY,
            quantity_unit=PreciousMetalUnit.KG,
            price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
            fineness_per_mille=Decimal("995.0"),
        )

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        # 10:00 Valid Snapshot A
        obs_a = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7000000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_a = create_mock_pm_snapshot(t_date, t_10, "pm_hash_a", [obs_a])

        # 11:00 Conflicting Snapshots B1 and B2
        obs_b1 = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7100000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_b1 = create_mock_pm_snapshot(t_date, t_11, "pm_hash_b1", [obs_b1])

        obs_b2 = create_mock_pm_obs(
            PreciousMetalType.GOLD, t_date, Decimal("7200000.00"), Currency.TRY, PreciousMetalUnit.KG,
            PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=Decimal("995.0")
        )
        snap_b2 = create_mock_pm_snapshot(t_date, t_11, "pm_hash_b2", [obs_b2])

        # As of 10:30 UTC: snap_a selected
        res_pit = PointInTimeMarketDataResolver.resolve_precious_metal(
            sem_key, [snap_a, snap_b1, snap_b2], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc)
        )
        assert res_pit.status == MarketDataResolutionStatus.SELECTED
        assert res_pit.selected_observation.price == Decimal("7000000.00")
        assert res_pit.snapshot_hash == "pm_hash_a"
        assert res_pit.evaluation_snapshot_ids == [str(snap_a.id)]

        # CURRENT_REPORTED: Fails closed on 11:00 conflict
        res_curr = PointInTimeMarketDataResolver.resolve_precious_metal(
            sem_key, [snap_a, snap_b1, snap_b2], MarketDataResolutionMode.CURRENT_REPORTED
        )
        assert res_curr.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT

    def test_26_future_bad_lineage_does_not_poison_system_as_of(self):
        """Scenario 26: Bad lineage in a future snapshot at 11:00 does not poison SYSTEM_AS_OF at 10:30."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        # 10:00 Valid Snapshot
        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_a = create_mock_bist_snapshot(t_date, t_10, "hash_valid_a", [obs_a])

        # 11:00 Snapshot with lineage mismatch on observation
        obs_bad = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("105.00"), snapshot_hash="CORRUPTED_HASH")
        snap_bad = create_mock_bist_snapshot(t_date, t_11, "CORRECT_SNAP_HASH", [obs_bad])

        # SYSTEM_AS_OF 10:30 UTC -> snap_a selected cleanly
        res_pit = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a, snap_bad], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc)
        )
        assert res_pit.status == MarketDataResolutionStatus.SELECTED
        assert res_pit.selected_observation.close == Decimal("100.00")

        # CURRENT_REPORTED -> Evaluates latest (snap_bad) and fails closed on lineage
        res_curr = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_a, snap_bad], MarketDataResolutionMode.CURRENT_REPORTED
        )
        assert res_curr.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    def test_27_failed_http_fetch_does_not_supersede_valid_bulletin(self):
        """Scenario 27: Failed HTTP fetch attempt (HTTP 500) does not supersede previous valid HTTP 200 snapshot."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_11 = datetime(2024, 10, 1, 11, 0, tzinfo=timezone.utc)

        # 10:00 Successful full bulletin
        obs_a = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_ok = create_mock_bist_snapshot(t_date, t_10, "hash_ok", [obs_a], http_status=200)

        # 11:00 Failed HTTP fetch attempt (500)
        snap_failed = create_mock_bist_snapshot(t_date, t_11, "hash_err", [], http_status=500)

        # CURRENT_REPORTED: snap_ok remains authoritative
        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_ok, snap_failed], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("100.00")
        assert res.snapshot_hash == "hash_ok"

    def test_28_bist_same_close_different_row_fails_closed(self):
        """Scenario 28: Two VALID observations with same close but different volume/high -> OBSERVATION_CONFLICT."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)

        # Same close (100.00) but differing volume
        obs_1 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), volume=Decimal("5000"))
        obs_2 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), volume=Decimal("8000"))
        snap = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_1, obs_2])

        res = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res.status == MarketDataResolutionStatus.OBSERVATION_CONFLICT

    def test_29_bist_exact_logical_duplicate_deduplicates(self):
        """Scenario 29: Exact logical duplicate observations with different UUIDs deduplicate deterministically."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)

        id1 = uuid4()
        id2 = uuid4()
        obs_1 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), volume=Decimal("5000"), observation_id=id1)
        obs_2 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), volume=Decimal("5000"), observation_id=id2)
        snap = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_1, obs_2])

        res_fwd = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res_fwd.status == MarketDataResolutionStatus.SELECTED
        assert res_fwd.selected_observation.close == Decimal("100.00")

        # Reversed order inside snapshot yields exact same representative and resolution key
        snap_rev = create_mock_bist_snapshot(t_date, t_10, "hash_a", [obs_2, obs_1], snapshot_id=snap.id)
        res_rev = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_rev], MarketDataResolutionMode.CURRENT_REPORTED)
        assert res_rev.status == MarketDataResolutionStatus.SELECTED
        assert res_rev.resolution_key == res_fwd.resolution_key
        assert res_rev.selected_observation_id == res_fwd.selected_observation_id

    def test_30_resolution_key_uuid_independence(self):
        """Scenario 30: Identical economic datasets constructed with distinct UUIDs yield identical resolution_key."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)

        # Dataset 1
        obs_1 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), observation_id=uuid4())
        snap_1 = create_mock_bist_snapshot(t_date, t_10, "hash_fixed", [obs_1], snapshot_id=uuid4())

        # Dataset 2 (new UUIDs, identical economic fields and payload hash)
        obs_2 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"), observation_id=uuid4())
        snap_2 = create_mock_bist_snapshot(t_date, t_10, "hash_fixed", [obs_2], snapshot_id=uuid4())

        res_1 = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_1], MarketDataResolutionMode.CURRENT_REPORTED)
        res_2 = PointInTimeMarketDataResolver.resolve_bist_eod(key, [snap_2], MarketDataResolutionMode.CURRENT_REPORTED)

        assert res_1.status == MarketDataResolutionStatus.SELECTED
        assert res_2.status == MarketDataResolutionStatus.SELECTED
        assert res_1.resolution_key == res_2.resolution_key
        assert res_1.resolution_key is not None

    def test_31_evaluation_snapshot_ids_audit(self):
        """Scenario 31: evaluation_snapshot_ids lists only eligible snapshots at that boundary, sorted deterministically."""
        t_date = date(2024, 10, 1)
        inst_id = uuid4()
        key = BISTInstrumentQueryKey(instrument_id=inst_id, trade_date=t_date, symbol="THYAO")

        t_09 = datetime(2024, 10, 1, 9, 0, tzinfo=timezone.utc)
        t_10 = datetime(2024, 10, 1, 10, 0, tzinfo=timezone.utc)
        t_12 = datetime(2024, 10, 1, 12, 0, tzinfo=timezone.utc)

        obs_09 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("99.00"))
        snap_09 = create_mock_bist_snapshot(t_date, t_09, "hash_09", [obs_09])

        obs_10 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("100.00"))
        snap_10 = create_mock_bist_snapshot(t_date, t_10, "hash_10", [obs_10])

        obs_12 = create_mock_bist_obs("THYAO", inst_id, t_date, Decimal("102.00"))
        snap_12 = create_mock_bist_snapshot(t_date, t_12, "hash_12", [obs_12])

        # As of 10:30 UTC: only snap_09 and snap_10 are evaluated
        res_pit = PointInTimeMarketDataResolver.resolve_bist_eod(
            key, [snap_12, snap_09, snap_10], MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc)
        )
        assert res_pit.status == MarketDataResolutionStatus.SELECTED
        assert str(snap_12.id) not in res_pit.evaluation_snapshot_ids
        expected_ids = sorted([str(snap_09.id), str(snap_10.id)])
        assert res_pit.evaluation_snapshot_ids == expected_ids
