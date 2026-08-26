"""
backend/engine/private/macro/models.py
========================================
Canonical data models for Macroeconomic Series and Point-in-Time Observations.

Core Principles:
    - Macro series (FX, Interest Rates, Inflation) have distinct identities from financial instruments (stocks/funds).
    - Enforces point-in-time timestamps (effective_date, published_at, observed_at, ingested_at).
    - Missing observation is ALWAYS None (Never fabricated as 0.0).
    - Manual ENAG records strictly enforce a verification lifecycle (PENDING, VERIFIED, REJECTED).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from backend.engine.private.domain import (
    DataConfidenceLevel,
    DataStatus,
    FreshnessBasis,
    SourceTier,
)


class MacroCategory(Enum):
    """Categorical classification of macroeconomic time-series."""
    FX = "fx"
    INTEREST_RATE = "interest_rate"
    INFLATION_CPI = "inflation_cpi"
    INFLATION_PPI = "inflation_ppi"
    LABOR = "labor"
    OUTPUT = "output"
    INDUSTRIAL_ACTIVITY = "industrial_activity"
    MONEY_SUPPLY = "money_supply"
    RESERVES = "reserves"


class MacroFrequency(Enum):
    """Reporting frequency of macroeconomic observations."""
    DAILY = "daily"
    BUSINESS_DAILY = "business_daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    EVENT_DRIVEN = "event_driven"


class MacroUnit(Enum):
    """Unit of measurement for macroeconomic data."""
    TRY = "TRY"
    USD = "USD"
    EUR = "EUR"
    PERCENT = "PERCENT"
    INDEX_POINTS = "INDEX_POINTS"
    MILLION_TRY = "MILLION_TRY"
    MILLION_USD = "MILLION_USD"
    MILLION_EUR = "MILLION_EUR"
    BILLIONS_USD = "BILLIONS_USD"


class VerificationStatus(Enum):
    """Verification lifecycle status for manually entered data points (e.g. ENAG)."""
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ContractStatus(Enum):
    """Contract and series code verification status."""
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    DISABLED = "disabled"


@dataclass
class MacroSeriesDefinition:
    """
    Canonical definition of a macroeconomic series.
    Decoupled from financial instruments (equities/funds).
    """
    canonical_key: str              # e.g. 'TR_FX_USDTRY', 'US_CPI_HEADLINE_INDEX'
    provider: str                   # e.g. 'TCMB_EVDS', 'FRED_ALFRED', 'TUIK_SDMX'
    provider_series_code: str       # e.g. 'TP.DK.USD.A.YTL', 'CPIAUCSL'
    category: MacroCategory
    description: str
    unit: MacroUnit
    frequency: MacroFrequency
    freshness_basis: FreshnessBasis
    source_tier: SourceTier
    geography: str                  # 'TR', 'US', 'EA', etc. (Explicit, no silent default)
    provider_native_units: Optional[str] = None
    provider_native_geography: Optional[str] = None
    composition_member_count: Optional[int] = None
    composition_valid_from: Optional[str] = None
    seasonal_adjustment: Optional[str] = None
    origin_source: Optional[str] = None
    release_name: Optional[str] = None
    contract_status: ContractStatus = ContractStatus.VERIFIED
    expected_release_interval_days: Optional[int] = 1
    source_url: Optional[str] = None
    verification_source: Optional[str] = None
    verification_notes: Optional[str] = None
    is_active: bool = True
    id: UUID = field(default_factory=uuid4)


@dataclass
class MacroObservationRecord:
    """
    Point-in-Time (PIT) normalized observation for a macroeconomic series.
    """
    series_key: str
    effective_date: date
    value: Optional[float]
    unit: MacroUnit
    frequency: MacroFrequency
    data_status: DataStatus
    confidence_level: DataConfidenceLevel
    source_tier: SourceTier
    retrieved_at: datetime
    published_at: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    source_available_date: Optional[date] = None       # e.g. Proven availability date
    availability_precision: Optional[str] = None       # 'DATE', 'TIMESTAMP', or None
    realtime_end: Optional[date] = None
    vintage_date: Optional[date] = None
    origin_source: Optional[str] = None
    release_name: Optional[str] = None
    snapshot_id: Optional[UUID] = None
    supersedes_record_id: Optional[UUID] = None
    is_superseded: bool = False
    superseded_at: Optional[datetime] = None
    warnings: List[str] = field(default_factory=list)
    source_ref: Optional[str] = None
    raw_payload: Any = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.observed_at is None:
            self.observed_at = self.retrieved_at
        if self.availability_precision is not None:
            valid_precisions = {"DATE", "TIMESTAMP"}
            if self.availability_precision not in valid_precisions:
                raise ValueError(
                    f"Invalid availability_precision: '{self.availability_precision}'. Must be 'DATE', 'TIMESTAMP', or None."
                )
        if self.source_available_date is None and self.availability_precision is not None:
            raise ValueError(
                f"Cannot specify availability_precision '{self.availability_precision}' when source_available_date is None."
            )

    @property
    def is_usable(self) -> bool:
        return self.data_status != DataStatus.UNAVAILABLE and self.value is not None

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "series_key": self.series_key,
            "effective_date": self.effective_date.isoformat(),
            "value": self.value,
            "unit": self.unit.value,
            "frequency": self.frequency.value,
            "data_status": self.data_status.value,
            "confidence_level": self.confidence_level.value,
            "source_tier": self.source_tier.value,
            "retrieved_at": self.retrieved_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "source_available_date": self.source_available_date.isoformat() if self.source_available_date else None,
            "availability_precision": self.availability_precision,
            "realtime_end": self.realtime_end.isoformat() if self.realtime_end else None,
            "vintage_date": self.vintage_date.isoformat() if self.vintage_date else None,
            "origin_source": self.origin_source,
            "release_name": self.release_name,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "supersedes_record_id": str(self.supersedes_record_id) if self.supersedes_record_id else None,
            "is_superseded": self.is_superseded,
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
            "warnings": self.warnings,
            "source_ref": self.source_ref,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ManualENAGRecord:
    """
    Manually entered and verified ENAG inflation observation.
    Requires explicit verification (VERIFIED) before use in decision models.
    """
    reference_period: str           # YYYY-MM (e.g. '2024-05')
    value_type: str                 # 'MONTHLY_PCT' or 'ANNUAL_PCT'
    value: float                    # e.g. 5.27
    unit: MacroUnit = MacroUnit.PERCENT
    source_url: str = ""
    source_title: str = "ENAGrup Tüketici Fiyat Endeksi Bülteni"
    verification_status: VerificationStatus = VerificationStatus.PENDING
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    verified_by: Optional[str] = None
    notes: Optional[str] = None
    supersedes_record_id: Optional[UUID] = None
    id: UUID = field(default_factory=uuid4)

    @property
    def is_usable(self) -> bool:
        """Only VERIFIED records with positive values and valid source URLs are usable."""
        return (
            self.verification_status == VerificationStatus.VERIFIED
            and bool(self.source_url)
            and self.value is not None
        )
