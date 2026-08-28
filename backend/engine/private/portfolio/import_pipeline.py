"""
backend/engine/private/portfolio/import_pipeline.py
===================================================
Verified Source-Parser Execution Harness & Canonical Staging Pipeline (Phase 13E).

This module establishes the deterministic, parser-execution and composition boundary that binds:
- one raw file byte payload
- one source-specific parser adapter
- target portfolio and account UUIDs
- import timestamp

and composes the closed Phase 13A/B/C/D artifacts into an immutable staging result.

Key Architectural Invariants:
1. Explicit Parser Adapter Contract & Single-Snapshot Metadata:
   - Parser adapter supplies source_key and parser_revision.
   - Pipeline reads parser.source_key and parser.parser_revision exactly ONCE.
   - Caller cannot supply or override source_key or parser_revision.
2. Single Execution with Exact Original Payload:
   - parser.extract_records(content) is called exactly ONCE with the exact original bytes object.
   - No decoding, re-encoding, newline normalization, slicing, or duplication.
3. Logical Ordinal Assignment:
   - The sequence order returned by parser.extract_records defines 1-indexed record_ordinal 1..N.
   - Records are NOT sorted by content/hash and duplicate raw records are NOT deduplicated.
4. Closed Lower-Layer Integrity & Fail-Closed Error Propagation:
   - All provenance, batch, parsing, and parsed-batch manifests are constructed via closed Phase 13A-13D builders.
   - Lower-layer errors (PortfolioImportProvenanceError, PortfolioImportBatchError, PortfolioImportParsingError,
     PortfolioParsedImportBatchError) and parser exceptions propagate unchanged.
   - Pipeline error (PortfolioImportPipelineError) is reserved strictly for parser contract, collection,
     and pipeline input violations.
5. Ephemeral Output Isolation & Raw-Byte Non-Retention:
   - ExtractedImportRecord is an ephemeral intermediate DTO consumed immediately.
   - Final ImportStagingResult retains no raw file/record bytes, no ephemeral DTOs, and no parser references.
6. Absolute Financial & Ledger Separation:
   - Contains no PortfolioTransaction, no ledger appending, no instrument mapping, and no persistence logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Protocol, Sequence, Tuple, runtime_checkable
from uuid import UUID

from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.import_batch import (
    ImportBatchManifest,
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    ParsedImportBatchManifest,
    build_parsed_import_batch_manifest,
)

_SOURCE_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class PortfolioImportPipelineError(ValueError):
    """Raised when import pipeline parser contracts, metadata, or staging compositions fail closed."""
    pass


@dataclass(frozen=True)
class ExtractedImportRecord:
    """
    Ephemeral, immutable parser record output consumed immediately by the staging pipeline.
    """
    raw_record: bytes
    fields: Tuple[ImportParsedField, ...] = ()

    def __post_init__(self) -> None:
        if type(self.raw_record) is not bytes or isinstance(self.raw_record, (bytearray, memoryview)):
            raise PortfolioImportPipelineError(
                f"raw_record must be an immutable bytes instance, got {type(self.raw_record).__name__}"
            )
        if len(self.raw_record) == 0:
            raise PortfolioImportPipelineError("raw_record must not be empty")

        if type(self.fields) is not tuple:
            raise PortfolioImportPipelineError(
                f"fields must be an immutable tuple, got {type(self.fields).__name__}"
            )

        last_key: str | None = None
        for i, f in enumerate(self.fields):
            if not isinstance(f, ImportParsedField):
                raise PortfolioImportPipelineError(
                    f"fields[{i}] must be an ImportParsedField instance, got {type(f).__name__}"
                )
            if last_key is not None:
                if f.field_key == last_key:
                    raise PortfolioImportPipelineError(
                        f"duplicate field_key detected: {f.field_key}"
                    )
                if f.field_key < last_key:
                    raise PortfolioImportPipelineError(
                        f"fields must be sorted ascending by field_key: {f.field_key} followed {last_key}"
                    )
            last_key = f.field_key


@runtime_checkable
class PortfolioImportSourceParser(Protocol):
    """
    Narrow structural protocol defining the source-specific parser adapter contract.
    """
    @property
    def source_key(self) -> str:
        ...

    @property
    def parser_revision(self) -> int:
        ...

    def extract_records(
        self,
        content: bytes,
    ) -> Sequence[ExtractedImportRecord]:
        ...


@dataclass(frozen=True)
class ImportStagingResult:
    """
    Immutable, complete staging result combining file provenance, raw manifest, and parsed batch manifest.
    """
    file_provenance: ImportFileProvenance
    raw_manifest: ImportBatchManifest
    parsed_manifest: ParsedImportBatchManifest

    def __post_init__(self) -> None:
        if not isinstance(self.file_provenance, ImportFileProvenance):
            raise PortfolioImportPipelineError(
                f"file_provenance must be an ImportFileProvenance instance, got {type(self.file_provenance).__name__}"
            )
        if not isinstance(self.raw_manifest, ImportBatchManifest):
            raise PortfolioImportPipelineError(
                f"raw_manifest must be an ImportBatchManifest instance, got {type(self.raw_manifest).__name__}"
            )
        if not isinstance(self.parsed_manifest, ParsedImportBatchManifest):
            raise PortfolioImportPipelineError(
                f"parsed_manifest must be a ParsedImportBatchManifest instance, got {type(self.parsed_manifest).__name__}"
            )

        if self.raw_manifest.file_provenance != self.file_provenance:
            raise PortfolioImportPipelineError(
                "raw_manifest.file_provenance does not match file_provenance"
            )
        if self.parsed_manifest.raw_manifest != self.raw_manifest:
            raise PortfolioImportPipelineError(
                "parsed_manifest.raw_manifest does not match raw_manifest"
            )


def build_import_staging_result(
    portfolio_id: UUID,
    account_id: UUID,
    filename: str,
    content: bytes,
    imported_at: datetime,
    parser: PortfolioImportSourceParser,
) -> ImportStagingResult:
    """
    Executes a verified source parser against raw file bytes and deterministically composes
    closed Phase 13A/B/C/D staging manifests into an immutable ImportStagingResult.

    Args:
        portfolio_id: Target portfolio UUID.
        account_id: Target portfolio account UUID.
        filename: Display filename metadata.
        content: Exact raw file bytes.
        imported_at: Timezone-aware timestamp of import.
        parser: Source-parser adapter satisfying PortfolioImportSourceParser protocol.

    Returns:
        Immutable ImportStagingResult containing verified file provenance, raw manifest,
        and parsed batch manifest.

    Raises:
        PortfolioImportPipelineError: On malformed parser contracts, collections, or staging binding errors.
        PortfolioImportProvenanceError: On invalid file/record provenance inputs.
        PortfolioImportBatchError: On raw batch composition or contiguousness failures.
        PortfolioImportParsingError: On parsed record extraction binding failures.
        PortfolioParsedImportBatchError: On parsed batch coverage or revision failures.
    """
    if parser is None:
        raise PortfolioImportPipelineError("parser must not be None")

    # Capture parser metadata in a single snapshot read
    try:
        captured_source_key: Any = getattr(parser, "source_key")
    except AttributeError:
        raise PortfolioImportPipelineError("parser must provide 'source_key' property")
    except Exception as e:
        raise PortfolioImportPipelineError(f"Failed to read parser.source_key: {e}") from e

    try:
        captured_parser_revision: Any = getattr(parser, "parser_revision")
    except AttributeError:
        raise PortfolioImportPipelineError("parser must provide 'parser_revision' property")
    except Exception as e:
        raise PortfolioImportPipelineError(f"Failed to read parser.parser_revision: {e}") from e

    extract_fn = getattr(parser, "extract_records", None)
    if extract_fn is None or not callable(extract_fn):
        raise PortfolioImportPipelineError("parser must provide callable 'extract_records' method")

    # Validate parser metadata contracts
    if isinstance(captured_source_key, bool) or not isinstance(captured_source_key, str) or not _SOURCE_KEY_PATTERN.fullmatch(captured_source_key):
        raise PortfolioImportPipelineError(
            f"parser.source_key must be 1-64 ASCII lowercase alphanumeric characters or '._-', got: {captured_source_key!r}"
        )

    if isinstance(captured_parser_revision, bool) or type(captured_parser_revision) is not int or captured_parser_revision < 1:
        raise PortfolioImportPipelineError(
            f"parser.parser_revision must be a positive integer >= 1, got {captured_parser_revision!r}"
        )

    # 1. Authoritative File Provenance (Closed Phase 13A builder)
    file_provenance = build_import_file_provenance(
        portfolio_id=portfolio_id,
        account_id=account_id,
        source_key=captured_source_key,
        filename=filename,
        content=content,
        imported_at=imported_at,
    )

    # 2. Invoke parser exactly once with exact original content
    extracted_records = parser.extract_records(content)

    # 3. Validate parser output collection
    if not isinstance(extracted_records, (list, tuple)) or isinstance(extracted_records, (str, bytes, bytearray, dict)):
        raise PortfolioImportPipelineError(
            f"parser.extract_records must return a materialized list or tuple, got {type(extracted_records).__name__}"
        )

    for i, item in enumerate(extracted_records):
        if not isinstance(item, ExtractedImportRecord):
            raise PortfolioImportPipelineError(
                f"parser output element at index {i} must be an ExtractedImportRecord instance, got {type(item).__name__}"
            )

    # 4. Build Record Provenances in logical parser-return order (Closed Phase 13A builder)
    record_provenances = [
        build_import_record_provenance(
            file_provenance=file_provenance,
            record_ordinal=i + 1,
            raw_record=item.raw_record,
        )
        for i, item in enumerate(extracted_records)
    ]

    # 5. Build Raw Batch Manifest (Closed Phase 13B builder)
    raw_manifest = build_import_batch_manifest(
        file_provenance=file_provenance,
        records=record_provenances,
    )

    # 6. Build Parsed Import Records (Closed Phase 13C builder)
    parsed_records = [
        build_parsed_import_record(
            record_provenance=rec_prov,
            raw_record=item.raw_record,
            parser_revision=captured_parser_revision,
            fields=item.fields,
        )
        for rec_prov, item in zip(raw_manifest.records, extracted_records)
    ]

    # 7. Build Parsed Batch Manifest (Closed Phase 13D builder)
    parsed_manifest = build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=captured_parser_revision,
        parsed_records=parsed_records,
    )

    # 8. Return Immutable Complete Staging Result
    return ImportStagingResult(
        file_provenance=file_provenance,
        raw_manifest=raw_manifest,
        parsed_manifest=parsed_manifest,
    )
