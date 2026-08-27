"""
backend/engine/private/precious_metals/models.py
================================================
Data models for Precious Metals (Gold / Silver) Market Observations and Discovery.
"""

from __future__ import annotations

import hashlib
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
from backend.engine.private.precious_metals.constants import (
    PreciousMetalMarket,
    PreciousMetalPriceType,
    PreciousMetalType,
    PreciousMetalUnit,
)
from backend.engine.private.storage_models import (
    NormalizedObservationRecord,
    RawProviderSnapshotRecord,
)


class PreciousMetalObservationStatus(Enum):
    """Integrity and validity status of a precious metal market observation."""
    VALID = "VALID"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"      # Missing or malformed required fields (e.g. invalid price)
    UNSUPPORTED_METAL = "UNSUPPORTED_METAL"          # Metal outside Gold/Silver scope (e.g. Platinum, Palladium)
    CONFLICT_QUARANTINED = "CONFLICT_QUARANTINED"    # Conflicting duplicate observations quarantined
    SCHEMA_DRIFT = "SCHEMA_DRIFT"                    # Required schema elements missing


class ComparabilityStatus(Enum):
    """Semantic comparability result between two precious metal market observations."""
    CONSISTENT = "CONSISTENT"          # Matching semantic dimensions and identical price
    DIVERGENT = "DIVERGENT"            # Matching semantic dimensions but differing price
    NOT_COMPARABLE = "NOT_COMPARABLE"  # Incompatible semantic dimensions (different currency, unit, purity, etc.)


@dataclass(frozen=True)
class ComparabilityResult:
    """Detailed result of a cross-source semantic comparability evaluation."""
    status: ComparabilityStatus
    is_comparable: bool
    price_a: Optional[Decimal]
    price_b: Optional[Decimal]
    difference: Optional[Decimal]
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "is_comparable": self.is_comparable,
            "price_a": str(self.price_a) if self.price_a is not None else None,
            "price_b": str(self.price_b) if self.price_b is not None else None,
            "difference": str(self.difference) if self.difference is not None else None,
            "reasons": self.reasons,
        }


class SeriesVerificationStatus(Enum):
    """Verification status of series contracts against official metadata evidence."""
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    DEPRECATED = "deprecated"


@dataclass
class PreciousMetalMarketObservation:
    """
    Dimensioned point-in-time precious metal market reference observation.
    Never a generic 'gold_price'; fully dimensioned with metal, currency, unit, purity, and price type.
    """
    metal: PreciousMetalType
    market: PreciousMetalMarket
    effective_date: date

    price: Optional[Decimal]
    price_currency: Currency
    quantity_unit: PreciousMetalUnit
    price_type: PreciousMetalPriceType

    price_quantity: Decimal = Decimal("1")
    purity: Optional[Decimal] = None                 # Deprecated / legacy alias for fineness or raw purity
    raw_purity_value: Optional[Decimal] = None       # e.g. Decimal("995"), Decimal("99.9")
    raw_purity_text: Optional[str] = None           # e.g. "995", "99.9", "99.90"
    purity_scale: Optional[str] = None              # "PER_MILLE", "PERCENT", or "UNKNOWN"
    fineness_per_mille: Optional[Decimal] = None    # Canonical per-mille fineness if unambiguously verified

    raw_value_date_text: Optional[str] = None       # Raw settlement/value-date string from source (e.g. "2608", "T+0")
    value_date: Optional[date] = None               # Explicit settlement/value date
    settlement_term: Optional[str] = None           # Explicit settlement term (e.g. "T+0", "T+1", None if unknown)

    volume: Optional[Decimal] = None                # Physical transaction quantity in quantity_unit or bars
    turnover: Optional[Decimal] = None              # Monetary turnover value in price_currency
    trade_count: Optional[int] = None               # Number of executed trades

    provider: str = "BIST_KMTP"
    originating_source: str = "BIST"
    raw_symbol: Optional[str] = None                # Provider-specific series symbol (e.g. 'AU_TL_S_995.0_BIM_1K_2608')

    snapshot_id: Optional[UUID] = None
    payload_hash: Optional[str] = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: Optional[datetime] = None

    status: PreciousMetalObservationStatus = PreciousMetalObservationStatus.VALID
    confidence: DataConfidenceLevel = DataConfidenceLevel.HIGH
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "metal": self.metal.value,
            "market": self.market.value,
            "effective_date": self.effective_date.isoformat(),
            "price": str(self.price) if self.price is not None else None,
            "price_currency": self.price_currency.value,
            "price_quantity": str(self.price_quantity),
            "quantity_unit": self.quantity_unit.value,
            "price_type": self.price_type.value,
            "purity": str(self.purity) if self.purity is not None else None,
            "raw_purity_value": str(self.raw_purity_value) if self.raw_purity_value is not None else None,
            "raw_purity_text": self.raw_purity_text,
            "purity_scale": self.purity_scale,
            "fineness_per_mille": str(self.fineness_per_mille) if self.fineness_per_mille is not None else None,
            "raw_value_date_text": self.raw_value_date_text,
            "value_date": self.value_date.isoformat() if self.value_date else None,
            "settlement_term": self.settlement_term,
            "volume": str(self.volume) if self.volume is not None else None,
            "turnover": str(self.turnover) if self.turnover is not None else None,
            "trade_count": self.trade_count,
            "provider": self.provider,
            "originating_source": self.originating_source,
            "raw_symbol": self.raw_symbol,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "payload_hash": self.payload_hash,
            "retrieved_at": self.retrieved_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "status": self.status.value,
            "confidence": self.confidence.value,
            "diagnostics": self.diagnostics,
        }

    def to_normalized_observation_record(self) -> NormalizedObservationRecord:
        """
        Converts to generic Sentinax NormalizedObservationRecord.
        Preserves snapshot_id strictly from raw snapshot (no fake UUID generation).
        """
        obs_status: DataStatus
        if self.status == PreciousMetalObservationStatus.VALID:
            obs_status = DataStatus.COMPLETE
        elif self.status == PreciousMetalObservationStatus.UNSUPPORTED_METAL:
            obs_status = DataStatus.UNAVAILABLE
        else:
            obs_status = DataStatus.DEGRADED

        inst_type = InstrumentType.GOLD if self.metal == PreciousMetalType.GOLD else InstrumentType.SILVER

        obs_data: Dict[str, Any] = {
            "value": str(self.price) if self.price is not None else None,
            "metal": self.metal.value,
            "market": self.market.value,
            "quantity_unit": self.quantity_unit.value,
            "price_quantity": str(self.price_quantity),
            "price_type": self.price_type.value,
            "purity": str(self.purity) if self.purity is not None else None,
            "raw_purity_value": str(self.raw_purity_value) if self.raw_purity_value is not None else None,
            "raw_purity_text": self.raw_purity_text,
            "purity_scale": self.purity_scale,
            "fineness_per_mille": str(self.fineness_per_mille) if self.fineness_per_mille is not None else None,
            "raw_value_date_text": self.raw_value_date_text,
            "value_date": self.value_date.isoformat() if self.value_date else None,
            "settlement_term": self.settlement_term,
            "provider": self.provider,
            "originating_source": self.originating_source,
            "raw_symbol": self.raw_symbol,
            "diagnostics": self.diagnostics,
        }

        source_tier = SourceTier.TIER_2_EXCHANGE if self.provider == "BIST_KMTP" else SourceTier.TIER_1_REGULATORY

        return NormalizedObservationRecord(
            id=self.id,
            snapshot_id=self.snapshot_id,  # Strictly preserve lineage; None if no snapshot
            instrument_id=None,  # Market reference rate; not bound to a single client portfolio instrument
            asset_class=AssetClass.COMMODITY,
            instrument_type=inst_type,
            observation_type="PRECIOUS_METAL_MARKET_REFERENCE",
            observation_data=obs_data,
            data_status=obs_status,
            confidence_level=self.confidence,
            source_tier=source_tier,
            effective_date=self.effective_date,
            observed_at=self.retrieved_at,
            currency=self.price_currency,
            published_at=self.published_at,
        )


@dataclass(frozen=True)
class PreciousMetalSeriesDefinition:
    """
    Registry metadata contract for a precious metal market reference series.
    """
    series_code: str
    canonical_name: str
    metal: PreciousMetalType
    provider: str
    originating_source: str
    frequency: str
    value_unit: str
    currency: Currency
    quantity_unit: PreciousMetalUnit
    price_type: PreciousMetalPriceType
    purity: Optional[Decimal] = None
    purity_scale: Optional[str] = None
    fineness_per_mille: Optional[Decimal] = None
    settlement_term: Optional[str] = None
    verification_status: SeriesVerificationStatus = SeriesVerificationStatus.UNVERIFIED
    verification_notes: str = ""
    verified_metadata_hash: Optional[str] = None
    notes: str = ""
    is_active: bool = True
    verified_at: Optional[date] = None
    source_catalog_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "series_code": self.series_code,
            "canonical_name": self.canonical_name,
            "metal": self.metal.value,
            "provider": self.provider,
            "originating_source": self.originating_source,
            "frequency": self.frequency,
            "value_unit": self.value_unit,
            "currency": self.currency.value,
            "quantity_unit": self.quantity_unit.value,
            "price_type": self.price_type.value,
            "purity": str(self.purity) if self.purity is not None else None,
            "purity_scale": self.purity_scale,
            "fineness_per_mille": str(self.fineness_per_mille) if self.fineness_per_mille is not None else None,
            "settlement_term": self.settlement_term,
            "verification_status": self.verification_status.value,
            "verification_notes": self.verification_notes,
            "verified_metadata_hash": self.verified_metadata_hash,
            "notes": self.notes,
            "is_active": self.is_active,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "source_catalog_url": self.source_catalog_url,
        }


@dataclass
class PreciousMetalSnapshot:
    """
    Immutable raw snapshot container for BIST KMTP or TCMB EVDS precious metal payloads.
    """
    trade_date: date
    retrieved_at: datetime
    http_status: int
    payload_hash: str
    content_type: str
    file_name: Optional[str] = None
    source_url: str = ""
    resolved_download_url: Optional[str] = None
    manifest_hash: Optional[str] = None
    is_stale_discovery: bool = False
    observations: List[PreciousMetalMarketObservation] = field(default_factory=list)
    raw_bytes: Optional[bytes] = None
    parser_version: str = "1.0.0"
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
            "resolved_download_url": self.resolved_download_url,
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
            provider="BIST_KMTP",
            endpoint=self.resolved_download_url or self.source_url or "https://www.borsaistanbul.com/files/DataFilePaths.zip",
            request_params={
                "trade_date": self.trade_date.isoformat(),
                "market": "PRECIOUS_METALS",
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
