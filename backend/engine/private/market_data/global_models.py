"""
backend/engine/private/market_data/global_models.py
==================================================
Canonical data models for Global (US & European) EOD Market Data Ingestion.

Core Invariants:
    - Pure Decimal for all financial prices and volumes. Zero floats.
    - Missing fields remain None. Missing != zero.
    - Rejects non-finite values (NaN, sNaN, Infinity, -Infinity).
    - Preserves canonical instrument_type (US_STOCK, EUROPEAN_STOCK, US_ETF, EUROPEAN_ETF).
    - Strict Point-in-Time (PIT) semantics: trade_date is the economic date, retrieved_at is network UTC.
    - published_at / source_as_of is None unless explicitly supplied by source (no fabrication).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    SourceTier,
)
from backend.engine.private.storage_models import (
    NormalizedObservationRecord,
    RawProviderSnapshotRecord,
    compute_payload_hash,
)


class GlobalObservationStatus(Enum):
    """Integrity and resolution status for a global EOD observation."""
    VALID = "valid"
    INVALID_OBSERVATION = "invalid_observation"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    RATE_LIMITED = "rate_limited"


class AlphaVantageCapability(Enum):
    """Capability indicators for Alpha Vantage data access."""
    LOW_VOLUME = "low_volume"
    PER_SYMBOL_REQUEST = "per_symbol_request"
    FREE_DAILY_LIMIT_CONSTRAINED = "free_daily_limit_constrained"
    FREE_COMPACT_HISTORY = "free_compact_history"


@dataclass
class GlobalEODObservation:
    """
    Normalized Point-in-Time EOD market observation for a US or European equity/ETF.
    """
    provider_symbol: str
    trade_date: date
    close: Optional[Decimal]
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    currency: Optional[Currency] = None
    exchange: Optional[str] = None
    instrument_id: Optional[UUID] = None
    instrument_type: Optional[InstrumentType] = None
    provider: str = "ALPHA_VANTAGE"
    snapshot_id: Optional[UUID] = None
    payload_hash: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    status: GlobalObservationStatus = GlobalObservationStatus.VALID
    confidence_level: DataConfidenceLevel = DataConfidenceLevel.MEDIUM
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    @property
    def is_valid(self) -> bool:
        return self.status == GlobalObservationStatus.VALID and self.close is not None and self.close.is_finite()

    def to_normalized_observation_record(self) -> NormalizedObservationRecord:
        """Converts to canonical PIT storage model."""
        asset_class = (
            AssetClass.ETF
            if self.instrument_type in (InstrumentType.US_ETF, InstrumentType.EUROPEAN_ETF)
            else AssetClass.EQUITY
        )
        data_status = (
            DataStatus.COMPLETE if self.status == GlobalObservationStatus.VALID else DataStatus.UNAVAILABLE
        )
        return NormalizedObservationRecord(
            id=self.id,
            snapshot_id=self.snapshot_id,
            instrument_id=self.instrument_id,
            asset_class=asset_class,
            instrument_type=self.instrument_type,
            observation_type="GLOBAL_EOD_PRICE",
            observation_data={
                "provider_symbol": self.provider_symbol,
                "exchange": self.exchange,
                "open": str(self.open) if self.open is not None else None,
                "high": str(self.high) if self.high is not None else None,
                "low": str(self.low) if self.low is not None else None,
                "close": str(self.close) if self.close is not None else None,
                "volume": str(self.volume) if self.volume is not None else None,
            },
            data_status=data_status,
            confidence_level=self.confidence_level,
            source_tier=SourceTier.TIER_3_AGGREGATOR,
            effective_date=self.trade_date,
            observed_at=self.retrieved_at,
            currency=self.currency,
            published_at=self.published_at,
            warnings=list(self.diagnostics),
            source_refs=[f"{self.provider}:{self.provider_symbol}@{self.trade_date.isoformat()}"],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes observation to dict with pure string Decimals and ISO timestamps."""
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "provider_symbol": self.provider_symbol,
            "exchange": self.exchange,
            "trade_date": self.trade_date.isoformat(),
            "open": str(self.open) if self.open is not None else None,
            "high": str(self.high) if self.high is not None else None,
            "low": str(self.low) if self.low is not None else None,
            "close": str(self.close) if self.close is not None else None,
            "volume": str(self.volume) if self.volume is not None else None,
            "currency": self.currency.value if self.currency else None,
            "instrument_type": self.instrument_type.value if self.instrument_type else None,
            "provider": self.provider,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "payload_hash": self.payload_hash,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "status": self.status.value,
            "confidence_level": self.confidence_level.value,
            "diagnostics": self.diagnostics,
        }


@dataclass
class GlobalEODSnapshot:
    """
    Immutable raw provider response snapshot representing a per-instrument EOD time series request.
    """
    provider: str
    provider_symbol: str
    retrieved_at: datetime
    http_status: int
    payload_hash: str
    raw_payload: str
    output_size: str = "compact"
    is_rate_limited: bool = False
    trade_date_range: Tuple[Optional[date], Optional[date]] = (None, None)
    observations: List[GlobalEODObservation] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def to_raw_provider_snapshot_record(self) -> RawProviderSnapshotRecord:
        """Converts to canonical PIT raw snapshot record."""
        return RawProviderSnapshotRecord(
            id=self.id,
            provider=self.provider,
            endpoint="TIME_SERIES_DAILY",
            request_params={
                "symbol": self.provider_symbol,
                "outputsize": self.output_size,
            },
            retrieved_at=self.retrieved_at,
            http_status=self.http_status,
            content_type="application/json",
            raw_payload=self.raw_payload,
            payload_hash=self.payload_hash,
            response_metadata={
                "is_rate_limited": self.is_rate_limited,
                "observation_count": len(self.observations),
                "trade_date_range": [
                    self.trade_date_range[0].isoformat() if self.trade_date_range[0] else None,
                    self.trade_date_range[1].isoformat() if self.trade_date_range[1] else None,
                ],
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "provider_symbol": self.provider_symbol,
            "retrieved_at": self.retrieved_at.isoformat(),
            "http_status": self.http_status,
            "payload_hash": self.payload_hash,
            "output_size": self.output_size,
            "is_rate_limited": self.is_rate_limited,
            "trade_date_range": [
                self.trade_date_range[0].isoformat() if self.trade_date_range[0] else None,
                self.trade_date_range[1].isoformat() if self.trade_date_range[1] else None,
            ],
            "observation_count": len(self.observations),
            "diagnostics": self.diagnostics,
        }
