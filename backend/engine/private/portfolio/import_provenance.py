"""
backend/engine/private/portfolio/import_provenance.py
=====================================================
Broker/File Import Provenance & Raw-Record Identity Foundation (Phase 13A).

This module establishes the immutable provenance and raw-record identity layer
for statement/file imports (CSV, XLSX, PDF, etc.) before parser creation.

Key Architectural Invariants:
1. Target-Bound & Source-Bound Identity:
   - File identity is bound to (portfolio_id, account_id, source_key, content_sha256).
   - Same bytes imported into different portfolios, accounts, or under different source parsers
     produce distinct file identities.
2. Content Hash Authority (Filename is Metadata Only):
   - content_sha256 is the exact lowercase SHA-256 hex digest of raw file bytes.
   - Filenames are display metadata only and do NOT participate in content identity.
3. Separation from Ledger External Identity:
   - Import provenance is for staging, audit, and diagnostics ONLY.
   - Provenance identities (file_identity, record_identity) MUST NOT be mapped to
     PortfolioTransaction.external_source or PortfolioTransaction.external_reference.
4. Record Identity & Granularity:
   - Record provenance is bound to (file_identity, record_ordinal, record_sha256).
   - Preserves parser-defined record_ordinal (1-indexed) and exact record byte hash.
   - Raw file/record bytes are NOT retained in provenance data structures.
5. Strict Type & Format Defense:
   - Exact lowercase 64-char hex digests, strict ASCII source_keys, non-empty byte payloads.
   - Naive/null-offset datetimes, string UUIDs, floats, and mutable collections are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from typing import Any, Tuple
from uuid import UUID

_SOURCE_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")


class PortfolioImportProvenanceError(ValueError):
    """Raised when import provenance validation fails closed on malformed inputs."""
    pass


def _is_aware_datetime(dt: Any) -> bool:
    """Returns True if dt is a non-bool datetime with tzinfo and a non-None utcoffset."""
    if dt is None or isinstance(dt, bool) or not isinstance(dt, datetime):
        return False
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def _validate_uuid(val: Any, field_name: str) -> UUID:
    """Validates that val is a non-bool UUID instance."""
    if isinstance(val, bool) or not isinstance(val, UUID):
        raise PortfolioImportProvenanceError(
            f"{field_name} must be a UUID instance, got {type(val).__name__}"
        )
    return val


def _validate_source_key(val: Any) -> str:
    """Validates that source_key satisfies strict ASCII lowercase syntax [a-z0-9][a-z0-9._-]{0,63}."""
    if isinstance(val, bool) or not isinstance(val, str):
        raise PortfolioImportProvenanceError(
            f"source_key must be a str instance, got {type(val).__name__}"
        )
    if not _SOURCE_KEY_PATTERN.fullmatch(val):
        raise PortfolioImportProvenanceError(
            f"source_key must be 1-64 ASCII lowercase alphanumeric characters or '._-', got: {val!r}"
        )
    return val


def _validate_filename(val: Any) -> str:
    """Validates display filename metadata."""
    if isinstance(val, bool) or not isinstance(val, str):
        raise PortfolioImportProvenanceError(
            f"filename must be a str instance, got {type(val).__name__}"
        )
    if not val or not val.strip():
        raise PortfolioImportProvenanceError("filename must not be empty or whitespace-only")
    if len(val) > 255:
        raise PortfolioImportProvenanceError(f"filename exceeds maximum length of 255 characters ({len(val)})")
    return val


def _validate_sha256_hex(val: Any, field_name: str) -> str:
    """Validates exact lowercase 64-char hexadecimal SHA-256 string."""
    if isinstance(val, bool) or not isinstance(val, str):
        raise PortfolioImportProvenanceError(
            f"{field_name} must be a str instance, got {type(val).__name__}"
        )
    if not _SHA256_HEX_PATTERN.fullmatch(val):
        raise PortfolioImportProvenanceError(
            f"{field_name} must be exactly 64 lowercase hexadecimal characters, got: {val!r}"
        )
    return val


def _validate_imported_at(val: Any) -> datetime:
    """Validates timezone-aware datetime with non-null utcoffset."""
    if isinstance(val, bool) or not isinstance(val, datetime):
        raise PortfolioImportProvenanceError(
            f"imported_at must be a datetime instance, got {type(val).__name__}"
        )
    if not _is_aware_datetime(val):
        raise PortfolioImportProvenanceError(
            f"imported_at must be timezone-aware with non-null utcoffset, got: {val}"
        )
    return val


def _validate_byte_length(val: Any) -> int:
    """Validates positive integer byte length."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise PortfolioImportProvenanceError(
            f"byte_length must be an int instance, got {type(val).__name__}"
        )
    if val <= 0:
        raise PortfolioImportProvenanceError(f"byte_length must be greater than 0, got {val}")
    return val


def _validate_record_ordinal(val: Any) -> int:
    """Validates 1-indexed positive integer record ordinal."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise PortfolioImportProvenanceError(
            f"record_ordinal must be an int instance, got {type(val).__name__}"
        )
    if val < 1:
        raise PortfolioImportProvenanceError(f"record_ordinal must be at least 1, got {val}")
    return val


@dataclass(frozen=True)
class ImportFileProvenance:
    """
    Immutable provenance and content identity for a raw imported file.
    """
    portfolio_id: UUID
    account_id: UUID
    source_key: str
    filename: str
    content_sha256: str
    byte_length: int
    imported_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.portfolio_id, "portfolio_id")
        _validate_uuid(self.account_id, "account_id")
        _validate_source_key(self.source_key)
        _validate_filename(self.filename)
        _validate_sha256_hex(self.content_sha256, "content_sha256")
        _validate_byte_length(self.byte_length)
        _validate_imported_at(self.imported_at)

    @property
    def file_identity(self) -> Tuple[UUID, UUID, str, str]:
        """
        Canonical target-bound and source-bound file identity tuple:
        (portfolio_id, account_id, source_key, content_sha256).
        """
        return (
            self.portfolio_id,
            self.account_id,
            self.source_key,
            self.content_sha256,
        )


def build_import_file_provenance(
    portfolio_id: UUID,
    account_id: UUID,
    source_key: str,
    filename: str,
    content: bytes,
    imported_at: datetime,
) -> ImportFileProvenance:
    """
    Constructs an authoritative ImportFileProvenance instance from raw file bytes.

    Args:
        portfolio_id: Target portfolio UUID.
        account_id: Target portfolio account UUID.
        source_key: Canonical source/parser identifier.
        filename: Display filename metadata.
        content: Raw file bytes (must be non-empty bytes).
        imported_at: Timestamp of import (must be timezone-aware).

    Returns:
        Immutable ImportFileProvenance instance.

    Raises:
        PortfolioImportProvenanceError: If any parameter violates domain constraints.
    """
    if type(content) is not bytes or isinstance(content, (bytearray, memoryview)):
        raise PortfolioImportProvenanceError(
            f"content must be an immutable bytes instance, got {type(content).__name__}"
        )
    if len(content) == 0:
        raise PortfolioImportProvenanceError("content must not be empty")

    content_sha256 = hashlib.sha256(content).hexdigest()
    byte_length = len(content)

    return ImportFileProvenance(
        portfolio_id=portfolio_id,
        account_id=account_id,
        source_key=source_key,
        filename=filename,
        content_sha256=content_sha256,
        byte_length=byte_length,
        imported_at=imported_at,
    )


@dataclass(frozen=True)
class ImportRecordProvenance:
    """
    Immutable provenance and identity for a single opaque raw record within an imported file.
    """
    file_identity: Tuple[UUID, UUID, str, str]
    record_ordinal: int
    record_sha256: str

    def __post_init__(self) -> None:
        if type(self.file_identity) is not tuple or len(self.file_identity) != 4:
            raise PortfolioImportProvenanceError(
                f"file_identity must be a 4-tuple (portfolio_id, account_id, source_key, content_sha256), got {type(self.file_identity).__name__}"
            )
        _validate_uuid(self.file_identity[0], "file_identity[0] (portfolio_id)")
        _validate_uuid(self.file_identity[1], "file_identity[1] (account_id)")
        _validate_source_key(self.file_identity[2])
        _validate_sha256_hex(self.file_identity[3], "file_identity[3] (content_sha256)")
        _validate_record_ordinal(self.record_ordinal)
        _validate_sha256_hex(self.record_sha256, "record_sha256")

    @property
    def record_identity(self) -> Tuple[UUID, UUID, str, str, int, str]:
        """
        Canonical staged record identity tuple:
        (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256).
        """
        return (
            self.file_identity[0],
            self.file_identity[1],
            self.file_identity[2],
            self.file_identity[3],
            self.record_ordinal,
            self.record_sha256,
        )


def build_import_record_provenance(
    file_provenance: ImportFileProvenance,
    record_ordinal: int,
    raw_record: bytes,
) -> ImportRecordProvenance:
    """
    Constructs an authoritative ImportRecordProvenance instance for an extracted record.

    Args:
        file_provenance: ImportFileProvenance of the enclosing source file.
        record_ordinal: 1-indexed record sequence number within the source file.
        raw_record: Exact raw record bytes extracted by parser.

    Returns:
        Immutable ImportRecordProvenance instance.

    Raises:
        PortfolioImportProvenanceError: If any parameter violates domain constraints.
    """
    if not isinstance(file_provenance, ImportFileProvenance):
        raise PortfolioImportProvenanceError(
            f"file_provenance must be an ImportFileProvenance instance, got {type(file_provenance).__name__}"
        )
    if type(raw_record) is not bytes or isinstance(raw_record, (bytearray, memoryview)):
        raise PortfolioImportProvenanceError(
            f"raw_record must be an immutable bytes instance, got {type(raw_record).__name__}"
        )
    if len(raw_record) == 0:
        raise PortfolioImportProvenanceError("raw_record must not be empty")

    _validate_record_ordinal(record_ordinal)
    record_sha256 = hashlib.sha256(raw_record).hexdigest()

    return ImportRecordProvenance(
        file_identity=file_provenance.file_identity,
        record_ordinal=record_ordinal,
        record_sha256=record_sha256,
    )
