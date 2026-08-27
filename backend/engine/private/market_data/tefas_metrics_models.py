"""
backend/engine/private/market_data/tefas_metrics_models.py
==========================================================
Canonical data models for TEFAS Turkish Investment Fund Current Valuation and Metrics Ingestion.

Design Principles:
    - Pure Decimal for portfolio_size, outstanding_units, and reported_current_unit_price (zero floats).
    - investor_count is a non-negative integer.
    - Missing fields remain None (missing != zero).
    - Negative values are invalid for portfolio_size, outstanding_units, and investor_count.
    - Zero values (0) are valid non-negative states.
    - Strict Point-in-Time (PIT) semantics: retrieved_at is network UTC; published_at and effective_date are strictly None.
    - Currency is Currency.TRY for accepted canonical observations.
    - reported_current_unit_price is DIAGNOSTIC CROSS-CHECK ONLY; does not control valuation or COMPLETE/PARTIAL status.
    - Category / rank / market share remain raw snapshot context only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
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
from backend.engine.private.market_data.tefas_models import (
    TefasCapability,
    TefasObservationStatus,
)
from backend.engine.private.storage_models import (
    NormalizedObservationRecord,
    RawProviderSnapshotRecord,
    compute_payload_hash,
)


@dataclass
class TefasFundCurrentMetricsObservation:
    """
    Normalized Point-in-Time current fund metrics observation for a Turkish TEFAS investment fund.
    Represents the latest reported AUM, outstanding units, and investor count.
    """
    provider_symbol: str
    portfolio_size: Optional[Decimal] = None
    portfolio_size_currency: Optional[Currency] = None
    outstanding_units: Optional[Decimal] = None
    investor_count: Optional[int] = None
    reported_current_unit_price: Optional[Decimal] = None
    instrument_id: Optional[UUID] = None
    instrument_type: Optional[InstrumentType] = None
    provider: str = "TEFAS"
    snapshot_id: Optional[UUID] = None
    payload_hash: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    effective_date: Optional[date] = None
    status: TefasObservationStatus = TefasObservationStatus.VALID
    confidence_level: DataConfidenceLevel = DataConfidenceLevel.MEDIUM
    diagnostics: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    @property
    def is_valid(self) -> bool:
        """
        An observation is valid if status is VALID, portfolio_size is finite and non-negative (>= 0),
        and portfolio_size_currency is Currency.TRY.
        """
        return (
            self.status == TefasObservationStatus.VALID
            and self.portfolio_size is not None
            and self.portfolio_size.is_finite()
            and self.portfolio_size >= Decimal("0")
            and self.portfolio_size_currency == Currency.TRY
        )

    def to_normalized_observation_record(self) -> NormalizedObservationRecord:
        """Converts to canonical PIT storage model."""
        if not self.is_valid:
            data_status = DataStatus.UNAVAILABLE
        elif (
            self.investor_count is not None
            and self.outstanding_units is not None
            and self.outstanding_units.is_finite()
            and self.outstanding_units >= Decimal("0")
        ):
            data_status = DataStatus.COMPLETE
        else:
            data_status = DataStatus.PARTIAL

        obs_data: Dict[str, Any] = {
            "provider_symbol": self.provider_symbol,
            "portfolio_size": str(self.portfolio_size) if self.portfolio_size is not None else None,
            "portfolio_size_currency": self.portfolio_size_currency.value if self.portfolio_size_currency else None,
            "outstanding_units": str(self.outstanding_units) if self.outstanding_units is not None else None,
            "investor_count": self.investor_count,
            "reported_current_unit_price": str(self.reported_current_unit_price) if self.reported_current_unit_price is not None else None,
        }

        return NormalizedObservationRecord(
            id=self.id,
            snapshot_id=self.snapshot_id,
            instrument_id=self.instrument_id,
            asset_class=AssetClass.FUND,
            instrument_type=self.instrument_type,
            observation_type="TEFAS_FUND_CURRENT_METRICS",
            observation_data=obs_data,
            data_status=data_status,
            confidence_level=self.confidence_level,
            source_tier=SourceTier.TIER_2_EXCHANGE,
            effective_date=None,
            observed_at=self.retrieved_at,
            currency=self.portfolio_size_currency,
            published_at=None,
            warnings=list(self.diagnostics),
            source_refs=[f"{self.provider}:{self.provider_symbol}@CURRENT"],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes observation to dict."""
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "provider_symbol": self.provider_symbol,
            "portfolio_size": str(self.portfolio_size) if self.portfolio_size is not None else None,
            "portfolio_size_currency": self.portfolio_size_currency.value if self.portfolio_size_currency else None,
            "outstanding_units": str(self.outstanding_units) if self.outstanding_units is not None else None,
            "investor_count": self.investor_count,
            "reported_current_unit_price": str(self.reported_current_unit_price) if self.reported_current_unit_price is not None else None,
            "instrument_type": self.instrument_type.value if self.instrument_type else None,
            "provider": self.provider,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "payload_hash": self.payload_hash,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "published_at": None,
            "effective_date": None,
            "status": self.status.value,
            "confidence_level": self.confidence_level.value,
            "diagnostics": list(self.diagnostics),
        }


@dataclass
class TefasFundMetricsSnapshot:
    """
    Immutable raw provider response snapshot for a TEFAS current fund metrics request.
    """
    provider: str = "TEFAS"
    provider_symbol: str = ""
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    http_status: int = 200
    payload_hash: str = ""
    raw_payload: str = ""
    instrument_id: Optional[UUID] = None
    endpoint: str = "FUND_CURRENT_METRICS"
    parser_version: str = "1.0.0"
    is_rate_limited: bool = False
    observation: Optional[TefasFundCurrentMetricsObservation] = None
    diagnostics: List[str] = field(default_factory=list)
    reconciliation_absolute_diff: Optional[Decimal] = None
    reconciliation_relative_diff: Optional[Decimal] = None
    id: UUID = field(default_factory=uuid4)

    def to_raw_provider_snapshot_record(self) -> RawProviderSnapshotRecord:
        """Converts to canonical PIT raw snapshot record."""
        req_params: Dict[str, Any] = {
            "fonKodu": self.provider_symbol,
            "dil": "TR",
        }

        resp_meta: Dict[str, Any] = {
            "is_rate_limited": self.is_rate_limited,
            "has_portfolio_size": self.observation.portfolio_size is not None if self.observation else False,
            "has_investor_count": self.observation.investor_count is not None if self.observation else False,
            "has_outstanding_units": self.observation.outstanding_units is not None if self.observation else False,
            "has_reported_current_unit_price": self.observation.reported_current_unit_price is not None if self.observation else False,
            "reconciliation_absolute_difference": str(self.reconciliation_absolute_diff) if self.reconciliation_absolute_diff is not None else None,
            "reconciliation_relative_difference": str(self.reconciliation_relative_diff) if self.reconciliation_relative_diff is not None else None,
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
            response_metadata=resp_meta,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "provider_symbol": self.provider_symbol,
            "retrieved_at": self.retrieved_at.isoformat(),
            "http_status": self.http_status,
            "payload_hash": self.payload_hash,
            "endpoint": self.endpoint,
            "parser_version": self.parser_version,
            "is_rate_limited": self.is_rate_limited,
            "observation": self.observation.to_dict() if self.observation else None,
            "reconciliation_absolute_difference": str(self.reconciliation_absolute_diff) if self.reconciliation_absolute_diff is not None else None,
            "reconciliation_relative_difference": str(self.reconciliation_relative_diff) if self.reconciliation_relative_diff is not None else None,
            "diagnostics": list(self.diagnostics),
        }
