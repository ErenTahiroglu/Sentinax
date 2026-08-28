"""
backend/engine/private/portfolio/import_parsed_batch.py
======================================================
Immutable Parsed-Batch Manifest & Full Record-Coverage Integrity (Phase 13D).

This module establishes the parser-neutral batch composition layer that proves one exact
ImportBatchManifest has been parsed completely and consistently under one explicit parser revision.

Key Architectural Invariants:
1. Full Record-Coverage & Exact Provenance Binding:
   - For a raw manifest with N records, the parsed batch MUST contain exactly N parsed records.
   - For every ordinal i, parsed_records[i].record_provenance MUST equal raw_manifest.records[i]
     identically (same ordinal, same raw record hash, and same file identity).
   - Omissions, extras, duplicates, and foreign records fail closed immediately.
2. Single Explicit Parser Revision:
   - The parsed batch and all constituent parsed records MUST share the identical parser_revision (int >= 1).
   - Mixed parser revisions in a single batch fail closed.
   - Empty raw manifests require empty parsed record sets under an explicit parser revision.
3. Deterministic Parsed-Batch Preimage & Digest:
   - parsed_manifest_sha256 is computed from compact JSON:
     [str(portfolio_id), str(account_id), source_key, file_content_sha256, raw_manifest.manifest_sha256, parser_revision, [[ord, rec_sha, parsed_sha], ...]]
   - Filenames, imported_at timestamps, byte lengths, and mutable metadata are strictly excluded.
4. Input Order Invariance:
   - Shuffled builder input is canonically sorted by record_ordinal ascending before freezing.
5. Strict Type, Full-String Lexical, and Immutability Defense:
   - Frozen dataclass, immutable tuples, strict fullmatch 64-char lowercase SHA-256 verification.
   - Direct constructor re-verifies digest matching and independent coverage rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Sequence, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_batch import ImportBatchManifest
from backend.engine.private.portfolio.import_parsing import ParsedImportRecord

_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")


class PortfolioParsedImportBatchError(ValueError):
    """Raised when parsed import batch manifest validation fails closed on coverage, provenance, or revision mismatches."""
    pass


def _compute_parsed_manifest_sha256(
    raw_manifest: ImportBatchManifest,
    parser_revision: int,
    parsed_records: Tuple[ParsedImportRecord, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for a parsed import batch manifest preimage:
    [str(portfolio_id), str(account_id), source_key, file_content_sha256, raw_manifest.manifest_sha256, parser_revision, [[ord, rec_sha, parsed_sha], ...]]
    """
    file_prov = raw_manifest.file_provenance
    preimage = [
        str(file_prov.portfolio_id),
        str(file_prov.account_id),
        file_prov.source_key,
        file_prov.content_sha256,
        raw_manifest.manifest_sha256,
        parser_revision,
        [
            [
                rec.record_provenance.record_ordinal,
                rec.record_provenance.record_sha256,
                rec.parsed_sha256,
            ]
            for rec in parsed_records
        ],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedImportBatchManifest:
    """
    Immutable parsed batch manifest binding one raw ImportBatchManifest to complete, verified parsed records.
    """
    raw_manifest: ImportBatchManifest
    parser_revision: int
    parsed_records: Tuple[ParsedImportRecord, ...]
    parsed_manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.raw_manifest, ImportBatchManifest):
            raise PortfolioParsedImportBatchError(
                f"raw_manifest must be an ImportBatchManifest instance, got {type(self.raw_manifest).__name__}"
            )

        if isinstance(self.parser_revision, bool) or type(self.parser_revision) is not int or self.parser_revision < 1:
            raise PortfolioParsedImportBatchError(
                f"parser_revision must be a positive integer >= 1, got {self.parser_revision!r}"
            )

        if type(self.parsed_records) is not tuple:
            raise PortfolioParsedImportBatchError(
                f"parsed_records must be an immutable tuple, got {type(self.parsed_records).__name__}"
            )

        # Validate count equality
        if len(self.parsed_records) != len(self.raw_manifest.records):
            raise PortfolioParsedImportBatchError(
                f"parsed_records count {len(self.parsed_records)} does not match raw_manifest.records count {len(self.raw_manifest.records)}"
            )

        # Validate per-record provenance correspondence, revision, and sorting
        for i, rec in enumerate(self.parsed_records):
            if not isinstance(rec, ParsedImportRecord):
                raise PortfolioParsedImportBatchError(
                    f"parsed_records[{i}] must be a ParsedImportRecord instance, got {type(rec).__name__}"
                )
            if rec.parser_revision != self.parser_revision:
                raise PortfolioParsedImportBatchError(
                    f"parsed_records[{i}] parser_revision {rec.parser_revision} does not match batch parser_revision {self.parser_revision}"
                )
            expected_raw_prov = self.raw_manifest.records[i]
            if rec.record_provenance != expected_raw_prov:
                raise PortfolioParsedImportBatchError(
                    f"parsed_records[{i}] record_provenance does not match raw_manifest.records[{i}]"
                )
            expected_ordinal = i + 1
            if rec.record_provenance.record_ordinal != expected_ordinal:
                raise PortfolioParsedImportBatchError(
                    f"parsed_records must be sorted ascending 1..N: expected ordinal {expected_ordinal} at index {i}, got {rec.record_provenance.record_ordinal}"
                )

        # Validate parsed_manifest_sha256 format and matching
        if isinstance(self.parsed_manifest_sha256, bool) or not isinstance(self.parsed_manifest_sha256, str):
            raise PortfolioParsedImportBatchError(
                f"parsed_manifest_sha256 must be a str instance, got {type(self.parsed_manifest_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.parsed_manifest_sha256):
            raise PortfolioParsedImportBatchError(
                f"parsed_manifest_sha256 must be exactly 64 lowercase hexadecimal characters, got: {self.parsed_manifest_sha256!r}"
            )

        expected_sha = _compute_parsed_manifest_sha256(self.raw_manifest, self.parser_revision, self.parsed_records)
        if self.parsed_manifest_sha256 != expected_sha:
            raise PortfolioParsedImportBatchError(
                f"parsed_manifest_sha256 {self.parsed_manifest_sha256} does not match canonical preimage digest {expected_sha}"
            )

    @property
    def record_count(self) -> int:
        """Returns the number of parsed records in this batch."""
        return len(self.parsed_records)

    @property
    def parsed_manifest_identity(self) -> Tuple[UUID, UUID, str, str, str, int, str]:
        """
        Canonical staging parsed-batch manifest identity tuple:
        (portfolio_id, account_id, source_key, file_content_sha256, raw_manifest_sha256, parser_revision, parsed_manifest_sha256).
        """
        file_prov = self.raw_manifest.file_provenance
        return (
            file_prov.portfolio_id,
            file_prov.account_id,
            file_prov.source_key,
            file_prov.content_sha256,
            self.raw_manifest.manifest_sha256,
            self.parser_revision,
            self.parsed_manifest_sha256,
        )


def build_parsed_import_batch_manifest(
    raw_manifest: ImportBatchManifest,
    parser_revision: int,
    parsed_records: Sequence[ParsedImportRecord],
) -> ParsedImportBatchManifest:
    """
    Constructs an authoritative, verified, and canonically sorted ParsedImportBatchManifest.

    Args:
        raw_manifest: The authoritative raw ImportBatchManifest.
        parser_revision: Strict positive integer contract revision (>= 1).
        parsed_records: Materialized sequence of ParsedImportRecord objects covering the raw manifest.

    Returns:
        Immutable ParsedImportBatchManifest instance.

    Raises:
        PortfolioParsedImportBatchError: If coverage is incomplete, records mismatch, or revision differs.
    """
    if not isinstance(raw_manifest, ImportBatchManifest):
        raise PortfolioParsedImportBatchError(
            f"raw_manifest must be an ImportBatchManifest instance, got {type(raw_manifest).__name__}"
        )

    if isinstance(parser_revision, bool) or type(parser_revision) is not int or parser_revision < 1:
        raise PortfolioParsedImportBatchError(
            f"parser_revision must be a positive integer >= 1, got {parser_revision!r}"
        )

    if not isinstance(parsed_records, (list, tuple)) or isinstance(parsed_records, (str, bytes, bytearray, dict)):
        raise PortfolioParsedImportBatchError(
            f"parsed_records must be a materialized list or tuple, got {type(parsed_records).__name__}"
        )

    # Validate elements, parser revision, and check for duplicate ordinals
    seen_ordinals: set[int] = set()
    for i, rec in enumerate(parsed_records):
        if not isinstance(rec, ParsedImportRecord):
            raise PortfolioParsedImportBatchError(
                f"parsed_records[{i}] must be a ParsedImportRecord instance, got {type(rec).__name__}"
            )
        if rec.parser_revision != parser_revision:
            raise PortfolioParsedImportBatchError(
                f"parsed_records[{i}] parser_revision {rec.parser_revision} does not match batch parser_revision {parser_revision}"
            )
        ord_val = rec.record_provenance.record_ordinal
        if ord_val in seen_ordinals:
            raise PortfolioParsedImportBatchError(
                f"duplicate parsed record_ordinal detected: {ord_val}"
            )
        seen_ordinals.add(ord_val)

    # Validate count equality before sorting
    if len(parsed_records) != len(raw_manifest.records):
        raise PortfolioParsedImportBatchError(
            f"parsed_records count {len(parsed_records)} does not match raw_manifest.records count {len(raw_manifest.records)}"
        )

    # Canonically sort by record_ordinal ascending
    sorted_records = tuple(sorted(parsed_records, key=lambda r: r.record_provenance.record_ordinal))

    # Verify exact 1:1 correspondence with raw_manifest.records
    for i, rec in enumerate(sorted_records):
        expected_raw_prov = raw_manifest.records[i]
        if rec.record_provenance != expected_raw_prov:
            raise PortfolioParsedImportBatchError(
                f"parsed_records[{i}] record_provenance does not match raw_manifest.records[{i}]"
            )

    parsed_manifest_sha256 = _compute_parsed_manifest_sha256(raw_manifest, parser_revision, sorted_records)

    return ParsedImportBatchManifest(
        raw_manifest=raw_manifest,
        parser_revision=parser_revision,
        parsed_records=sorted_records,
        parsed_manifest_sha256=parsed_manifest_sha256,
    )
