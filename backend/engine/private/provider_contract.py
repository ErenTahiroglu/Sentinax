"""
backend/engine/private/provider_contract.py
=============================================
Common provider protocol, execution context, and diagnostic audit models.

Core Principles:
    - Provider Framework is strictly decoupled from specific external data vendors.
    - FetchContext cleanly separates canonical UUID from provider-native symbol.
    - Historical requests are explicitly separated from latest/live requests.
    - Providers are ONLY responsible for their own fetch/normalize/validate/provenance.
    - Provider NEVER selects fallback or knows about sibling providers.
    - Async execution model natively integrates with FastAPI and httpx.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from uuid import UUID

from backend.engine.private.domain import (
    AsOfMode,
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fetch Context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FetchContext:
    """
    Context for a data fetch request.

    Separates the canonical instrument identity from the provider-native symbol.
    Historical requests (`as_of_time` is set) are explicitly flagged to prevent
    stale/latest cache collisions.
    """
    observation_type: str
    canonical_instrument_id: Optional[UUID] = None
    provider_symbol: Optional[str] = None
    as_of_time: Optional[datetime] = None
    as_of_mode: AsOfMode = AsOfMode.SYSTEM_AS_OF
    effective_date: Optional[date] = None
    request_parameters: Dict[str, Any] = field(default_factory=dict)
    force_refresh: bool = False

    @property
    def is_historical(self) -> bool:
        """True if this request targets a specific point in time in the past."""
        return self.as_of_time is not None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Provider Attempt Diagnostics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderAttempt:
    """
    Diagnostic record of an individual provider fetch attempt.
    Preserves audit trail of why a provider failed before fallback was triggered.
    """
    provider_name: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = False
    failure_type: Optional[str] = None  # e.g. 'TIMEOUT', 'HTTP_ERROR', 'SCHEMA_MISMATCH', 'CIRCUIT_OPEN', 'UNAVAILABLE'
    message: Optional[str] = None
    latency_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provider Response
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderResponse:
    """
    Canonical output of any data provider's fetch() call.

    All fields are point-in-time safe:
        retrieved_at   — when the fetch call occurred (wall-clock UTC)
        published_at   — when the source published the data (if known)
        effective_date — the calendar date the economic truth applies to
    """
    provider_name: str
    source_quality: SourceTier
    retrieved_at: datetime
    published_at: Optional[datetime]
    effective_date: Optional[date]
    status: DataStatus
    raw: Any
    warnings: List[str] = field(default_factory=list)
    canonical_instrument_id: Optional[UUID] = None
    provider_symbol: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        """True if the response can be normalized and used."""
        return self.status not in (DataStatus.UNAVAILABLE,)

    def to_source_ref(self) -> str:
        """Generates a compact, unambiguous source reference string."""
        eff = self.effective_date.isoformat() if self.effective_date else "unknown-date"
        identifier = self.provider_symbol or (str(self.canonical_instrument_id) if self.canonical_instrument_id else "unspecified")
        return f"{self.provider_name}:{identifier}@{eff}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Provider Provenance
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderProvenance:
    """
    Audit trail for a data point: which provider, which version, which endpoint.
    """
    provider_name: str
    provider_version: str               # Semantic version of provider module
    endpoint: str                       # API endpoint / method identifier
    retrieved_at: datetime
    source_quality: SourceTier
    canonical_instrument_id: Optional[UUID] = None
    provider_symbol: Optional[str] = None
    effective_date: Optional[date] = None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Data Provider Protocol (Async Contract)
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class DataProviderContract(Protocol):
    """
    Structural interface that every data provider must satisfy.

    Methods:
        fetch(context) → ProviderResponse
            Retrieve raw data for the given context.
            Must NEVER raise for missing data — return status=UNAVAILABLE instead.
            Must NEVER fabricate data if the instrument or field is unknown.

        normalize(raw) → dict[str, Any]
            Map the raw payload to a canonical field dict.
            Unknown fields may be omitted; absent fields must NOT be set to 0.

        validate(normalized) → list[str]
            Check the normalized dict for anomalies/schema violations.
            Returns a list of warning strings. Empty list = clean.
            NEVER raises — all non-fatal issues are returned as warnings.

        provenance(response) → ProviderProvenance
            Return the audit trail for a given ProviderResponse.
    """
    provider_name: str
    source_quality: SourceTier
    access_status: ProviderAccessStatus

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """Retrieve raw data asynchronously. Never raises for missing data."""
        ...

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """Map raw payload to canonical field dict. No fabrication."""
        ...

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        """Return warnings for anomalies/schema deviations. Never raises."""
        ...

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        """Return the audit trail for this response."""
        ...
