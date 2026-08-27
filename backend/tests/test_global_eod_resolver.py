"""
backend/tests/test_global_eod_resolver.py
=========================================
Comprehensive Test Suite for PointInTimeMarketDataResolver Global EOD Resolution (Phase 10E & 10E.5).

Tests all Point-in-Time invariants for Alpha Vantage, Tiingo, and Marketstack EOD:
    1. Tiingo current correction: Later retrieved_at supersedes under CURRENT_REPORTED; earlier selected under SYSTEM_AS_OF.
    2. Future isolation: Future corruption/conflict at 11:00 does not contaminate SYSTEM_AS_OF at 10:30.
    3. Incremental snapshot does not erase history: 2026 incremental snapshot does not supersede 2024 query.
    4. True no-resurrection: Newer snapshot covering target date with missing/invalid row returns NO_ELIGIBLE_OBSERVATION.
    5. Alpha Vantage compact range: Compact snapshot covering target date with missing row does not resurrect older snapshot.
    6. Alpha Vantage compact range outside target: Recent compact snapshot does not supersede older historical snapshot.
    7. Different instrument isolation: Later SPY snapshot never supersedes AAPL snapshot.
    8. Different provider isolation: Later Alpha Vantage AAPL snapshot never supersedes TIINGO query.
    9. Failed fetch isolation: HTTP 500 snapshot does not supersede earlier successful HTTP 200 snapshot.
    10. Same-boundary hash conflict: Identical scope and retrieved_at with differing payload_hash flags SNAPSHOT_CONFLICT.
    11. Logical duplicate snapshot deduplication: Different snapshot UUIDs with identical content deduplicate safely.
    12. Observation conflict: Multiple differing observations for target date inside authoritative snapshot flag OBSERVATION_CONFLICT.
    13. UUID-independent resolution key: Rebuilding identical economic snapshots with new UUIDs yields identical resolution_key.
    14. Naive timestamp rejection: Naive retrieved_at on covering snapshot or naive as_of flags INVALID_TEMPORAL_LINEAGE.
    15. SOURCE_AS_OF unavailable: Requesting SOURCE_AS_OF returns UNAVAILABLE_SOURCE_AS_OF without approximation.
    16. Adjusted fields preserved: Tiingo / Marketstack adj_close, div_cash, split_factor preserved without modification.
    17. Alpha Vantage raw-only valid: Alpha Vantage observation with close present and adj_close None is valid and selectable.
    18. Input order independence: Reversing snapshot list and observation list produces identical result and resolution key.
    19. Evaluation snapshot IDs: Exposes sorted physical IDs of eligible covering snapshots; excludes future under SYSTEM_AS_OF.
    20. One-sided start does not overclaim: start_date set with end_date None does not cover future target.
    21. One-sided end does not overclaim: end_date set with start_date None does not cover past target.
    22. One-sided with actual target: exact observation presence establishes coverage.
    23. Failed naive snapshot: Failed HTTP 500 snapshot with naive timestamp does not poison resolution of valid snapshot.
    24. Non-covering naive snapshot: Successful naive snapshot that does not cover target does not poison historical resolution.
    25. Covering naive snapshot fails closed: Successful covering naive snapshot returns INVALID_TEMPORAL_LINEAGE.
    26. Scope UUID swap: Swapping physical UUIDs between different scopes at same retrieved_at yields identical resolution key.
    27. Lineage classification: Observation with mismatched snapshot_id, payload_hash, or provider returns INVALID_TEMPORAL_LINEAGE.
    28. Zero network calls (pytest-socket active).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    Currency,
    DataConfidenceLevel,
    InstrumentType,
)
from backend.engine.private.market_data.global_models import (
    GlobalEODObservation,
    GlobalEODSnapshot,
    GlobalObservationStatus,
)
from backend.engine.private.market_data.models import (
    GlobalEODQueryKey,
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
)
from backend.engine.private.market_data.resolver import (
    PointInTimeMarketDataResolver,
)


AAPL_ID = UUID("11111111-1111-1111-1111-111111111111")
SPY_ID = UUID("22222222-2222-2222-2222-222222222222")
MBG_ID = UUID("33333333-3333-3333-3333-333333333333")


def make_obs(
    instrument_id: UUID,
    provider: str,
    provider_symbol: str,
    trade_date: date,
    close: Decimal,
    open_val: Optional[Decimal] = None,
    high: Optional[Decimal] = None,
    low: Optional[Decimal] = None,
    volume: Optional[Decimal] = None,
    adj_close: Optional[Decimal] = None,
    div_cash: Optional[Decimal] = None,
    split_factor: Optional[Decimal] = None,
    status: GlobalObservationStatus = GlobalObservationStatus.VALID,
    currency: Currency = Currency.USD,
    exchange: str = "XNAS",
    instrument_type: InstrumentType = InstrumentType.US_STOCK,
    snapshot_id: Optional[UUID] = None,
    payload_hash: str = "hash1",
    retrieved_at: Optional[datetime] = None,
    obs_id: Optional[UUID] = None,
) -> GlobalEODObservation:
    return GlobalEODObservation(
        id=obs_id or uuid4(),
        provider_symbol=provider_symbol,
        trade_date=trade_date,
        close=close,
        open=open_val or close,
        high=high or close,
        low=low or close,
        volume=volume or Decimal("1000000"),
        adj_close=adj_close,
        div_cash=div_cash or Decimal("0.0"),
        split_factor=split_factor or Decimal("1.0"),
        currency=currency,
        exchange=exchange,
        instrument_id=instrument_id,
        instrument_type=instrument_type,
        provider=provider,
        snapshot_id=snapshot_id,
        payload_hash=payload_hash,
        retrieved_at=retrieved_at,
        status=status,
        confidence_level=DataConfidenceLevel.MEDIUM if status == GlobalObservationStatus.VALID else DataConfidenceLevel.NONE,
    )


def make_snapshot(
    instrument_id: UUID,
    provider: str,
    provider_symbol: str,
    retrieved_at: Optional[datetime],
    payload_hash: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    trade_date_range: tuple = (None, None),
    observations: Optional[List[GlobalEODObservation]] = None,
    http_status: int = 200,
    snap_id: Optional[UUID] = None,
    endpoint: Optional[str] = None,
    output_size: Optional[str] = None,
) -> GlobalEODSnapshot:
    sid = snap_id or uuid4()
    obs_list = observations or []
    for o in obs_list:
        o.snapshot_id = sid
        o.payload_hash = payload_hash
        o.retrieved_at = retrieved_at
        o.provider = provider
        o.instrument_id = instrument_id

    min_d = min((o.trade_date for o in obs_list), default=None)
    max_d = max((o.trade_date for o in obs_list), default=None)
    eff_range = trade_date_range if trade_date_range != (None, None) else (min_d, max_d)

    return GlobalEODSnapshot(
        id=sid,
        provider=provider,
        provider_symbol=provider_symbol,
        instrument_id=instrument_id,
        retrieved_at=retrieved_at,
        http_status=http_status,
        payload_hash=payload_hash,
        raw_payload="{}",
        endpoint=endpoint,
        output_size=output_size,
        start_date=start_date,
        end_date=end_date,
        trade_date_range=eff_range,
        observations=obs_list,
    )


class TestGlobalEODPointInTimeResolver:

    def test_01_tiingo_current_correction_and_system_as_of(self):
        """Test 1: Later retrieved_at supersedes under CURRENT_REPORTED; earlier selected under SYSTEM_AS_OF."""
        t_target = date(2024, 6, 10)
        t_early = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_late = datetime(2024, 6, 12, 14, 0, tzinfo=timezone.utc)

        obs_early = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), adj_close=Decimal("190.50"))
        snap_early = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_early, "hash_early",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_early]
        )

        obs_late = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("191.00"), adj_close=Decimal("191.00"))
        snap_late = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_late, "hash_late",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_late]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")

        # CURRENT_REPORTED -> selects later corrected value
        res_curr = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_early, snap_late])
        assert res_curr.status == MarketDataResolutionStatus.SELECTED
        assert res_curr.selected_observation.close == Decimal("191.00")
        assert res_curr.snapshot_hash == "hash_late"

        # SYSTEM_AS_OF before correction -> selects earlier value
        as_of_mid = datetime(2024, 6, 11, 12, 0, tzinfo=timezone.utc)
        res_sys = PointInTimeMarketDataResolver.resolve_global_eod(
            query, [snap_early, snap_late], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_mid
        )
        assert res_sys.status == MarketDataResolutionStatus.SELECTED
        assert res_sys.selected_observation.close == Decimal("190.50")
        assert res_sys.snapshot_hash == "hash_early"

    def test_02_future_isolation(self):
        """Test 2: Future corruption/conflict at 11:00 does not contaminate SYSTEM_AS_OF at 10:30."""
        t_target = date(2024, 6, 10)
        t_1000 = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_1100 = datetime(2024, 6, 11, 11, 0, tzinfo=timezone.utc)

        obs_valid = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))
        snap_valid = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_1000, "hash_valid", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_valid])

        # Conflicting snapshot at 11:00
        snap_bad_1 = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_1100, "hash_bad_1", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("10.00"))])
        snap_bad_2 = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_1100, "hash_bad_2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("20.00"))])

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")

        # CURRENT_REPORTED fails closed due to 11:00 conflict
        res_curr = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_valid, snap_bad_1, snap_bad_2])
        assert res_curr.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT

        # SYSTEM_AS_OF 10:30 cleanly selects 10:00 without contamination
        as_of_1030 = datetime(2024, 6, 11, 10, 30, tzinfo=timezone.utc)
        res_sys = PointInTimeMarketDataResolver.resolve_global_eod(
            query, [snap_valid, snap_bad_1, snap_bad_2], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_1030
        )
        assert res_sys.status == MarketDataResolutionStatus.SELECTED
        assert res_sys.selected_observation.close == Decimal("190.50")
        assert res_sys.snapshot_hash == "hash_valid"

    def test_03_incremental_does_not_erase_history(self):
        """Test 3: 2026 incremental snapshot does not supersede or erase 2024 historical query."""
        t_hist = date(2024, 6, 10)
        t_snap_hist = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        t_snap_inc = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

        # 2020-2025 history snapshot
        obs_hist = make_obs(AAPL_ID, "TIINGO", "AAPL", t_hist, Decimal("190.50"))
        snap_hist = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_snap_hist, "hash_hist",
            start_date=date(2020, 1, 1), end_date=date(2025, 12, 31),
            observations=[obs_hist]
        )

        # 2026 incremental snapshot
        obs_inc = make_obs(AAPL_ID, "TIINGO", "AAPL", date(2026, 8, 19), Decimal("225.00"))
        snap_inc = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_snap_inc, "hash_inc",
            start_date=date(2026, 8, 19), end_date=date(2026, 8, 20),
            observations=[obs_inc]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_hist, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_hist, snap_inc])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("190.50")
        assert res.snapshot_hash == "hash_hist"

    def test_04_true_no_resurrection_when_covered(self):
        """Test 4: Newer snapshot covering target date with missing row returns NO_ELIGIBLE_OBSERVATION."""
        t_target = date(2024, 6, 10)
        t_1000 = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_1100 = datetime(2024, 6, 11, 11, 0, tzinfo=timezone.utc)

        obs_old = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))
        snap_old = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_1000, "hash_old",
            start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
            observations=[obs_old]
        )

        # Newer snapshot explicitly covers 2024-01-01 -> 2024-12-31, but 2024-06-10 is absent
        obs_other = make_obs(AAPL_ID, "TIINGO", "AAPL", date(2024, 6, 11), Decimal("192.00"))
        snap_new = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_1100, "hash_new",
            start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
            observations=[obs_other]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_old, snap_new])

        assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
        assert res.selected_observation is None
        assert res.snapshot_hash == "hash_new"

    def test_05_alpha_vantage_compact_range_coverage(self):
        """Test 5: Alpha Vantage compact snapshot covering target date without target row does not resurrect."""
        t_target = date(2026, 7, 1)
        t_1000 = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
        t_1100 = datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc)

        obs_old = make_obs(AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_target, Decimal("215.00"))
        snap_old = make_snapshot(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_1000, "hash_av_old",
            trade_date_range=(date(2026, 5, 1), date(2026, 8, 27)),
            observations=[obs_old],
            endpoint="TIME_SERIES_DAILY",
            output_size="compact",
        )

        # Later compact snapshot covers 2026-05-01 to 2026-08-27, but t_target row is absent
        obs_other = make_obs(AAPL_ID, "ALPHA_VANTAGE", "AAPL", date(2026, 8, 26), Decimal("220.00"))
        snap_new = make_snapshot(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_1100, "hash_av_new",
            trade_date_range=(date(2026, 5, 1), date(2026, 8, 27)),
            observations=[obs_other],
            endpoint="TIME_SERIES_DAILY",
            output_size="compact",
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="ALPHA_VANTAGE")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_old, snap_new])

        assert res.status == MarketDataResolutionStatus.NO_ELIGIBLE_OBSERVATION
        assert res.snapshot_hash == "hash_av_new"

    def test_06_alpha_vantage_compact_outside_range(self):
        """Test 6: Alpha Vantage compact snapshot whose range does not cover historical date leaves old snapshot authoritative."""
        t_hist = date(2024, 1, 15)
        t_snap_old = datetime(2024, 2, 1, 10, 0, tzinfo=timezone.utc)
        t_snap_recent = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)

        obs_hist = make_obs(AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_hist, Decimal("180.00"))
        snap_old = make_snapshot(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_snap_old, "hash_old",
            trade_date_range=(date(2024, 1, 1), date(2024, 1, 31)),
            observations=[obs_hist],
            endpoint="TIME_SERIES_DAILY",
        )

        obs_recent = make_obs(AAPL_ID, "ALPHA_VANTAGE", "AAPL", date(2026, 8, 26), Decimal("220.00"))
        snap_recent = make_snapshot(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_snap_recent, "hash_recent",
            trade_date_range=(date(2026, 5, 1), date(2026, 8, 27)),
            observations=[obs_recent],
            endpoint="TIME_SERIES_DAILY",
            output_size="compact",
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_hist, provider="ALPHA_VANTAGE")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_old, snap_recent])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("180.00")
        assert res.snapshot_hash == "hash_old"

    def test_07_different_instrument_isolation(self):
        """Test 7: Later SPY snapshot never supersedes AAPL snapshot."""
        t_target = date(2024, 6, 10)
        t_aapl = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_spy = datetime(2024, 6, 11, 12, 0, tzinfo=timezone.utc)

        snap_aapl = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_aapl, "hash_aapl",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )
        snap_spy = make_snapshot(
            SPY_ID, "TIINGO", "SPY", t_spy, "hash_spy",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(SPY_ID, "TIINGO", "SPY", t_target, Decimal("530.00"))]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_aapl, snap_spy])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.instrument_id == AAPL_ID
        assert res.selected_observation.close == Decimal("190.50")
        assert res.snapshot_hash == "hash_aapl"

    def test_08_different_provider_isolation(self):
        """Test 8: Later Alpha Vantage AAPL snapshot never supersedes query provider=TIINGO."""
        t_target = date(2024, 6, 10)
        t_tiingo = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_av = datetime(2024, 6, 11, 12, 0, tzinfo=timezone.utc)

        snap_tiingo = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_tiingo, "hash_tiingo",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )
        snap_av = make_snapshot(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_av, "hash_av",
            trade_date_range=(date(2024, 1, 1), date(2024, 6, 10)),
            observations=[make_obs(AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_target, Decimal("190.80"))]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_tiingo, snap_av])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.provider == "TIINGO"
        assert res.selected_observation.close == Decimal("190.50")
        assert res.snapshot_hash == "hash_tiingo"

    def test_09_failed_fetch_isolation(self):
        """Test 9: Failed HTTP 500 snapshot does not supersede earlier successful HTTP 200 snapshot."""
        t_target = date(2024, 6, 10)
        t_200 = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_500 = datetime(2024, 6, 11, 12, 0, tzinfo=timezone.utc)

        snap_ok = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_200, "hash_ok",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )
        snap_fail = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_500, "hash_fail",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            http_status=500,
            observations=[]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_ok, snap_fail])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("190.50")
        assert res.snapshot_hash == "hash_ok"

    def test_10_same_boundary_hash_conflict(self):
        """Test 10: Identical scope and retrieved_at with differing payload_hash flags SNAPSHOT_CONFLICT."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        snap_1 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_A",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )
        snap_2 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_B",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.60"))]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_1, snap_2])

        assert res.status == MarketDataResolutionStatus.SNAPSHOT_CONFLICT
        assert res.selected_observation is None

    def test_11_logical_duplicate_snapshot_deduplication(self):
        """Test 11: Different snapshot UUIDs with identical content deduplicate safely."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        snap_1 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_dup",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), payload_hash="hash_dup")]
        )
        snap_2 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_dup",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), payload_hash="hash_dup")]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res1 = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_1, snap_2])
        res2 = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_2, snap_1])

        assert res1.status == MarketDataResolutionStatus.SELECTED
        assert res2.status == MarketDataResolutionStatus.SELECTED
        assert res1.selected_observation.close == Decimal("190.50")
        assert res1.resolution_key == res2.resolution_key

    def test_12_observation_conflict_in_snapshot(self):
        """Test 12: Multiple differing observations for target date inside authoritative snapshot flag OBSERVATION_CONFLICT."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        # Same close price, different volume
        obs_1 = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), volume=Decimal("1000"))
        obs_2 = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), volume=Decimal("2000"))

        snap = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_obs_conflict",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[obs_1, obs_2]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.OBSERVATION_CONFLICT
        assert res.selected_observation is None

    def test_13_uuid_independent_resolution_key(self):
        """Test 13: Rebuilding identical economic snapshots with new UUIDs yields identical resolution_key."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        obs_a = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), adj_close=Decimal("190.50"), obs_id=uuid4())
        snap_a = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_now, "hash_same", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_a], snap_id=uuid4())

        obs_b = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), adj_close=Decimal("190.50"), obs_id=uuid4())
        snap_b = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_now, "hash_same", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_b], snap_id=uuid4())

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res_a = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_a])
        res_b = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_b])

        assert res_a.status == MarketDataResolutionStatus.SELECTED
        assert res_b.status == MarketDataResolutionStatus.SELECTED
        assert res_a.resolution_key is not None
        assert res_a.resolution_key == res_b.resolution_key

    def test_14_naive_timestamp_rejection(self):
        """Test 14: Naive retrieved_at on covering snapshot or naive as_of flags INVALID_TEMPORAL_LINEAGE."""
        t_target = date(2024, 6, 10)
        t_naive = datetime(2024, 6, 11, 10, 0)  # No tzinfo

        snap = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_naive, "hash_naive",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])
        assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

        # Naive as_of in SYSTEM_AS_OF
        t_valid_tz = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        snap_valid = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_valid_tz, "hash_valid",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )
        res_naive_as_of = PointInTimeMarketDataResolver.resolve_global_eod(
            query, [snap_valid], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=t_naive
        )
        assert res_naive_as_of.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    def test_15_source_as_of_unavailable(self):
        """Test 15: Requesting SOURCE_AS_OF returns UNAVAILABLE_SOURCE_AS_OF for all global providers."""
        t_target = date(2024, 6, 10)

        for prov, i_id in [("ALPHA_VANTAGE", AAPL_ID), ("TIINGO", AAPL_ID), ("MARKETSTACK", MBG_ID)]:
            query = GlobalEODQueryKey(instrument_id=i_id, trade_date=t_target, provider=prov)
            res = PointInTimeMarketDataResolver.resolve_global_eod(query, [], mode=MarketDataResolutionMode.SOURCE_AS_OF)
            assert res.status == MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF

    def test_16_adjusted_fields_preserved_without_arithmetic(self):
        """Test 16: Tiingo / Marketstack adj_close, div_cash, split_factor preserved intact."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        obs = make_obs(
            MBG_ID, "MARKETSTACK", "MBG.XETR", t_target,
            close=Decimal("64.90"),
            adj_close=Decimal("60.20"),
            div_cash=Decimal("5.20"),
            split_factor=Decimal("1.0"),
            currency=Currency.EUR,
            exchange="XETR",
            instrument_type=InstrumentType.EUROPEAN_STOCK,
        )
        snap = make_snapshot(
            MBG_ID, "MARKETSTACK", "MBG.XETR", t_now, "hash_ms",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[obs],
            endpoint="EOD",
        )

        query = GlobalEODQueryKey(instrument_id=MBG_ID, trade_date=t_target, provider="MARKETSTACK")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.SELECTED
        sel = res.selected_observation
        assert sel.close == Decimal("64.90")
        assert sel.adj_close == Decimal("60.20")
        assert sel.div_cash == Decimal("5.20")
        assert sel.split_factor == Decimal("1.0")

    def test_17_alpha_vantage_raw_only_selectable(self):
        """Test 17: Alpha Vantage observation with close present and adj_close None is valid and selectable."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        obs = make_obs(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_target,
            close=Decimal("190.50"),
            adj_close=None,
            div_cash=Decimal("0.0"),
            split_factor=Decimal("1.0"),
        )
        snap = make_snapshot(
            AAPL_ID, "ALPHA_VANTAGE", "AAPL", t_now, "hash_av_raw",
            trade_date_range=(date(2024, 1, 1), date(2024, 6, 10)),
            observations=[obs],
            endpoint="TIME_SERIES_DAILY",
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="ALPHA_VANTAGE")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("190.50")
        assert res.selected_observation.adj_close is None

    def test_18_input_order_independence(self):
        """Test 18: Reversing snapshot list and observation list produces identical result and resolution key."""
        t_target = date(2024, 6, 10)
        t_1 = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_2 = datetime(2024, 6, 11, 12, 0, tzinfo=timezone.utc)

        obs_1 = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.00"))
        snap_1 = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_1, "h1", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_1])

        obs_2 = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))
        snap_2 = make_snapshot(AAPL_ID, "TIINGO", "AAPL", t_2, "h2", start_date=date(2024, 1, 1), end_date=date(2024, 6, 10), observations=[obs_2])

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res_fwd = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_1, snap_2])
        res_rev = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_2, snap_1])

        assert res_fwd.status == MarketDataResolutionStatus.SELECTED
        assert res_rev.status == MarketDataResolutionStatus.SELECTED
        assert res_fwd.selected_observation.close == res_rev.selected_observation.close == Decimal("190.50")
        assert res_fwd.resolution_key == res_rev.resolution_key

    def test_19_evaluation_snapshot_ids_exposed(self):
        """Test 19: Exposes sorted physical IDs of eligible covering snapshots; excludes future under SYSTEM_AS_OF."""
        t_target = date(2024, 6, 10)
        t_1000 = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_1100 = datetime(2024, 6, 11, 11, 0, tzinfo=timezone.utc)

        id_1 = uuid4()
        id_2 = uuid4()

        snap_1 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_1000, "h1",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.00"))],
            snap_id=id_1
        )
        snap_2 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_1100, "h2",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))],
            snap_id=id_2
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")

        # CURRENT_REPORTED sees both snapshots
        res_curr = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_1, snap_2])
        assert sorted(res_curr.evaluation_snapshot_ids) == sorted([str(id_1), str(id_2)])

        # SYSTEM_AS_OF 10:30 only sees snapshot 1
        as_of_1030 = datetime(2024, 6, 11, 10, 30, tzinfo=timezone.utc)
        res_sys = PointInTimeMarketDataResolver.resolve_global_eod(
            query, [snap_1, snap_2], mode=MarketDataResolutionMode.SYSTEM_AS_OF, as_of=as_of_1030
        )
        assert res_sys.evaluation_snapshot_ids == [str(id_1)]

    def test_20_one_sided_start_does_not_overclaim(self):
        """Test 20: One-sided start_date without end_date does not overclaim future dates."""
        t_target = date(2027, 1, 5)
        t_now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)

        # Snapshot with start_date=2026-08-20, end_date=None, observations only through 2026-08-27
        obs = make_obs(AAPL_ID, "TIINGO", "AAPL", date(2026, 8, 27), Decimal("220.00"))
        snap = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_one_sided",
            start_date=date(2026, 8, 20), end_date=None,
            trade_date_range=(date(2026, 8, 20), None),
            observations=[obs]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.NO_SNAPSHOT
        assert res.selected_observation is None

    def test_21_one_sided_end_does_not_overclaim(self):
        """Test 21: One-sided end_date without start_date does not overclaim historical dates."""
        t_target = date(2020, 1, 2)
        t_now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)

        obs = make_obs(AAPL_ID, "TIINGO", "AAPL", date(2026, 8, 20), Decimal("220.00"))
        snap = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_one_sided_end",
            start_date=None, end_date=date(2026, 8, 20),
            trade_date_range=(None, date(2026, 8, 20)),
            observations=[obs]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.NO_SNAPSHOT
        assert res.selected_observation is None

    def test_22_one_sided_with_actual_target(self):
        """Test 22: One-sided bound with actual observation row on target is covering via observation presence."""
        t_target = date(2026, 8, 25)
        t_now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)

        obs = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("222.50"))
        snap = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "hash_has_target",
            start_date=date(2026, 8, 20), end_date=None,
            trade_date_range=(date(2026, 8, 20), None),
            observations=[obs]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("222.50")

    def test_23_failed_naive_snapshot_does_not_poison(self):
        """Test 23: Failed HTTP 500 snapshot with naive timestamp does not poison resolution of valid snapshot."""
        t_target = date(2024, 6, 10)
        t_valid = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_naive = datetime(2024, 6, 11, 11, 0)  # Naive timestamp on failed snapshot

        snap_valid = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_valid, "hash_valid",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )
        snap_failed = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_naive, "hash_failed",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            http_status=500,
            observations=[]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_valid, snap_failed])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("190.50")
        assert res.snapshot_hash == "hash_valid"

    def test_24_non_covering_naive_snapshot_does_not_poison(self):
        """Test 24: Successful naive snapshot that does not cover target date does not poison historical resolution."""
        t_hist = date(2024, 6, 10)
        t_valid = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        t_naive = datetime(2026, 8, 20, 10, 0)  # Naive timestamp on 2026 incremental snapshot

        snap_hist = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_valid, "hash_hist",
            start_date=date(2020, 1, 1), end_date=date(2024, 12, 31),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_hist, Decimal("190.50"))]
        )
        snap_2026 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_naive, "hash_2026",
            start_date=date(2026, 8, 19), end_date=date(2026, 8, 20),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", date(2026, 8, 19), Decimal("225.00"))]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_hist, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_hist, snap_2026])

        assert res.status == MarketDataResolutionStatus.SELECTED
        assert res.selected_observation.close == Decimal("190.50")
        assert res.snapshot_hash == "hash_hist"

    def test_25_covering_naive_snapshot_fails_closed(self):
        """Test 25: Successful covering snapshot with naive retrieved_at returns INVALID_TEMPORAL_LINEAGE."""
        t_target = date(2024, 6, 10)
        t_naive = datetime(2024, 6, 11, 10, 0)

        snap = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_naive, "hash_naive",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"))]
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap])

        assert res.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

    def test_26_scope_uuid_swap_resolution_key_invariant(self):
        """Test 26: Swapping physical UUIDs between different scopes at same retrieved_at yields identical resolution key."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)

        uuid_1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        uuid_2 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        # Scope 1 (full year)
        obs_1a = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), payload_hash="h1")
        # Scope 2 (single day)
        obs_2a = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), payload_hash="h2")

        # Run 1: Scope 1 gets uuid_1, Scope 2 gets uuid_2
        snap_s1_u1 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h1",
            start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
            observations=[obs_1a], snap_id=uuid_1
        )
        snap_s2_u2 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h2",
            start_date=date(2024, 6, 10), end_date=date(2024, 6, 10),
            observations=[obs_2a], snap_id=uuid_2
        )

        obs_1b = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), payload_hash="h1")
        obs_2b = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), payload_hash="h2")

        # Run 2: Scope 1 gets uuid_2, Scope 2 gets uuid_1
        snap_s1_u2 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h1",
            start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
            observations=[obs_1b], snap_id=uuid_2
        )
        snap_s2_u1 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h2",
            start_date=date(2024, 6, 10), end_date=date(2024, 6, 10),
            observations=[obs_2b], snap_id=uuid_1
        )

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res_run1 = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_s1_u1, snap_s2_u2])
        res_run2 = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_s1_u2, snap_s2_u1])

        assert res_run1.status == MarketDataResolutionStatus.SELECTED
        assert res_run2.status == MarketDataResolutionStatus.SELECTED
        assert res_run1.selected_observation.close == res_run2.selected_observation.close == Decimal("190.50")
        assert res_run1.resolution_key == res_run2.resolution_key

    def test_27_lineage_classification_status(self):
        """Test 27: Mismatched snapshot_id, payload_hash, or provider on observation returns INVALID_TEMPORAL_LINEAGE."""
        t_target = date(2024, 6, 10)
        t_now = datetime(2024, 6, 11, 10, 0, tzinfo=timezone.utc)
        snap_id = uuid4()

        # 1. Wrong snapshot_id
        obs_wrong_sid = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), snapshot_id=uuid4(), payload_hash="h1")
        snap_1 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h1",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[obs_wrong_sid], snap_id=snap_id
        )
        obs_wrong_sid.snapshot_id = uuid4()

        query = GlobalEODQueryKey(instrument_id=AAPL_ID, trade_date=t_target, provider="TIINGO")
        res_sid = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_1])
        assert res_sid.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

        # 2. Wrong payload_hash
        obs_wrong_hash = make_obs(AAPL_ID, "TIINGO", "AAPL", t_target, Decimal("190.50"), snapshot_id=snap_id, payload_hash="corrupt_hash")
        snap_2 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h1",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[obs_wrong_hash], snap_id=snap_id
        )
        obs_wrong_hash.payload_hash = "corrupt_hash"
        res_hash = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_2])
        assert res_hash.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE

        # 3. Wrong provider
        obs_wrong_prov = make_obs(AAPL_ID, "MARKETSTACK", "AAPL", t_target, Decimal("190.50"), snapshot_id=snap_id, payload_hash="h1")
        snap_3 = make_snapshot(
            AAPL_ID, "TIINGO", "AAPL", t_now, "h1",
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 10),
            observations=[obs_wrong_prov], snap_id=snap_id
        )
        obs_wrong_prov.provider = "MARKETSTACK"
        res_prov = PointInTimeMarketDataResolver.resolve_global_eod(query, [snap_3])
        assert res_prov.status == MarketDataResolutionStatus.INVALID_TEMPORAL_LINEAGE
