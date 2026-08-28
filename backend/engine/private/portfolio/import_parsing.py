"""
backend/engine/private/portfolio/import_parsing.py
==================================================
Parser-Neutral Record Extraction Contract & Raw-Byte Binding (Phase 13C).

This module defines the immutable parser-output contract for individual raw import records,
cryptographically binding extracted textual fields to exact Phase 13A record provenance and
explicit parser revisions.

Key Architectural Invariants:
1. Cryptographic Raw-Byte Binding:
   - Builders MUST verify sha256(raw_record) == record_provenance.record_sha256 before accepting
     any extracted parser fields.
   - Raw record bytes are never retained in parsed models.
2. Explicit Parser Revision:
   - parser_revision is a strict positive integer (>= 1).
   - Variations in parser revision change parsed_sha256 and parsed_identity.
3. String-Only Textual Field Representation:
   - ImportParsedField stores exact source strings without numeric, date, or currency coercion.
   - Whitespace and Unicode characters are strictly preserved without automatic normalization.
   - Empty values are represented as ImportParsedField(key, ""), distinct from absent fields.
4. Canonical Field Ordering & Unique Key Contract:
   - Parsed fields are canonically sorted ascending by field_key.
   - Duplicate field keys fail closed immediately without silent overwriting.
5. Deterministic Parsed Preimage & Digest:
   - parsed_sha256 is computed from compact JSON:
     [portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256, parser_revision, [[key, value], ...]]
   - Filenames, timestamps, byte lengths, and ledger identity fields are strictly excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Sequence, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_provenance import ImportRecordProvenance

_FIELD_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_FIELD_VALUE_LENGTH = 16384


class PortfolioImportParsingError(ValueError):
    """Raised when parser record extraction contracts or raw-byte bindings fail closed."""
    pass


@dataclass(frozen=True)
class ImportParsedField:
    """
    Immutable, textual extracted field preserving exact source representation.
    """
    field_key: str
    field_value: str

    def __post_init__(self) -> None:
        if isinstance(self.field_key, bool) or not isinstance(self.field_key, str):
            raise PortfolioImportParsingError(
                f"field_key must be a str instance, got {type(self.field_key).__name__}"
            )
        if not _FIELD_KEY_PATTERN.fullmatch(self.field_key):
            raise PortfolioImportParsingError(
                f"field_key must match pattern '^[a-z][a-z0-9_]{{0,63}}$', got: {self.field_key!r}"
            )

        if isinstance(self.field_value, bool) or not isinstance(self.field_value, str):
            raise PortfolioImportParsingError(
                f"field_value must be a str instance, got {type(self.field_value).__name__}"
            )
        if len(self.field_value) > _MAX_FIELD_VALUE_LENGTH:
            raise PortfolioImportParsingError(
                f"field_value exceeds maximum length of {_MAX_FIELD_VALUE_LENGTH} characters, got {len(self.field_value)}"
            )


def _compute_parsed_sha256(
    record_provenance: ImportRecordProvenance,
    parser_revision: int,
    fields: Tuple[ImportParsedField, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for a parsed import record preimage:
    [str(portfolio_id), str(account_id), source_key, file_content_sha256, record_ordinal, record_sha256, parser_revision, [[key, value], ...]]
    """
    file_id = record_provenance.file_identity
    preimage = [
        str(file_id[0]),
        str(file_id[1]),
        file_id[2],
        file_id[3],
        record_provenance.record_ordinal,
        record_provenance.record_sha256,
        parser_revision,
        [[f.field_key, f.field_value] for f in fields],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedImportRecord:
    """
    Immutable parsed record binding textual fields to exact record provenance and parser revision.
    """
    record_provenance: ImportRecordProvenance
    parser_revision: int
    fields: Tuple[ImportParsedField, ...]
    parsed_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.record_provenance, ImportRecordProvenance):
            raise PortfolioImportParsingError(
                f"record_provenance must be an ImportRecordProvenance instance, got {type(self.record_provenance).__name__}"
            )

        if isinstance(self.parser_revision, bool) or type(self.parser_revision) is not int or self.parser_revision < 1:
            raise PortfolioImportParsingError(
                f"parser_revision must be a positive integer >= 1, got {self.parser_revision!r}"
            )

        if type(self.fields) is not tuple:
            raise PortfolioImportParsingError(
                f"fields must be an immutable tuple, got {type(self.fields).__name__}"
            )

        # Validate fields and verify strictly sorted unique keys
        last_key: str | None = None
        for i, f in enumerate(self.fields):
            if not isinstance(f, ImportParsedField):
                raise PortfolioImportParsingError(
                    f"fields[{i}] must be an ImportParsedField instance, got {type(f).__name__}"
                )
            if last_key is not None:
                if f.field_key == last_key:
                    raise PortfolioImportParsingError(
                        f"duplicate field_key detected: {f.field_key}"
                    )
                if f.field_key < last_key:
                    raise PortfolioImportParsingError(
                        f"fields must be sorted ascending by field_key: {f.field_key} followed {last_key}"
                    )
            last_key = f.field_key

        # Validate parsed_sha256 format and matching
        if isinstance(self.parsed_sha256, bool) or not isinstance(self.parsed_sha256, str):
            raise PortfolioImportParsingError(
                f"parsed_sha256 must be a str instance, got {type(self.parsed_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.parsed_sha256):
            raise PortfolioImportParsingError(
                f"parsed_sha256 must be exactly 64 lowercase hexadecimal characters, got: {self.parsed_sha256!r}"
            )

        expected_sha = _compute_parsed_sha256(self.record_provenance, self.parser_revision, self.fields)
        if self.parsed_sha256 != expected_sha:
            raise PortfolioImportParsingError(
                f"parsed_sha256 {self.parsed_sha256} does not match canonical preimage digest {expected_sha}"
            )

    @property
    def source_key(self) -> str:
        """Derived source key from authoritative record provenance."""
        return self.record_provenance.file_identity[2]

    @property
    def parsed_identity(self) -> Tuple[UUID, UUID, str, str, int, str, int, str]:
        """
        Canonical staging parsed identity tuple:
        (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256, parser_revision, parsed_sha256).
        """
        file_id = self.record_provenance.file_identity
        return (
            file_id[0],
            file_id[1],
            file_id[2],
            file_id[3],
            self.record_provenance.record_ordinal,
            self.record_provenance.record_sha256,
            self.parser_revision,
            self.parsed_sha256,
        )


def build_parsed_import_record(
    record_provenance: ImportRecordProvenance,
    raw_record: bytes,
    parser_revision: int,
    fields: Sequence[ImportParsedField],
) -> ParsedImportRecord:
    """
    Constructs an authoritative, verified, and canonically sorted ParsedImportRecord.

    Args:
        record_provenance: Authoritative ImportRecordProvenance.
        raw_record: Exact raw record bytes to cryptographically verify against record_provenance.
        parser_revision: Strict positive integer contract revision (>= 1).
        fields: Materialized list or tuple of extracted ImportParsedField objects.

    Returns:
        Immutable ParsedImportRecord instance.

    Raises:
        PortfolioImportParsingError: If raw bytes mismatch provenance SHA, or if fields/revision are invalid.
    """
    if not isinstance(record_provenance, ImportRecordProvenance):
        raise PortfolioImportParsingError(
            f"record_provenance must be an ImportRecordProvenance instance, got {type(record_provenance).__name__}"
        )

    if isinstance(raw_record, (str, bytearray, memoryview)) or type(raw_record) is not bytes:
        raise PortfolioImportParsingError(
            f"raw_record must be exact immutable bytes, got {type(raw_record).__name__}"
        )
    if len(raw_record) == 0:
        raise PortfolioImportParsingError("raw_record bytes cannot be empty")

    computed_raw_sha = hashlib.sha256(raw_record).hexdigest()
    if computed_raw_sha != record_provenance.record_sha256:
        raise PortfolioImportParsingError(
            f"raw_record SHA-256 {computed_raw_sha} does not match record_provenance.record_sha256 {record_provenance.record_sha256}"
        )

    if isinstance(parser_revision, bool) or type(parser_revision) is not int or parser_revision < 1:
        raise PortfolioImportParsingError(
            f"parser_revision must be a positive integer >= 1, got {parser_revision!r}"
        )

    if not isinstance(fields, (list, tuple)) or isinstance(fields, (str, bytes, bytearray, dict)):
        raise PortfolioImportParsingError(
            f"fields must be a materialized list or tuple, got {type(fields).__name__}"
        )

    # Detect duplicate field keys explicitly without silent dict/set overwrite
    seen_keys: set[str] = set()
    for i, f in enumerate(fields):
        if not isinstance(f, ImportParsedField):
            raise PortfolioImportParsingError(
                f"fields[{i}] must be an ImportParsedField instance, got {type(f).__name__}"
            )
        if f.field_key in seen_keys:
            raise PortfolioImportParsingError(
                f"duplicate field_key detected: {f.field_key}"
            )
        seen_keys.add(f.field_key)

    # Canonically sort by field_key ascending
    sorted_fields = tuple(sorted(fields, key=lambda f: f.field_key))

    parsed_sha256 = _compute_parsed_sha256(record_provenance, parser_revision, sorted_fields)

    return ParsedImportRecord(
        record_provenance=record_provenance,
        parser_revision=parser_revision,
        fields=sorted_fields,
        parsed_sha256=parsed_sha256,
    )
