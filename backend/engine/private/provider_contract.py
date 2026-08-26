"""
backend/engine/private/provider_contract.py
=============================================
Common provider protocol, execution context, and diagnostic audit models.

Core Principles:
    - Provider Framework is strictly decoupled from specific external data vendors.
    - FetchContext cleanly separates canonical UUID from provider-native symbol.
    - Historical requests are explicitly separated from latest/live requests.
    - Cache keys deterministically incorporate all material parameters while stripping secrets.
    - Providers are ONLY responsible for their own fetch/normalize/validate/provenance.
    - Provider NEVER selects fallback or knows about sibling providers.
    - Async execution model natively integrates with FastAPI and httpx.
"""

from __future__ import annotations

import hashlib
import json
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

    def __post_init__(self) -> None:
        if isinstance(self.as_of_mode, str):
            val = self.as_of_mode.strip()
            try:
                self.as_of_mode = AsOfMode(val.lower())
            except ValueError:
                try:
                    self.as_of_mode = AsOfMode[val.upper()]
                except KeyError:
                    raise ValueError(f"Invalid as_of_mode: '{self.as_of_mode}'. Must be AsOfMode.SYSTEM_AS_OF or AsOfMode.SOURCE_AS_OF.")
        elif not isinstance(self.as_of_mode, AsOfMode):
            raise ValueError(f"as_of_mode must be an instance of AsOfMode, got {type(self.as_of_mode).__name__}")

    @property
    def is_historical(self) -> bool:
        """True if this request targets a specific point in time in the past."""
        return self.as_of_time is not None

    def generate_cache_key(self) -> str:
        """
        Generates a collision-resistant deterministic cache key.
        Strips sensitive credential keys (e.g. api_key, token, password, secret).
        """
        # Filter sensitive fields from request_parameters
        sanitized_params = {}
        sensitive_substrings = {"key", "token", "secret", "password", "auth", "credential"}
        for k, v in sorted(self.request_parameters.items()):
            if not any(sub in k.lower() for sub in sensitive_substrings):
                sanitized_params[k] = v

        params_json = json.dumps(sanitized_params, sort_keys=True, default=str)
        params_hash = hashlib.sha256(params_json.encode("utf-8")).hexdigest()[:16]

        inst_part = str(self.canonical_instrument_id) if self.canonical_instrument_id else "none"
        sym_part = self.provider_symbol or "none"
        eff_part = self.effective_date.isoformat() if self.effective_date else "latest"
        
        if self.is_historical and self.as_of_time:
            pit_part = f"asof_{self.as_of_mode.value}_{self.as_of_time.isoformat()}"
        else:
            pit_part = "live"

        return f"pit:{self.observation_type}:{inst_part}:{sym_part}:{eff_part}:{pit_part}:{params_hash}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Provider Attempt Diagnostics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderAttempt:
    """
    Diagnostic record of an individual provider fetch attempt.
    Preserves audit trail of why a provider failed and retry count before fallback was triggered.
    """
    provider_name: str
    attempt_number: int = 1
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = False
    failure_type: Optional[str] = None  # e.g. 'TIMEOUT', 'RATE_LIMIT', 'AUTH_ERROR', 'SCHEMA_MISMATCH', 'LOOKAHEAD_REJECTED'
    message: Optional[str] = None
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "attempt_number": self.attempt_number,
            "timestamp": self.timestamp.isoformat(),
            "success": self.success,
            "failure_type": self.failure_type,
            "message": self.message,
            "latency_ms": round(self.latency_ms, 2),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderAttempt:
        return cls(
            provider_name=data["provider_name"],
            attempt_number=data.get("attempt_number", 1),
            timestamp=datetime.fromisoformat(data["timestamp"]) if isinstance(data["timestamp"], str) else data["timestamp"],
            success=data["success"],
            failure_type=data.get("failure_type"),
            message=data.get("message"),
            latency_ms=data.get("latency_ms", 0.0),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provider Response
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderResponse:
    """
    Canonical output of any data provider's fetch() call.

    All fields are point-in-time safe:
        retrieved_at   — when the network fetch completed (wall-clock UTC)
        observed_at    — when Sentinax captured the observation fact
        published_at   — when the source officially published the data (if known)
        effective_date — the calendar date the economic truth applies to
    """
    provider_name: str
    source_quality: SourceTier
    retrieved_at: datetime
    published_at: Optional[datetime]
    effective_date: Optional[date]
    status: DataStatus
    raw: Any
    observed_at: Optional[datetime] = None
    warnings: List[str] = field(default_factory=list)
    canonical_instrument_id: Optional[UUID] = None
    provider_symbol: Optional[str] = None
    source_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_at is None:
            self.observed_at = self.retrieved_at

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
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "endpoint": self.endpoint,
            "retrieved_at": self.retrieved_at.isoformat(),
            "source_quality": self.source_quality.value,
            "canonical_instrument_id": str(self.canonical_instrument_id) if self.canonical_instrument_id else None,
            "provider_symbol": self.provider_symbol,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderProvenance:
        return cls(
            provider_name=data["provider_name"],
            provider_version=data["provider_version"],
            endpoint=data["endpoint"],
            retrieved_at=datetime.fromisoformat(data["retrieved_at"]) if isinstance(data["retrieved_at"], str) else data["retrieved_at"],
            source_quality=SourceTier(data["source_quality"]),
            canonical_instrument_id=UUID(data["canonical_instrument_id"]) if data.get("canonical_instrument_id") else None,
            provider_symbol=data.get("provider_symbol"),
            effective_date=date.fromisoformat(data["effective_date"]) if data.get("effective_date") else None,
            metadata=data.get("metadata", {}),
        )

    def to_source_ref(self) -> str:
        eff = self.effective_date.isoformat() if self.effective_date else "unknown-date"
        identifier = self.provider_symbol or (str(self.canonical_instrument_id) if self.canonical_instrument_id else "unspecified")
        return f"{self.provider_name}:{identifier}@{eff}"


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
