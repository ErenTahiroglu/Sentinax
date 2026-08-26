"""
backend/engine/private/provider_contract.py
=============================================
Minimal contract (Protocol + response dataclass) that ALL future data providers
must implement. No actual providers are written here.

Design references (conceptual only — no code or dependencies borrowed):
    - OpenBB Platform: provider_name, provenance metadata per response
    - LEAN Engine: instrument ID stability, corporate-action separation
    - Freqtrade: historical/live separation, lookahead-bias protection

Requirements for any compliant provider:
    1. fetch()      — returns raw response + metadata (ProviderResponse)
    2. normalize()  — maps raw to a canonical dict keyed by field name
    3. validate()   — returns a list of warnings (never raises for missing data)
    4. provenance() — returns a ProviderProvenance for audit trail

Point-in-time rule:
    effective_date MUST reflect when the data was true, not when it was fetched.
    A quarterly earnings filing from 2024-Q3 has effective_date = last day of Q3,
    regardless of when the filing was retrieved.

No external dependencies — pure Python 3.10+ stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from backend.engine.private.domain import DataStatus, SourceTier


# ─────────────────────────────────────────────────────────────────────────────
# Provider Response
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderResponse:
    """
    The canonical output of any data provider's fetch() call.

    All fields are point-in-time safe:
        retrieved_at  — when the API call was made (wall-clock UTC)
        published_at  — when the source published the data (if known)
        effective_date — the date the data was economically true

    Design rule: A provider MUST NOT infer effective_date from retrieved_at.
    If effective_date is unknown, set it to None and add a warning.

    Attributes:
        provider_name:   Unique, stable identifier for the provider.
        source_quality:  SourceTier classification of this provider.
        retrieved_at:    UTC datetime of the fetch call.
        published_at:    UTC datetime the source published this data. May be None.
        effective_date:  The economic effective date of the data. May be None.
        status:          Whether the fetch succeeded and data is usable.
        raw:             The unmodified raw payload from the provider.
                         Preserved for audit and re-normalization.
        warnings:        Non-fatal issues encountered during the fetch.
                         Examples: rate-limit header seen, field missing in response.
        instrument_id:   The provider-specific instrument identifier used.
    """
    provider_name: str
    source_quality: SourceTier
    retrieved_at: datetime
    published_at: datetime | None
    effective_date: date | None
    status: DataStatus
    raw: Any
    warnings: list[str] = field(default_factory=list)
    instrument_id: str = ""

    @property
    def is_usable(self) -> bool:
        """True if the response can be normalized and used."""
        return self.status not in (DataStatus.UNAVAILABLE,)

    def to_source_ref(self) -> str:
        """Generate a compact source reference string for DataResult.source_refs."""
        eff = self.effective_date.isoformat() if self.effective_date else "unknown-date"
        return f"{self.provider_name}:{self.instrument_id}@{eff}"


# ─────────────────────────────────────────────────────────────────────────────
# Provider Provenance
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProviderProvenance:
    """
    Audit trail for a data point: which provider, which version, which endpoint.

    Used by the Private Engine to reconstruct the data lineage for any
    computed result. Stored alongside the result for audit purposes.
    """
    provider_name: str
    provider_version: str               # Semantic version of the provider module
    endpoint: str                       # API endpoint or method name used
    instrument_id: str                  # Provider-specific instrument identifier
    retrieved_at: datetime
    effective_date: date | None
    source_quality: SourceTier


# ─────────────────────────────────────────────────────────────────────────────
# Provider Protocol
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class DataProviderContract(Protocol):
    """
    Structural interface that every data provider must satisfy.

    Implementing classes do NOT need to inherit from this Protocol.
    Python's structural subtyping (duck typing) handles conformance checks.
    Use isinstance(provider, DataProviderContract) for runtime checks.

    Methods:
        fetch(instrument_id) → ProviderResponse
            Retrieve raw data for the given instrument.
            Must NEVER raise for missing data — return status=UNAVAILABLE instead.
            Must NEVER fabricate data if the instrument is unknown.

        normalize(raw) → dict[str, Any]
            Map the raw payload to a canonical field dict.
            Unknown fields may be omitted; absent fields must NOT be set to 0.

        validate(normalized) → list[str]
            Check the normalized dict for anomalies.
            Returns a list of warning strings. Empty list = clean.
            NEVER raises — all issues are returned as warnings.

        provenance(response) → ProviderProvenance
            Return the audit trail for a given ProviderResponse.

    Attributes:
        provider_name (str): Unique, stable, human-readable identifier.
        source_quality (SourceTier): Quality tier declared by the provider.
    """
    provider_name: str
    source_quality: SourceTier

    def fetch(self, instrument_id: str) -> ProviderResponse:
        """Retrieve raw data. Never raises for missing data."""
        ...

    def normalize(self, raw: Any) -> dict[str, Any]:
        """Map raw payload to canonical field dict. No fabrication."""
        ...

    def validate(self, normalized: dict[str, Any]) -> list[str]:
        """Return warnings for anomalies. Never raises."""
        ...

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        """Return the audit trail for this response."""
        ...
