"""
backend/engine/private/storage_models.py
=========================================
Canonical Point-In-Time (PIT) storage models for the Private Engine.

Maps directly to PostgreSQL / Supabase tables:
    - raw_provider_snapshots
    - normalized_observations

Principles:
    - Raw snapshots are immutable.
    - Revisions create new records with supersedes_record_id.
    - Every snapshot computes a deterministic SHA-256 payload hash.
    - Strict Point-In-Time (PIT) timestamp semantics prevent look-ahead bias.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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


def compute_payload_hash(payload: Any) -> str:
    """
    Computes a deterministic SHA-256 hex digest of a raw payload.
    Handles dict, list, str, bytes, or primitives.
    """
    if isinstance(payload, bytes):
        raw_bytes = payload
    elif isinstance(payload, str):
        raw_bytes = payload.encode("utf-8")
    else:
        # Canonical JSON string with sorted keys
        canonical_str = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        raw_bytes = canonical_str.encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()


@dataclass
class RawProviderSnapshotRecord:
    """
    Python representation of a `raw_provider_snapshots` record.
    """
    provider: str
    endpoint: str
    request_params: Dict[str, Any]
    retrieved_at: datetime
    content_type: str
    raw_payload: Any
    payload_hash: str
    http_status: Optional[int] = None
    response_metadata: Dict[str, Any] = field(default_factory=dict)
    storage_ref: Optional[str] = None
    schema_version: str = "1.0.0"
    parser_version: str = "1.0.0"
    license_profile: str = "PROPRIETARY"
    supersedes_record_id: Optional[UUID] = None
    is_superseded: bool = False
    superseded_at: Optional[datetime] = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def create(
        cls,
        provider: str,
        endpoint: str,
        request_params: Dict[str, Any],
        raw_payload: Any,
        http_status: Optional[int] = 200,
        response_metadata: Optional[Dict[str, Any]] = None,
        content_type: str = "application/json",
        storage_ref: Optional[str] = None,
        schema_version: str = "1.0.0",
        parser_version: str = "1.0.0",
        license_profile: str = "PROPRIETARY",
        supersedes_record_id: Optional[UUID] = None,
        retrieved_at: Optional[datetime] = None,
    ) -> RawProviderSnapshotRecord:
        """Factory method that calculates deterministic payload_hash."""
        calculated_hash = compute_payload_hash(raw_payload)
        now_utc = retrieved_at or datetime.now(timezone.utc)
        return cls(
            provider=provider,
            endpoint=endpoint,
            request_params=request_params,
            retrieved_at=now_utc,
            http_status=http_status,
            response_metadata=response_metadata or {},
            content_type=content_type,
            raw_payload=raw_payload,
            storage_ref=storage_ref,
            payload_hash=calculated_hash,
            schema_version=schema_version,
            parser_version=parser_version,
            license_profile=license_profile,
            supersedes_record_id=supersedes_record_id,
        )

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "endpoint": self.endpoint,
            "request_params": self.request_params,
            "retrieved_at": self.retrieved_at.isoformat(),
            "http_status": self.http_status,
            "response_metadata": self.response_metadata,
            "content_type": self.content_type,
            "raw_payload": self.raw_payload,
            "storage_ref": self.storage_ref,
            "payload_hash": self.payload_hash,
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "license_profile": self.license_profile,
            "supersedes_record_id": str(self.supersedes_record_id) if self.supersedes_record_id else None,
            "is_superseded": self.is_superseded,
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class NormalizedObservationRecord:
    """
    Python representation of a `normalized_observations` record.
    Enforces strict point-in-time semantics.
    """
    snapshot_id: Optional[UUID] = None
    instrument_id: Optional[UUID] = None
    asset_class: Optional[AssetClass] = None
    instrument_type: Optional[InstrumentType] = None
    observation_type: str = ""
    observation_data: Dict[str, Any] = field(default_factory=dict)
    data_status: DataStatus = DataStatus.COMPLETE
    confidence_level: DataConfidenceLevel = DataConfidenceLevel.HIGH
    source_tier: SourceTier = SourceTier.TIER_1_REGULATORY
    effective_date: Optional[date] = None
    observed_at: Optional[datetime] = None
    currency: Optional[Currency] = None
    published_at: Optional[datetime] = None

    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    revised_at: Optional[datetime] = None
    supersedes_record_id: Optional[UUID] = None
    is_superseded: bool = False
    superseded_at: Optional[datetime] = None
    missing_inputs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source_refs: List[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "asset_class": self.asset_class.value if self.asset_class else None,
            "instrument_type": self.instrument_type.value if self.instrument_type else None,
            "observation_type": self.observation_type,

            "observation_data": self.observation_data,
            "data_status": self.data_status.value,
            "confidence_level": self.confidence_level.value,
            "source_tier": self.source_tier.value,
            "currency": self.currency.value if self.currency else None,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "ingested_at": self.ingested_at.isoformat(),
            "revised_at": self.revised_at.isoformat() if self.revised_at else None,
            "supersedes_record_id": str(self.supersedes_record_id) if self.supersedes_record_id else None,
            "is_superseded": self.is_superseded,
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
            "missing_inputs": self.missing_inputs,
            "warnings": self.warnings,
            "source_refs": self.source_refs,
            "created_at": self.created_at.isoformat(),
        }
