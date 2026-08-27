"""
backend/engine/private/market_data/tefas_models.py
==================================================
Canonical data models for TEFAS Turkish Investment Fund EOD Price Ingestion.

Design Principles:
    - Pure Decimal for all unit prices (zero floats).
    - Missing fields remain None (missing != zero).
    - Rejects non-finite values (NaN, sNaN, Infinity, -Infinity) and zero/negative prices.
    - Preserves canonical instrument_type (TEFAS_FUND, TEFAS_MONEY_MARKET, TEFAS_EQUITY, TEFAS_VARIABLE, TEFAS_BALANCED).
    - Strict Point-in-Time (PIT) semantics: trade_date is the economic date, retrieved_at is network UTC.
    - published_at is None (TEFAS EOD does not supply micro-timestamps; no fabrication).
    - Source metadata is current-view only (CURRENT_METADATA_ONLY); not look-ahead PIT authority.
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


class TefasObservationStatus(Enum):
    """Integrity and resolution status for a TEFAS fund observation."""
    VALID = "valid"
    INVALID_OBSERVATION = "invalid_observation"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    RATE_LIMITED = "rate_limited"
    INVALID_SOURCE_CONTEXT = "invalid_source_context"


class TefasCapability(Enum):
    """Capability indicators for TEFAS data access."""
    PUBLIC_LOW_FREQUENCY = "public_low_frequency"
    FUND_PRICE_HISTORY = "fund_price_history"
    ROLLING_5Y_HISTORY = "rolling_5y_history"


@dataclass
class TefasFundPriceObservation:
    """
    Normalized Point-in-Time fund price observation for a Turkish TEFAS investment fund.
    Represents the published fund unit price for a given trade date.
    """
    provider_symbol: str
    trade_date: date
    unit_price: Optional[Decimal]
    currency: Optional[Currency] = None
    instrument_id: Optional[UUID] = None
    instrument_type: Optional[InstrumentType] = None
    provider: str = "TEFAS"
    snapshot_id: Optional[UUID] = None
    payload_hash: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    status: TefasObservationStatus = TefasObservationStatus.VALID
    confidence_level: DataConfidenceLevel = DataConfidenceLevel.MEDIUM
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    @property
    def is_valid(self) -> bool:
        return (
            self.status == TefasObservationStatus.VALID
            and self.unit_price is not None
            and self.unit_price.is_finite()
            and self.unit_price > Decimal("0")
        )

    def to_normalized_observation_record(self) -> NormalizedObservationRecord:
        """Converts to canonical PIT storage model."""
        data_status = (
            DataStatus.COMPLETE if self.is_valid else DataStatus.UNAVAILABLE
        )
        obs_data: Dict[str, Any] = {
            "provider_symbol": self.provider_symbol,
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
        }

        return NormalizedObservationRecord(
            id=self.id,
            snapshot_id=self.snapshot_id,
            instrument_id=self.instrument_id,
            asset_class=AssetClass.FUND,
            instrument_type=self.instrument_type,
            observation_type="TEFAS_FUND_PRICE",
            observation_data=obs_data,
            data_status=data_status,
            confidence_level=self.confidence_level,
            source_tier=SourceTier.TIER_2_EXCHANGE,
            effective_date=self.trade_date,
            observed_at=self.retrieved_at,
            currency=self.currency,
            published_at=self.published_at,
            warnings=list(self.diagnostics),
            source_refs=[f"{self.provider}:{self.provider_symbol}@{self.trade_date.isoformat()}"],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes observation to dict with pure string Decimals and ISO dates."""
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "provider_symbol": self.provider_symbol,
            "trade_date": self.trade_date.isoformat(),
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "currency": self.currency.value if self.currency else None,
            "instrument_type": self.instrument_type.value if self.instrument_type else None,
            "provider": self.provider,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "payload_hash": self.payload_hash,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "status": self.status.value,
            "confidence_level": self.confidence_level.value,
            "diagnostics": list(self.diagnostics),
        }


@dataclass
class TefasFundPriceSnapshot:
    """
    Immutable raw provider response snapshot for a TEFAS fund price history request.
    """
    provider: str = "TEFAS"
    provider_symbol: str = ""
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    http_status: int = 200
    payload_hash: str = ""
    raw_payload: str = ""
    instrument_id: Optional[UUID] = None
    period_months: int = 1
    endpoint: str = "FUND_PRICE_HISTORY"
    parser_version: str = "1.0.0"
    is_rate_limited: bool = False
    source_row_count: int = 0
    malformed_row_count: int = 0
    trade_date_range: Tuple[Optional[date], Optional[date]] = (None, None)
    observations: List[TefasFundPriceObservation] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def to_raw_provider_snapshot_record(self) -> RawProviderSnapshotRecord:
        """Converts to canonical PIT raw snapshot record."""
        req_params: Dict[str, Any] = {
            "fonKodu": self.provider_symbol,
            "dil": "TR",
            "periyod": self.period_months,
        }

        return RawProviderSnapshotRecord(
            id=self.id,
            provider=self.provider,
            endpoint=self.endpoint,
            request_params=req_params,
            retrieved_at=self.retrieved_at,
            http_status=self.http_status,
            content_type="application/json",
            raw_payload=self.raw_payload,
            payload_hash=self.payload_hash,
            response_metadata={
                "is_rate_limited": self.is_rate_limited,
                "source_row_count": self.source_row_count,
                "malformed_row_count": self.malformed_row_count,
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
            "period_months": self.period_months,
            "endpoint": self.endpoint,
            "parser_version": self.parser_version,
            "is_rate_limited": self.is_rate_limited,
            "source_row_count": self.source_row_count,
            "malformed_row_count": self.malformed_row_count,
            "trade_date_range": [
                self.trade_date_range[0].isoformat() if self.trade_date_range[0] else None,
                self.trade_date_range[1].isoformat() if self.trade_date_range[1] else None,
            ],
            "observation_count": len(self.observations),
            "diagnostics": list(self.diagnostics),
        }
