"""
backend/tests/test_tefas_metrics_resolver.py
============================================
Comprehensive Unit Test Suite for PointInTimeMarketDataResolver TEFAS Current Fund Metrics Resolution.

Test Coverage:
    1. CURRENT_REPORTED selects latest snapshot by retrieved_at knowledge time
    2. SYSTEM_AS_OF isolates historical knowledge by retrieved_at <= as_of
    3. Fresh PARTIAL metrics beat old COMPLETE metrics (no field resurrection)
    4. Latest HTTP-200 snapshot with invalid AUM blocks resurrection of older snapshot (fail-closed)
    5. Latest HTTP-200 snapshot with no observation blocks resurrection (fail-closed)
    6. HTTP transport failures (403, 429, 500) do not supersede older valid HTTP-200 snapshots
    7. Future invalid snapshots do not contaminate historical SYSTEM_AS_OF queries
    8. Same-time differing payload hashes yield SNAPSHOT_CONFLICT
    9. Logical duplicate snapshots with differing UUIDs deduplicate deterministically
    10. Lineage validation failures (provider, snapshot_id, hash, instrument, symbol, time mismatches)
    11. Fabricated effective_date rejected as INVALID_TEMPORAL_LINEAGE
    12. Fabricated published_at rejected as INVALID_TEMPORAL_LINEAGE
    13. Naive snapshot retrieved_at fails closed as INVALID_TEMPORAL_LINEAGE
    14. Naive as_of in SYSTEM_AS_OF fails closed as INVALID_TEMPORAL_LINEAGE
    15. Defensive model hardening: malformed investor_count or units cannot be COMPLETE
    16. Zero values for AUM, units, and investors remain valid and COMPLETE
    17. Exact Decimal precision preserved without quantization or float conversion
    18. SOURCE_AS_OF always returns UNAVAILABLE_SOURCE_AS_OF
    19. UUID independence: identical logical history produces identical resolution_key
    20. Input order independence: reversing snapshot list preserves resolution_key and result
    21. Evaluation snapshot IDs accurately audit evaluated candidates
    22. TRY-only safety: Non-TRY currency observation rejected
    23. Unsupported instrument type rejected as ineligible
    24. Resolver contains no artificial staleness threshold
    25. Latest snapshot with no observation produces deterministic resolution_key

Zero external network calls (pytest-socket enforced).
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
)
from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    MarketObservationResolutionResult,
    TefasFundCurrentMetricsQueryKey,
)
from backend.engine.private.market_data.resolver import (
    PointInTimeMarketDataResolver,
)
from backend.engine.private.market_data.tefas_metrics_models import (
    TefasFundCurrentMetricsObservation,
    TefasFundMetricsSnapshot,
)
from backend.engine.private.market_data.tefas_models import (
    TefasObservationStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture Helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_metrics_snapshot(
    instrument_id: UUID,
    symbol: str,
    retrieved_at: datetime,
    portfolio_size: Optional[Decimal] = Decimal("1000000000.00"),
    portfolio_size_currency: Optional[Currency] = Currency.TRY,
    outstanding_units: Optional[Decimal] = Decimal("10000000"),
    investor_count: Optional[int] = 10000,
    reported_current_unit_price: Optional[Decimal] = Decimal("100.00"),
    status: TefasObservationStatus = TefasObservationStatus.VALID,
    http_status: int = 200,
    payload_hash: Optional[str] = None,
    instrument_type: InstrumentType = InstrumentType.TEFAS_FUND,
    has_observation: bool = True,
    effective_date: Optional[date] = None,
    published_at: Optional[datetime] = None,
    snap_id: Optional[UUID] = None,
    obs_id: Optional[UUID] = None,
) -> TefasFundMetricsSnapshot:
    s_id = snap_id or uuid4()
    p_hash = payload_hash or f"hash_{retrieved_at.isoformat()}_{symbol}"

    obs: Optional[TefasFundCurrentMetricsObservation] = None
    if has_observation:
        o_id = obs_id or uuid4()
        obs = TefasFundCurrentMetricsObservation(
            id=o_id,
            snapshot_id=s_id,
            instrument_id=instrument_id,
            provider="TEFAS",
            provider_symbol=symbol,
            portfolio_size=portfolio_size,
            portfolio_size_currency=portfolio_size_currency,
            outstanding_units=outstanding_units,
            investor_count=investor_count,
            reported_current_unit_price=reported_current_unit_price,
            instrument_type=instrument_type,
            payload_hash=p_hash,
            retrieved_at=retrieved_at,
            published_at=published_at,
            effective_date=effective_date,
            status=status,
            confidence_level=DataConfidenceLevel.MEDIUM,
        )

    return TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol=symbol,
        retrieved_at=retrieved_at,
        http_status=http_status,
        payload_hash=p_hash,
        raw_payload=f'{{"fonKodu": "{symbol}"}}',
        instrument_id=instrument_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────────────

def test_01_current_reported_selects_latest():
    """Verify CURRENT_REPORTED selects latest snapshot by retrieved_at."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
        investor_count=10000,
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1100000000.00"),
        investor_count=10500,
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation is not None
    assert res.selected_observation.portfolio_size == Decimal("1100000000.00")
    assert res.selected_observation.investor_count == 10500
    assert res.snapshot_id == s2.id


def test_02_system_as_of_historical_isolation():
    """Verify SYSTEM_AS_OF isolates historical knowledge using retrieved_at <= as_of."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
        investor_count=10000,
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1100000000.00"),
        investor_count=10500,
    )

    as_of = datetime(2026, 8, 27, 10, 30, 0, tzinfo=timezone.utc)
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("1000000000.00")
    assert res.snapshot_id == s1.id


def test_03_partial_freshness_beats_old_complete():
    """Verify fresh PARTIAL metrics beat older COMPLETE metrics without field resurrection."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    # 10:00 COMPLETE
    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
        outstanding_units=Decimal("10000000"),
        investor_count=10000,
    )
    # 11:00 PARTIAL (missing investor_count)
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1100000000.00"),
        outstanding_units=Decimal("11000000"),
        investor_count=None,
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("1100000000.00")
    assert res.selected_observation.investor_count is None
    # Verify normalized record is PARTIAL
    norm = res.selected_observation.to_normalized_observation_record()
    assert norm.data_status == DataStatus.PARTIAL


def test_04_invalid_aum_no_resurrection():
    """Verify latest HTTP-200 snapshot with invalid AUM fails closed (no resurrection)."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        portfolio_size=None,  # Invalid AUM
        status=TefasObservationStatus.INVALID_OBSERVATION,
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res.selected_observation is None
    assert res.resolution_key is not None


def test_05_no_observation_no_resurrection():
    """Verify latest HTTP-200 snapshot with observation=None fails closed (no resurrection)."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        has_observation=False,  # e.g. EMPTY_RESPONSE
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res.selected_observation is None
    assert res.resolution_key is not None


def test_06_failed_fetch_isolation():
    """Verify HTTP transport failure (e.g. 500) does not supersede older valid HTTP-200 snapshot."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
        http_status=200,
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        portfolio_size=None,
        http_status=500,
        has_observation=False,
    )

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("1000000000.00")
    assert res.snapshot_id == s1.id


def test_07_future_invalid_isolation():
    """Verify future invalid snapshot does not contaminate historical SYSTEM_AS_OF query."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        has_observation=False,
    )

    as_of = datetime(2026, 8, 27, 10, 30, 0, tzinfo=timezone.utc)
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of
    )
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("1000000000.00")
    assert res.snapshot_id == s1.id


def test_08_same_time_hash_conflict():
    """Verify multiple snapshots at exact same retrieved_at with differing payload hashes return SNAPSHOT_CONFLICT."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash="hash_AAA")
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash="hash_BBB")

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT


def test_09_logical_duplicate_deduped():
    """Verify logical duplicate snapshots with different UUIDs deduplicate cleanly."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash="hash_SAME", snap_id=uuid4(), obs_id=uuid4())
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash="hash_SAME", snap_id=uuid4(), obs_id=uuid4())

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s1, s2], mode=MarketDataResolutionMode.CURRENT_REPORTED
    )
    assert res.status == MarketDataResolutionStatus.SELECTED


def test_10_lineage_validation_failures():
    """Verify observation lineage mismatches return INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. snapshot_id mismatch
    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time)
    s1.observation.snapshot_id = uuid4()  # Mismatch
    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1])
    assert res1.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    # 2. payload_hash mismatch
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time)
    s2.observation.payload_hash = "wrong_hash"
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2])
    assert res2.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    # 3. instrument_id mismatch
    s3 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time)
    s3.observation.instrument_id = uuid4()
    res3 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s3])
    assert res3.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    # 4. symbol mismatch
    s4 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time)
    s4.observation.provider_symbol = "XYZ"
    res4 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s4])
    assert res4.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_11_fabricated_effective_date_rejected():
    """Verify observation with non-None effective_date is rejected as INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        effective_date=date(2026, 8, 27),  # Fabricated date
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_12_fabricated_published_at_rejected():
    """Verify observation with non-None published_at is rejected as INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        published_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),  # Fabricated timestamp
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_13_naive_snapshot_retrieved_at_fails_closed():
    """Verify naive retrieved_at on snapshot fails closed with INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0),  # Naive
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_14_naive_system_as_of_fails_closed():
    """Verify naive as_of in SYSTEM_AS_OF mode fails closed with INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=datetime(2026, 8, 27, 12, 0, 0)  # Naive
    )
    assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_15_optional_field_model_hardening():
    """Verify hardened to_normalized_observation_record rejects malformed investor_count / units from COMPLETE."""
    # 1. Negative investor_count
    obs1 = TefasFundCurrentMetricsObservation(
        provider_symbol="MAC",
        portfolio_size=Decimal("1000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("1000"),
        investor_count=-1,
    )
    assert obs1.to_normalized_observation_record().data_status == DataStatus.PARTIAL

    # 2. Boolean investor_count
    obs2 = TefasFundCurrentMetricsObservation(
        provider_symbol="MAC",
        portfolio_size=Decimal("1000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("1000"),
        investor_count=True,  # type: ignore
    )
    assert obs2.to_normalized_observation_record().data_status == DataStatus.PARTIAL


def test_16_zero_values_valid_and_complete():
    """Verify zero values for AUM, units, and investors resolve cleanly to SELECTED and COMPLETE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("0"),
        outstanding_units=Decimal("0"),
        investor_count=0,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("0")
    assert res.selected_observation.to_normalized_observation_record().data_status == DataStatus.COMPLETE


def test_17_exact_decimal_preserved():
    """Verify exact Decimal precision is preserved without quantization or float contamination."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("0.100000"),
        reported_current_unit_price=Decimal("0.000001"),
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("0.100000")
    assert str(res.selected_observation.portfolio_size) == "0.100000"


def test_18_source_as_of_always_unavailable():
    """Verify SOURCE_AS_OF mode always returns UNAVAILABLE_SOURCE_AS_OF."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(
        query, [s], mode=MarketDataResolutionMode.SOURCE_AS_OF
    )
    assert res.status == MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF


def test_19_uuid_independence():
    """Verify regenerating UUIDs for logically identical history produces identical resolution_key."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, snap_id=uuid4(), obs_id=uuid4())
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, snap_id=uuid4(), obs_id=uuid4())

    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1])
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2])

    assert res1.resolution_key == res2.resolution_key


def test_20_input_order_independence():
    """Verify reversing input snapshot list preserves resolution_key and selected result."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1000000000.00"),
    )
    s2 = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("1100000000.00"),
    )

    res_fwd = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1, s2])
    res_rev = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2, s1])

    assert res_fwd.resolution_key == res_rev.resolution_key
    assert res_fwd.selected_observation_id == res_rev.selected_observation_id


def test_21_evaluation_snapshot_ids_audit():
    """Verify evaluation_snapshot_ids exposes sorted IDs of candidate snapshots."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc))
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc))
    s_failed = create_metrics_snapshot(inst_id, "MAC", retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc), http_status=500)

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1, s2, s_failed])
    expected_ids = sorted([str(s1.id), str(s2.id)])
    assert res.evaluation_snapshot_ids == expected_ids
    assert str(s_failed.id) not in res.evaluation_snapshot_ids


def test_22_try_only_safety():
    """Verify observation with non-TRY currency is rejected as ineligible."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="EURF")

    s = create_metrics_snapshot(
        inst_id, "EURF",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        portfolio_size_currency=Currency.EUR,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_23_unsupported_instrument_type_fails_closed():
    """Verify unsupported instrument type (e.g. US_STOCK) fails closed as ineligible."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        instrument_type=InstrumentType.US_STOCK,  # type: ignore
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_24_no_staleness_policy_in_resolver():
    """Verify resolver does not reject older snapshots based on artificial staleness cutoffs."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    # Snapshot from 60 days ago
    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("1000000000.00")


def test_25_no_observation_deterministic_resolution_key():
    """Verify latest snapshot with observation=None produces deterministic resolution_key."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, has_observation=False, payload_hash="hash_EMPTY", snap_id=uuid4())
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, has_observation=False, payload_hash="hash_EMPTY", snap_id=uuid4())

    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1])
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2])

    assert res1.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res1.resolution_key == res2.resolution_key
    assert res1.resolution_key is not None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 11D.3.5 — Fail-Closed Type, Identity & Resolution-Key Hardening Tests
# ─────────────────────────────────────────────────────────────────────────────

def _create_well_formed_obs(
    instrument_id: UUID,
    symbol: str,
    retrieved_at: datetime,
    snap_id: UUID,
    payload_hash: str,
    parser_version: str = "1.0.0",
    instrument_type=InstrumentType.TEFAS_FUND,
) -> TefasFundCurrentMetricsObservation:
    """Create a well-formed observation with correct lineage for isolation testing."""
    return TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=snap_id,
        instrument_id=instrument_id,
        provider="TEFAS",
        provider_symbol=symbol,
        portfolio_size=Decimal("1000000000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"),
        investor_count=10000,
        reported_current_unit_price=Decimal("100.00"),
        instrument_type=instrument_type,
        payload_hash=payload_hash,
        retrieved_at=retrieved_at,
        published_at=None,
        effective_date=None,
        status=TefasObservationStatus.VALID,
        confidence_level=DataConfidenceLevel.MEDIUM,
    )


def _create_snap_raw(
    instrument_id: UUID,
    symbol: str,
    retrieved_at: datetime,
    observation,
    payload_hash: str,
    parser_version: str = "1.0.0",
) -> TefasFundMetricsSnapshot:
    """Create snapshot referencing a given observation object directly."""
    s_id = uuid4()
    if observation is not None:
        observation.snapshot_id = s_id
    return TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol=symbol,
        retrieved_at=retrieved_at,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload=f'{{"fonKodu": "{symbol}"}}',
        instrument_id=instrument_id,
        endpoint="FUND_CURRENT_METRICS",
        parser_version=parser_version,
        observation=observation,
    )


def test_26_missing_instrument_type_fails_closed():
    """Verify instrument_type=None is INELIGIBLE (positive membership required)."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_type_none"
    s_id = uuid4()

    obs = TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=s_id,
        instrument_id=inst_id,
        provider="TEFAS",
        provider_symbol="MAC",
        portfolio_size=Decimal("1000000000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"),
        investor_count=10000,
        payload_hash=payload_hash,
        retrieved_at=ret_time,
        status=TefasObservationStatus.VALID,
        instrument_type=None,  # None -> INELIGIBLE
    )
    snap = TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol="MAC",
        retrieved_at=ret_time,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload='{}',
        instrument_id=inst_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_27_float_portfolio_size_fails_closed():
    """Verify float portfolio_size is INELIGIBLE — no AttributeError raised."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_float_psize"
    s_id = uuid4()

    obs = TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=s_id,
        instrument_id=inst_id,
        provider="TEFAS",
        provider_symbol="MAC",
        portfolio_size=1000.0,  # type: ignore  # float, not Decimal
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"),
        investor_count=10000,
        payload_hash=payload_hash,
        retrieved_at=ret_time,
        status=TefasObservationStatus.VALID,
        instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap = TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol="MAC",
        retrieved_at=ret_time,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload='{}',
        instrument_id=inst_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_28_string_portfolio_size_fails_closed():
    """Verify string portfolio_size is INELIGIBLE — no coercion."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_str_psize"
    s_id = uuid4()

    obs = TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=s_id,
        instrument_id=inst_id,
        provider="TEFAS",
        provider_symbol="MAC",
        portfolio_size="1000000000.00",  # type: ignore  # str, not Decimal
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"),
        investor_count=10000,
        payload_hash=payload_hash,
        retrieved_at=ret_time,
        status=TefasObservationStatus.VALID,
        instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap = TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol="MAC",
        retrieved_at=ret_time,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload='{}',
        instrument_id=inst_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_29_int_portfolio_size_fails_closed():
    """Verify int portfolio_size is INELIGIBLE — canonical persisted metric must be Decimal."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_int_psize"
    s_id = uuid4()

    obs = TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=s_id,
        instrument_id=inst_id,
        provider="TEFAS",
        provider_symbol="MAC",
        portfolio_size=1000,  # type: ignore  # int, not Decimal
        portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"),
        investor_count=10000,
        payload_hash=payload_hash,
        retrieved_at=ret_time,
        status=TefasObservationStatus.VALID,
        instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap = TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol="MAC",
        retrieved_at=ret_time,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload='{}',
        instrument_id=inst_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_30_float_outstanding_units_fails_closed():
    """Verify float outstanding_units is INELIGIBLE — no Decimal(float) coercion."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_float_units"
    s_id = uuid4()

    obs = TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=s_id,
        instrument_id=inst_id,
        provider="TEFAS",
        provider_symbol="MAC",
        portfolio_size=Decimal("1000000000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units=1000.0,  # type: ignore  # float, not Decimal
        investor_count=10000,
        payload_hash=payload_hash,
        retrieved_at=ret_time,
        status=TefasObservationStatus.VALID,
        instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap = TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol="MAC",
        retrieved_at=ret_time,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload='{}',
        instrument_id=inst_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_31_string_outstanding_units_fails_closed():
    """Verify string outstanding_units is INELIGIBLE — no coercion."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_str_units"
    s_id = uuid4()

    obs = TefasFundCurrentMetricsObservation(
        id=uuid4(),
        snapshot_id=s_id,
        instrument_id=inst_id,
        provider="TEFAS",
        provider_symbol="MAC",
        portfolio_size=Decimal("1000000000.00"),
        portfolio_size_currency=Currency.TRY,
        outstanding_units="1000",  # type: ignore  # str, not Decimal
        investor_count=10000,
        payload_hash=payload_hash,
        retrieved_at=ret_time,
        status=TefasObservationStatus.VALID,
        instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap = TefasFundMetricsSnapshot(
        id=s_id,
        provider="TEFAS",
        provider_symbol="MAC",
        retrieved_at=ret_time,
        http_status=200,
        payload_hash=payload_hash,
        raw_payload='{}',
        instrument_id=inst_id,
        endpoint="FUND_CURRENT_METRICS",
        observation=obs,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap])
    assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_32_none_outstanding_units_still_selected():
    """Verify outstanding_units=None is SELECTED (missing != malformed). Normalized to PARTIAL."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        outstanding_units=None,
        investor_count=10000,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.outstanding_units is None
    norm = res.selected_observation.to_normalized_observation_record()
    assert norm.data_status == DataStatus.PARTIAL


def test_33_malformed_investor_count_resolver_fails_closed():
    """Verify malformed investor_count (negative and bool) fails closed at resolver level."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Negative
    s_neg = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, investor_count=-1, payload_hash="hash_inv_neg")
    res_neg = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s_neg])
    assert res_neg.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION

    # Bool (True)
    s_bool = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, investor_count=True, payload_hash="hash_inv_bool")  # type: ignore
    res_bool = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s_bool])
    assert res_bool.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION


def test_34_same_time_different_symbol_conflict():
    """Verify same retrieved_at, same payload_hash, different provider symbols -> SNAPSHOT_CONFLICT."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_same"

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash=payload_hash)
    s2 = create_metrics_snapshot(inst_id, "XYZ", retrieved_at=ret_time, payload_hash=payload_hash)

    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1, s2])
    assert res.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT
    assert "SNAPSHOT_CONFLICT" in res.diagnostics[0]


def test_35_symbol_lineage_missing_vs_present():
    """Verify snapshot symbol MAC vs observation symbol '' (empty) is INVALID_TEMPORAL_LINEAGE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_sym_mismatch"
    s_id = uuid4()

    # Case 1: snapshot=MAC, obs=""
    obs1 = TefasFundCurrentMetricsObservation(
        id=uuid4(), snapshot_id=s_id, instrument_id=inst_id,
        provider="TEFAS", provider_symbol="",
        portfolio_size=Decimal("1000000000.00"), portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"), investor_count=10000,
        payload_hash=payload_hash, retrieved_at=ret_time,
        status=TefasObservationStatus.VALID, instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap1 = TefasFundMetricsSnapshot(
        id=s_id, provider="TEFAS", provider_symbol="MAC",
        retrieved_at=ret_time, http_status=200, payload_hash=payload_hash,
        raw_payload='{}', instrument_id=inst_id, endpoint="FUND_CURRENT_METRICS",
        observation=obs1,
    )
    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap1])
    assert res1.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    # Case 2: snapshot="", obs=MAC
    s_id2 = uuid4()
    obs2 = TefasFundCurrentMetricsObservation(
        id=uuid4(), snapshot_id=s_id2, instrument_id=inst_id,
        provider="TEFAS", provider_symbol="MAC",
        portfolio_size=Decimal("1000000000.00"), portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("10000000"), investor_count=10000,
        payload_hash=payload_hash, retrieved_at=ret_time,
        status=TefasObservationStatus.VALID, instrument_type=InstrumentType.TEFAS_FUND,
    )
    snap2 = TefasFundMetricsSnapshot(
        id=s_id2, provider="TEFAS", provider_symbol="",
        retrieved_at=ret_time, http_status=200, payload_hash=payload_hash,
        raw_payload='{}', instrument_id=inst_id, endpoint="FUND_CURRENT_METRICS",
        observation=obs2,
    )
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [snap2])
    assert res2.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE


def test_36_parser_version_changes_resolution_key():
    """Verify that changing only parser_version produces a different resolution_key."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_same_econ"

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash=payload_hash)
    s1.parser_version = "1.0.0"
    if s1.observation:
        s1.observation.snapshot_id = s1.id

    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash=payload_hash, snap_id=uuid4(), obs_id=uuid4())
    s2.parser_version = "1.0.1"
    if s2.observation:
        s2.observation.snapshot_id = s2.id

    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1])
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2])

    assert res1.status == MarketDataResolutionStatus.SELECTED
    assert res2.status == MarketDataResolutionStatus.SELECTED
    # Different parser_version -> different resolution_key
    assert res1.resolution_key != res2.resolution_key


def test_37_uuid_independence_with_parser_version():
    """Verify same parser version and economics with different UUIDs produce identical resolution_key."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_uuid_indep"

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash=payload_hash, snap_id=uuid4(), obs_id=uuid4())
    s1.parser_version = "1.0.0"
    if s1.observation:
        s1.observation.snapshot_id = s1.id

    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, payload_hash=payload_hash, snap_id=uuid4(), obs_id=uuid4())
    s2.parser_version = "1.0.0"
    if s2.observation:
        s2.observation.snapshot_id = s2.id

    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1])
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2])

    assert res1.status == MarketDataResolutionStatus.SELECTED
    assert res2.status == MarketDataResolutionStatus.SELECTED
    assert res1.resolution_key == res2.resolution_key


def test_38_no_observation_key_changes_with_symbol():
    """Verify no-observation resolution_key changes when provider_symbol changes."""
    inst_id = uuid4()
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_no_obs"

    query_mac = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    s_mac = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, has_observation=False, payload_hash=payload_hash)
    res_mac = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query_mac, [s_mac])

    query_xyz = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="XYZ")
    s_xyz = create_metrics_snapshot(inst_id, "XYZ", retrieved_at=ret_time, has_observation=False, payload_hash=payload_hash)
    res_xyz = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query_xyz, [s_xyz])

    assert res_mac.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res_xyz.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
    assert res_mac.resolution_key != res_xyz.resolution_key


def test_39_no_observation_key_changes_with_parser_version():
    """Verify no-observation resolution_key changes when parser_version changes."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")
    ret_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    payload_hash = "hash_no_obs_pv"

    s1 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, has_observation=False, payload_hash=payload_hash, snap_id=uuid4())
    s1.parser_version = "1.0.0"
    s2 = create_metrics_snapshot(inst_id, "MAC", retrieved_at=ret_time, has_observation=False, payload_hash=payload_hash, snap_id=uuid4())
    s2.parser_version = "2.0.0"

    res1 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s1])
    res2 = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s2])

    assert res1.resolution_key != res2.resolution_key


def test_40_exact_decimal_regression():
    """Verify Decimal('0.1000') remains exactly Decimal('0.1000') without quantization."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("0.1000"),
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("0.1000")
    assert str(res.selected_observation.portfolio_size) == "0.1000"


def test_41_valid_zero_values_complete():
    """Verify Decimal('0') AUM, Decimal('0') units, investor_count=0 are eligible and COMPLETE."""
    inst_id = uuid4()
    query = TefasFundCurrentMetricsQueryKey(instrument_id=inst_id, provider_symbol="MAC")

    s = create_metrics_snapshot(
        inst_id, "MAC",
        retrieved_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        portfolio_size=Decimal("0"),
        outstanding_units=Decimal("0"),
        investor_count=0,
    )
    res = PointInTimeMarketDataResolver.resolve_tefas_current_metrics(query, [s])
    assert res.status == MarketDataResolutionStatus.SELECTED
    assert res.selected_observation.portfolio_size == Decimal("0")
    norm = res.selected_observation.to_normalized_observation_record()
    assert norm.data_status == DataStatus.COMPLETE

