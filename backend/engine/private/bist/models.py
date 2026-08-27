"""
backend/engine/private/bist/models.py
=====================================
Data models for Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 market backbone.

Principles:
    - Strict Decimal arithmetic for all monetary values and volumes. Zero floats.
    - Missing fields remain None (missing != zero).
    - Malformed/missing close prices NEVER become Decimal("0").
    - Raw source symbol (raw_provider_symbol) preserved alongside normalized symbol.
    - Clear point-in-time separation: trade_date (effective date) vs retrieved_at (network UTC).
    - Preserves raw snapshot before normalization.
    - Seamless serialization to generic PIT storage models (RawProviderSnapshotRecord, NormalizedObservationRecord).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
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
)


class BISTCapability(Enum):
    """BIST provider access capabilities."""
    CURRENT_DAILY_PUBLIC = "current_daily_public"
    HISTORICAL_PUBLIC_IF_AVAILABLE = "historical_public_if_available"
    HISTORICAL_DATASTORE_RESTRICTED = "historical_datastore_restricted"


class BISTMarketSegment(Enum):
    """Recognized BIST market segments."""
    YILDIZ_PAZAR = "YILDIZ PAZAR"
    ANA_PAZAR = "ANA PAZAR"
    ALT_PAZAR = "ALT PAZAR"
    YAKIN_IZLEME = "YAKIN IZLEME PAZARI"
    PIYASA_ONCESI = "PIYASA ONCESI ISLEM PLATFORMU"
    EMTIA_PAZARI = "EMTIA SERTIFIKALARI"
    FON_PAZARI = "BORSA YATIRIM FONLARI"
    OTHER = "OTHER"


class BISTObservationStatus(Enum):
    """Integrity and resolution status of an individual BIST observation."""
    VALID = "valid"
    INVALID_OBSERVATION = "invalid_observation"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    SCHEMA_DRIFT = "schema_drift"
    CONFLICT_QUARANTINED = "conflict_quarantined"


@dataclass
class BISTEODObservation:
    """
    Normalized End-of-Day (EOD) observation for a BIST instrument.
    """
    symbol: str
    trade_date: date
    close: Optional[Decimal] = None
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    weighted_average: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    turnover: Optional[Decimal] = None
    trade_count: Optional[int] = None
    currency: Currency = Currency.TRY
    market_segment: Optional[str] = None
    instrument_name: Optional[str] = None
    raw_provider_symbol: Optional[str] = None
    instrument_id: Optional[UUID] = None
    asset_class: Optional[AssetClass] = None
    instrument_type: Optional[InstrumentType] = None
    status: BISTObservationStatus = BISTObservationStatus.VALID
    source_provider: str = "BIST_EOD"
    snapshot_id: Optional[UUID] = None
    snapshot_hash: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    source_as_of: Optional[datetime] = None
    confidence_level: DataConfidenceLevel = DataConfidenceLevel.HIGH
    source_tier: SourceTier = SourceTier.TIER_2_EXCHANGE
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "symbol": self.symbol,
            "raw_provider_symbol": self.raw_provider_symbol or self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "open": str(self.open) if self.open is not None else None,
            "high": str(self.high) if self.high is not None else None,
            "low": str(self.low) if self.low is not None else None,
            "close": str(self.close) if self.close is not None else None,
            "previous_close": str(self.previous_close) if self.previous_close is not None else None,
            "weighted_average": str(self.weighted_average) if self.weighted_average is not None else None,
            "volume": str(self.volume) if self.volume is not None else None,
            "turnover": str(self.turnover) if self.turnover is not None else None,
            "trade_count": self.trade_count,
            "currency": self.currency.value,
            "market_segment": self.market_segment,
            "instrument_name": self.instrument_name,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "asset_class": self.asset_class.value if self.asset_class else None,
            "instrument_type": self.instrument_type.value if self.instrument_type else None,
            "status": self.status.value,
            "source_provider": self.source_provider,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "snapshot_hash": self.snapshot_hash,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "source_as_of": self.source_as_of.isoformat() if self.source_as_of else None,
            "confidence_level": self.confidence_level.value,
            "source_tier": self.source_tier.value,
            "diagnostics": self.diagnostics,
        }

    def to_normalized_observation_record(self) -> NormalizedObservationRecord:
        """
        Converts to generic Sentinax NormalizedObservationRecord.
        """
        obs_data = {
            "symbol": self.symbol,
            "raw_provider_symbol": self.raw_provider_symbol or self.symbol,
            "close": str(self.close) if self.close is not None else None,
            "open": str(self.open) if self.open is not None else None,
            "high": str(self.high) if self.high is not None else None,
            "low": str(self.low) if self.low is not None else None,
            "previous_close": str(self.previous_close) if self.previous_close is not None else None,
            "weighted_average": str(self.weighted_average) if self.weighted_average is not None else None,
            "volume": str(self.volume) if self.volume is not None else None,
            "turnover": str(self.turnover) if self.turnover is not None else None,
            "trade_count": self.trade_count,
            "market_segment": self.market_segment,
            "instrument_name": self.instrument_name,
        }

        # Map internal status to generic DataStatus
        if self.status == BISTObservationStatus.VALID:
            data_status = DataStatus.COMPLETE
        elif self.status == BISTObservationStatus.UNRESOLVED_IDENTITY:
            data_status = DataStatus.PARTIAL
        else:
            data_status = DataStatus.DEGRADED

        source_ref = f"{self.source_provider}:{self.raw_provider_symbol or self.symbol}@{self.trade_date.isoformat()}"

        return NormalizedObservationRecord(
            id=self.id,
            snapshot_id=self.snapshot_id,
            instrument_id=self.instrument_id,
            asset_class=self.asset_class,
            instrument_type=self.instrument_type,
            observation_type="BIST_EOD_PRICE",
            observation_data=obs_data,
            data_status=data_status,
            confidence_level=self.confidence_level,
            source_tier=self.source_tier,
            effective_date=self.trade_date,
            observed_at=self.retrieved_at or datetime.now(timezone.utc),
            currency=self.currency,
            published_at=self.source_as_of,
            ingested_at=datetime.now(timezone.utc),
            warnings=self.diagnostics,
            source_refs=[source_ref],
        )


@dataclass
class BISTBulletinSnapshot:
    """
    Representation of an immutable raw BIST daily bulletin snapshot.
    """
    trade_date: date
    retrieved_at: datetime
    http_status: int
    payload_hash: str
    content_type: str
    file_name: Optional[str] = None
    source_url: str = ""
    landing_page_url: Optional[str] = None
    resolved_download_url: Optional[str] = None
    requested_trade_date: Optional[date] = None
    filename_trade_date: Optional[date] = None
    manifest_hash: Optional[str] = None
    is_stale_discovery: bool = False
    observations: List[BISTEODObservation] = field(default_factory=list)
    raw_bytes: Optional[bytes] = None
    parser_version: str = "1.2.0"
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "trade_date": self.trade_date.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "http_status": self.http_status,
            "payload_hash": self.payload_hash,
            "content_type": self.content_type,
            "file_name": self.file_name,
            "source_url": self.source_url,
            "landing_page_url": self.landing_page_url,
            "resolved_download_url": self.resolved_download_url,
            "requested_trade_date": self.requested_trade_date.isoformat() if self.requested_trade_date else None,
            "filename_trade_date": self.filename_trade_date.isoformat() if self.filename_trade_date else None,
            "manifest_hash": self.manifest_hash,
            "is_stale_discovery": self.is_stale_discovery,
            "observation_count": len(self.observations),
            "parser_version": self.parser_version,
            "diagnostics": self.diagnostics,
        }

    def to_raw_snapshot_record(self) -> RawProviderSnapshotRecord:
        """
        Converts to generic Sentinax RawProviderSnapshotRecord.
        """
        payload_content: Any
        if self.raw_bytes is not None:
            try:
                payload_content = self.raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                payload_content = f"binary_blob_len_{len(self.raw_bytes)}"
        else:
            payload_content = [obs.to_dict() for obs in self.observations]

        return RawProviderSnapshotRecord(
            id=self.id,
            provider="BIST_EOD",
            endpoint=self.resolved_download_url or self.source_url or "https://www.borsaistanbul.com/files/DataFilePaths.zip",
            request_params={
                "trade_date": self.trade_date.isoformat(),
                "market": "EQUITY",
                "landing_page_url": self.landing_page_url,
                "resolved_download_url": self.resolved_download_url,
                "file_name": self.file_name,
                "manifest_hash": self.manifest_hash,
                "is_stale_discovery": self.is_stale_discovery,
            },
            retrieved_at=self.retrieved_at,
            http_status=self.http_status,
            response_metadata={
                "file_name": self.file_name,
                "observation_count": len(self.observations),
                "landing_page_url": self.landing_page_url,
                "resolved_download_url": self.resolved_download_url,
                "manifest_hash": self.manifest_hash,
                "is_stale_discovery": self.is_stale_discovery,
            },
            content_type=self.content_type,
            raw_payload=payload_content,
            payload_hash=self.payload_hash,
            parser_version=self.parser_version,
            license_profile="PROPRIETARY_EXCHANGE_BULLETIN",
        )
