"""
backend/engine/private/portfolio/import_batch.py
================================================
Immutable Import Batch Manifest & Record-Set Integrity (Phase 13B).

This module establishes the parser-neutral immutable batch-manifest layer that groups
Phase 13A file and record provenance into one deterministic, integrity-checked record set.

Key Architectural Invariants:
1. File & Record Binding Authority:
   - One ImportBatchManifest binds exactly one ImportFileProvenance and a canonical ordered
     tuple of ImportRecordProvenance instances.
   - Every record MUST match the enclosing file_provenance.file_identity exactly.
   - Cross-file record mixing fails closed immediately.
2. Contiguous & Unique Logical Ordinal Contract:
   - For non-empty record sets, record ordinals MUST be uniquely and contiguously numbered:
     1, 2, 3, ..., N without gaps or duplicates.
   - Zero-length record sets are valid (e.g. empty statement extraction).
3. Deterministic Manifest Preimage & Digest:
   - manifest_sha256 is computed from compact JSON:
     [str(portfolio_id), str(account_id), source_key, file_content_sha256, [[ord, rec_sha], ...]]
   - Filenames, imported_at timestamps, byte lengths, and mutable metadata are excluded.
   - Renaming or re-importing identical bytes under the same source producing the same records
     yields the identical manifest_sha256.
4. Input Order Invariance:
   - Shuffled builder input is canonically sorted by record_ordinal ascending before freezing.
5. Strict Type & Immutability Defense:
   - Frozen dataclass, immutable tuples, strict lowercase 64-char SHA-256 verification.
   - Direct constructor re-verifies digest matching and reject unsorted or malformed collections.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, List, Sequence, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
)

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PortfolioImportBatchError(ValueError):
    """Raised when import batch manifest validation fails closed on malformed or inconsistent inputs."""
    pass


def _compute_manifest_sha256(
    file_identity: Tuple[UUID, UUID, str, str],
    records: Tuple[ImportRecordProvenance, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for an import batch manifest preimage:
    [str(portfolio_id), str(account_id), source_key, file_content_sha256, [[ord, rec_sha], ...]]
    """
    preimage = [
        str(file_identity[0]),
        str(file_identity[1]),
        file_identity[2],
        file_identity[3],
        [[r.record_ordinal, r.record_sha256] for r in records],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImportBatchManifest:
    """
    Immutable manifest binding one ImportFileProvenance to an ordered, contiguous record set.
    """
    file_provenance: ImportFileProvenance
    records: Tuple[ImportRecordProvenance, ...]
    manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.file_provenance, ImportFileProvenance):
            raise PortfolioImportBatchError(
                f"file_provenance must be an ImportFileProvenance instance, got {type(self.file_provenance).__name__}"
            )

        if type(self.records) is not tuple:
            raise PortfolioImportBatchError(
                f"records must be an immutable tuple, got {type(self.records).__name__}"
            )

        # Validate records collection
        expected_file_identity = self.file_provenance.file_identity
        for i, rec in enumerate(self.records):
            if not isinstance(rec, ImportRecordProvenance):
                raise PortfolioImportBatchError(
                    f"records[{i}] must be an ImportRecordProvenance instance, got {type(rec).__name__}"
                )
            if rec.file_identity != expected_file_identity:
                raise PortfolioImportBatchError(
                    f"records[{i}] file_identity {rec.file_identity} does not match file_provenance {expected_file_identity}"
                )
            expected_ordinal = i + 1
            if rec.record_ordinal != expected_ordinal:
                raise PortfolioImportBatchError(
                    f"records must be strictly sorted and contiguous 1..N: expected ordinal {expected_ordinal} at index {i}, got {rec.record_ordinal}"
                )

        # Validate manifest_sha256 format and matching
        if isinstance(self.manifest_sha256, bool) or not isinstance(self.manifest_sha256, str):
            raise PortfolioImportBatchError(
                f"manifest_sha256 must be a str instance, got {type(self.manifest_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.match(self.manifest_sha256):
            raise PortfolioImportBatchError(
                f"manifest_sha256 must be exactly 64 lowercase hexadecimal characters, got: {self.manifest_sha256!r}"
            )

        expected_sha = _compute_manifest_sha256(expected_file_identity, self.records)
        if self.manifest_sha256 != expected_sha:
            raise PortfolioImportBatchError(
                f"manifest_sha256 {self.manifest_sha256} does not match canonical preimage digest {expected_sha}"
            )

    @property
    def record_count(self) -> int:
        """Returns the number of logical records in this manifest."""
        return len(self.records)

    @property
    def manifest_identity(self) -> Tuple[UUID, UUID, str, str, str]:
        """
        Canonical staging manifest identity tuple:
        (portfolio_id, account_id, source_key, file_content_sha256, manifest_sha256).
        """
        return (
            self.file_provenance.portfolio_id,
            self.file_provenance.account_id,
            self.file_provenance.source_key,
            self.file_provenance.content_sha256,
            self.manifest_sha256,
        )


def build_import_batch_manifest(
    file_provenance: ImportFileProvenance,
    records: Sequence[ImportRecordProvenance],
) -> ImportBatchManifest:
    """
    Constructs an authoritative, sorted, integrity-checked ImportBatchManifest.

    Args:
        file_provenance: The authoritative ImportFileProvenance of the source file.
        records: Materialized sequence (list or tuple) of extracted ImportRecordProvenance objects.

    Returns:
        Immutable ImportBatchManifest instance.

    Raises:
        PortfolioImportBatchError: If records are malformed, cross-file, duplicated, or non-contiguous.
    """
    if not isinstance(file_provenance, ImportFileProvenance):
        raise PortfolioImportBatchError(
            f"file_provenance must be an ImportFileProvenance instance, got {type(file_provenance).__name__}"
        )

    if not isinstance(records, (list, tuple)) or isinstance(records, (str, bytes, bytearray, dict)):
        raise PortfolioImportBatchError(
            f"records must be a materialized list or tuple, got {type(records).__name__}"
        )

    # Validate elements and check for duplicate ordinals explicitly without set/dict deduplication
    seen_ordinals: set[int] = set()
    for i, rec in enumerate(records):
        if not isinstance(rec, ImportRecordProvenance):
            raise PortfolioImportBatchError(
                f"records[{i}] must be an ImportRecordProvenance instance, got {type(rec).__name__}"
            )
        if rec.file_identity != file_provenance.file_identity:
            raise PortfolioImportBatchError(
                f"records[{i}] file_identity {rec.file_identity} does not match file_provenance {file_provenance.file_identity}"
            )
        if rec.record_ordinal in seen_ordinals:
            raise PortfolioImportBatchError(
                f"duplicate record_ordinal detected: {rec.record_ordinal}"
            )
        seen_ordinals.add(rec.record_ordinal)

    # Canonically sort by record_ordinal ascending
    sorted_records = tuple(sorted(records, key=lambda r: r.record_ordinal))

    # Verify contiguous 1..N numbering
    for i, rec in enumerate(sorted_records):
        expected_ordinal = i + 1
        if rec.record_ordinal != expected_ordinal:
            raise PortfolioImportBatchError(
                f"records must be contiguous 1..N: expected ordinal {expected_ordinal}, got {rec.record_ordinal}"
            )

    manifest_sha256 = _compute_manifest_sha256(file_provenance.file_identity, sorted_records)

    return ImportBatchManifest(
        file_provenance=file_provenance,
        records=sorted_records,
        manifest_sha256=manifest_sha256,
    )
