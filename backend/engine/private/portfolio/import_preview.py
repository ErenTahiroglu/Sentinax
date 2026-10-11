"""
backend/engine/private/portfolio/import_preview.py
==================================================
Read-only PREVIEW of a Sentinax Canonical CSV v1 transaction-history import (Phase 25D1E6).

This is a thin, pure composition ABOVE the authoritative Phase 13 pipeline. It re-implements nothing:

    explicit immutable bytes
      -> build_import_staging_result      (Phase 13 parser + provenance + raw/parsed manifests)
      -> SentinaxCanonicalCsvSemanticInterpreterV1.interpret   (assessment + typed economic drafts)
      -> resolve_import_draft_batch_instruments                (PIT instrument resolution, caller-supplied resolver)

and projects the verified result into a strictly typed, versioned, JSON-safe review envelope.

Hard boundaries (each is pinned by tests):
- No persistence, no database, no network, no environment/clock/randomness, no log emission.
- No materialisation, no ledger binding, no commit RPC: this module does not import them. A preview NEVER
  authorises a commit; ``commit_authorized`` is always False.
- No ledger transaction identifiers or any other identifier is generated.
- The envelope carries no raw file bytes, no raw record text and no file name. Exception text is never
  echoed; file-level failures map to fixed codes by exception CLASS only.
- Decimal values are rendered as exact text (never floats); missing values are never replaced by zero,
  guessed, repaired or defaulted. Identical source records are never de-duplicated.
- Structurally valid does NOT mean economically valid, resolved, de-duplicated, idempotent or approved.

Only the Sentinax Canonical CSV v1 TRANSACTION-HISTORY format is supported. A current-holdings snapshot is a
different future feature and is never converted into transactions here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentStatus,
)
from backend.engine.private.portfolio.import_draft import ImportTransactionDraft
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolution,
    ImportInstrumentResolutionStatus,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
    resolve_import_draft_batch_instruments,
)
from backend.engine.private.portfolio.import_pipeline import build_import_staging_result
from backend.engine.private.portfolio.parsers.sentinax_csv import (
    SentinaxCanonicalCsvError,
    SentinaxCanonicalCsvParserV1,
)
from backend.engine.private.portfolio.parsers.sentinax_csv_semantics import (
    _CANONICAL_FIELD_KEYS,
    SentinaxCanonicalCsvSemanticError,
    SentinaxCanonicalCsvSemanticInterpreterV1,
)
from backend.engine.private.portfolio.import_provenance import PortfolioImportProvenanceError

PREVIEW_CONTRACT_VERSION = "sentinax_import_preview.v1"

# Fixed, closed vocabularies (safe to show; none is derived from file content).
RESOLUTION_NOT_EVALUATED = "not_evaluated"
_ALWAYS_BLOCKING = (
    "preview_only_no_commit_path",
    "idempotency_and_deduplication_not_evaluated",
    "owner_approval_not_granted",
)

_FILE_ERROR_FORMAT = "csv_format_invalid"
_FILE_ERROR_SCHEMA = "csv_schema_invalid"
_FILE_ERROR_PROVENANCE = "file_metadata_invalid"
_FILE_ERROR_OTHER = "file_rejected"


class ImportPreviewStatus(str, Enum):
    """File-level review state. None of these values means 'authorised to commit'."""
    FILE_INVALID = "file_invalid"
    EMPTY_VALID = "empty_valid"
    HAS_REJECTED_RECORDS = "has_rejected_records"
    RESOLUTION_UNAVAILABLE = "resolution_unavailable"
    NEEDS_RESOLUTION_REVIEW = "needs_resolution_review"
    STRUCTURALLY_PREVIEWABLE = "structurally_previewable"


@dataclass(frozen=True)
class ImportPreviewDiagnostic:
    code: str
    message: str
    field_key: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "field_key": self.field_key}


@dataclass(frozen=True)
class ImportPreviewRecord:
    record_ordinal: int
    assessment_status: str
    diagnostics: Tuple[ImportPreviewDiagnostic, ...]
    transaction_type: Optional[str]
    effective_date: Optional[str]
    executed_at: Optional[str]
    instrument_reference: Optional[str]
    quantity: Optional[str]
    unit_price: Optional[str]
    trade_currency: Optional[str]
    cash_amount: Optional[str]
    cash_currency: Optional[str]
    from_currency: Optional[str]
    from_amount: Optional[str]
    to_currency: Optional[str]
    to_amount: Optional[str]
    resolution_status: str
    resolution_diagnostic_codes: Tuple[str, ...]
    candidate_count: int
    identical_raw_record_of: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_ordinal": self.record_ordinal,
            "assessment_status": self.assessment_status,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "transaction_type": self.transaction_type,
            "effective_date": self.effective_date,
            "executed_at": self.executed_at,
            "instrument_reference": self.instrument_reference,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "trade_currency": self.trade_currency,
            "cash_amount": self.cash_amount,
            "cash_currency": self.cash_currency,
            "from_currency": self.from_currency,
            "from_amount": self.from_amount,
            "to_currency": self.to_currency,
            "to_amount": self.to_amount,
            "resolution_status": self.resolution_status,
            "resolution_diagnostic_codes": list(self.resolution_diagnostic_codes),
            "candidate_count": self.candidate_count,
            "identical_raw_record_of": self.identical_raw_record_of,
        }


@dataclass(frozen=True)
class ImportPreview:
    """Immutable, versioned, JSON-safe review envelope. Carries no raw bytes, file name or identifiers."""
    contract_version: str
    status: ImportPreviewStatus
    source_key: Optional[str]
    parser_revision: Optional[int]
    file_content_sha256: Optional[str]
    raw_manifest_sha256: Optional[str]
    parsed_manifest_sha256: Optional[str]
    assessment_manifest_sha256: Optional[str]
    draft_manifest_sha256: Optional[str]
    resolution_manifest_sha256: Optional[str]
    record_count: int
    ready_count: int
    rejected_count: int
    unresolved_count: int
    resolved_count: int
    not_required_count: int
    unresolved_instrument_count: int
    ambiguous_instrument_count: int
    resolver_key: Optional[str]
    resolver_revision: Optional[int]
    blocking_reasons: Tuple[str, ...]
    commit_authorized: bool
    persistent_state_changed: bool
    ledger_transaction_ids_generated: bool
    file_error_code: Optional[str]
    records: Tuple[ImportPreviewRecord, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "source_key": self.source_key,
            "parser_revision": self.parser_revision,
            "file_content_sha256": self.file_content_sha256,
            "raw_manifest_sha256": self.raw_manifest_sha256,
            "parsed_manifest_sha256": self.parsed_manifest_sha256,
            "assessment_manifest_sha256": self.assessment_manifest_sha256,
            "draft_manifest_sha256": self.draft_manifest_sha256,
            "resolution_manifest_sha256": self.resolution_manifest_sha256,
            "record_count": self.record_count,
            "ready_count": self.ready_count,
            "rejected_count": self.rejected_count,
            "unresolved_count": self.unresolved_count,
            "resolved_count": self.resolved_count,
            "not_required_count": self.not_required_count,
            "unresolved_instrument_count": self.unresolved_instrument_count,
            "ambiguous_instrument_count": self.ambiguous_instrument_count,
            "resolver_key": self.resolver_key,
            "resolver_revision": self.resolver_revision,
            "blocking_reasons": list(self.blocking_reasons),
            "commit_authorized": self.commit_authorized,
            "persistent_state_changed": self.persistent_state_changed,
            "ledger_transaction_ids_generated": self.ledger_transaction_ids_generated,
            "file_error_code": self.file_error_code,
            "records": [r.to_dict() for r in self.records],
        }


class _ResolverNotConnected(Exception):
    """Private marker raised by the placeholder resolver. Carries no message."""


class _NoPitResolver:
    """
    Placeholder used when the caller supplies no PIT resolver. It can never return candidates, so it can
    never cause any instrument to be reported as resolved; if consulted it only signals 'not connected'.
    """
    resolver_key = "none.not-connected"
    resolver_revision = 1

    def resolve_candidates(self, instrument_reference: str, as_of_date: date) -> Any:
        raise _ResolverNotConnected()


def _validate_caller_arguments(
    portfolio_id: Any, account_id: Any, filename: Any, content: Any, imported_at: Any
) -> None:
    if not isinstance(portfolio_id, UUID):
        raise TypeError("portfolio_id must be a UUID")
    if not isinstance(account_id, UUID):
        raise TypeError("account_id must be a UUID")
    if not isinstance(filename, str):
        raise TypeError("filename must be a str")
    if not filename.strip() or len(filename) > 255:
        raise ValueError("filename must be non-blank and at most 255 characters")
    if type(content) is not bytes:
        raise TypeError("content must be immutable bytes")
    if not isinstance(imported_at, datetime):
        raise TypeError("imported_at must be a datetime")
    if imported_at.tzinfo is None or imported_at.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")


def _header_is_canonical(content: bytes) -> bool:
    """
    Phase 13 checks the schema per record, so a file with ZERO records is never schema-checked there. A
    header-only file is only a valid empty import if its header is exactly the canonical 13-key set (the same
    set the Phase 13 interpreter requires of every record; column order is not significant, as in Phase 13).
    The parser has already accepted this header's syntax, newline style and UTF-8 encoding.
    """
    first_line = content.split(b"\n", 1)[0].rstrip(b"\r")
    try:
        rows = list(csv.reader([first_line.decode("utf-8")], delimiter=",", quotechar='"', strict=True))
    except (UnicodeDecodeError, csv.Error):
        return False
    return len(rows) == 1 and len(rows[0]) == len(_CANONICAL_FIELD_KEYS) and set(rows[0]) == _CANONICAL_FIELD_KEYS


def _dec(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, "f")


def _enum_value(value: Any) -> Optional[str]:
    return None if value is None else value.value


def _file_error_code(exc: Exception) -> str:
    if isinstance(exc, SentinaxCanonicalCsvError):
        return _FILE_ERROR_FORMAT
    if isinstance(exc, SentinaxCanonicalCsvSemanticError):
        return _FILE_ERROR_SCHEMA
    if isinstance(exc, PortfolioImportProvenanceError):
        return _FILE_ERROR_PROVENANCE
    return _FILE_ERROR_OTHER


def _invalid_file_preview(code: str) -> ImportPreview:
    return ImportPreview(
        contract_version=PREVIEW_CONTRACT_VERSION,
        status=ImportPreviewStatus.FILE_INVALID,
        source_key=None,
        parser_revision=None,
        file_content_sha256=None,
        raw_manifest_sha256=None,
        parsed_manifest_sha256=None,
        assessment_manifest_sha256=None,
        draft_manifest_sha256=None,
        resolution_manifest_sha256=None,
        record_count=0,
        ready_count=0,
        rejected_count=0,
        unresolved_count=0,
        resolved_count=0,
        not_required_count=0,
        unresolved_instrument_count=0,
        ambiguous_instrument_count=0,
        resolver_key=None,
        resolver_revision=None,
        blocking_reasons=("file_invalid",) + _ALWAYS_BLOCKING,
        commit_authorized=False,
        persistent_state_changed=False,
        ledger_transaction_ids_generated=False,
        file_error_code=code,
        records=(),
    )


def _project_record(
    assessment: Any,
    draft: Optional[ImportTransactionDraft],
    resolution: Optional[ImportInstrumentResolution],
    identical_to: Optional[int],
) -> ImportPreviewRecord:
    provenance = assessment.parsed_record.record_provenance
    diagnostics = tuple(
        ImportPreviewDiagnostic(code=d.code, message=d.message, field_key=d.field_key)
        for d in assessment.diagnostics
    )
    # Economic fields are shown only for READY records that have a typed draft. A rejected record shows no
    # economic value at all: nothing is zero-filled, guessed or repaired.
    shown = draft if assessment.status is ImportAssessmentStatus.READY else None
    if resolution is not None:
        res_status = resolution.status.value
        res_codes = tuple(d.code for d in resolution.diagnostics)
        candidates = len(resolution.candidate_instrument_ids)
    else:
        res_status = RESOLUTION_NOT_EVALUATED
        res_codes = ()
        candidates = 0
    return ImportPreviewRecord(
        record_ordinal=provenance.record_ordinal,
        assessment_status=assessment.status.value,
        diagnostics=diagnostics,
        transaction_type=_enum_value(shown.transaction_type) if shown else None,
        effective_date=shown.effective_date.isoformat() if shown else None,
        executed_at=shown.executed_at.isoformat() if shown and shown.executed_at else None,
        instrument_reference=shown.instrument_reference if shown else None,
        quantity=_dec(shown.quantity) if shown else None,
        unit_price=_dec(shown.unit_price) if shown else None,
        trade_currency=_enum_value(shown.trade_currency) if shown else None,
        cash_amount=_dec(shown.cash_amount) if shown else None,
        cash_currency=_enum_value(shown.cash_currency) if shown else None,
        from_currency=_enum_value(shown.from_currency) if shown else None,
        from_amount=_dec(shown.from_amount) if shown else None,
        to_currency=_enum_value(shown.to_currency) if shown else None,
        to_amount=_dec(shown.to_amount) if shown else None,
        resolution_status=res_status,
        resolution_diagnostic_codes=res_codes,
        candidate_count=candidates,
        identical_raw_record_of=identical_to,
    )


def build_sentinax_canonical_csv_import_preview(
    *,
    portfolio_id: UUID,
    account_id: UUID,
    filename: str,
    content: bytes,
    imported_at: datetime,
    resolver: Optional[PortfolioImportInstrumentResolver] = None,
) -> ImportPreview:
    """
    Build a read-only review envelope for Sentinax Canonical CSV v1 bytes.

    Caller mistakes (wrong argument types, naive timestamp) raise TypeError/ValueError. Anything wrong with the
    FILE itself yields a FILE_INVALID preview with a fixed error code. ``resolver=None`` means no PIT resolver
    is connected: instrument-bearing records are reported as not evaluated, never as resolved.
    """
    _validate_caller_arguments(portfolio_id, account_id, filename, content, imported_at)

    try:
        staging = build_import_staging_result(
            portfolio_id=portfolio_id,
            account_id=account_id,
            filename=filename,
            content=content,
            imported_at=imported_at,
            parser=SentinaxCanonicalCsvParserV1(),
        )
        draft_manifest = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(staging.parsed_manifest)
    except ValueError as exc:
        return _invalid_file_preview(_file_error_code(exc))

    assessment_batch = draft_manifest.assessment_batch
    if assessment_batch.record_count == 0 and not _header_is_canonical(content):
        return _invalid_file_preview(_FILE_ERROR_SCHEMA)

    resolution_batch = None
    resolution_failure: Optional[str] = None
    try:
        resolution_batch = resolve_import_draft_batch_instruments(
            draft_manifest=draft_manifest,
            resolver=_NoPitResolver() if resolver is None else resolver,
        )
    except _ResolverNotConnected:
        resolution_failure = "resolver_unavailable"
    except Exception:  # noqa: BLE001 - resolver text is untrusted; only a fixed code is reported
        resolution_failure = "resolver_error"

    drafts_by_ordinal = {d.record_ordinal: d for d in draft_manifest.drafts}
    resolutions_by_ordinal = (
        {r.draft.record_ordinal: r for r in resolution_batch.resolutions} if resolution_batch else {}
    )

    first_ordinal_by_record_sha: Dict[str, int] = {}
    records: List[ImportPreviewRecord] = []
    for assessment in assessment_batch.assessments:
        provenance = assessment.parsed_record.record_provenance
        ordinal = provenance.record_ordinal
        identical_to = first_ordinal_by_record_sha.get(provenance.record_sha256)
        first_ordinal_by_record_sha.setdefault(provenance.record_sha256, ordinal)
        records.append(
            _project_record(assessment, drafts_by_ordinal.get(ordinal), resolutions_by_ordinal.get(ordinal), identical_to)
        )

    unresolved_instrument = ambiguous_instrument = resolved = not_required = 0
    resolver_key: Optional[str] = None
    resolver_revision: Optional[int] = None
    if resolution_batch is not None:
        resolved = resolution_batch.resolved_count
        not_required = resolution_batch.not_required_count
        unresolved_instrument = resolution_batch.unresolved_count
        ambiguous_instrument = resolution_batch.ambiguous_count
        for r in resolution_batch.resolutions:
            if r.status is not ImportInstrumentResolutionStatus.NOT_REQUIRED:
                resolver_key, resolver_revision = r.resolver_key, r.resolver_revision
                break

    record_count = assessment_batch.record_count
    rejected = assessment_batch.rejected_count
    assessment_unresolved = assessment_batch.unresolved_count

    blocking: List[str] = []
    if record_count == 0:
        blocking.append("no_records")
    if rejected:
        blocking.append("rejected_records")
    if assessment_unresolved:
        blocking.append("unresolved_records")
    if resolution_failure is not None:
        blocking.append(resolution_failure)
    if unresolved_instrument:
        blocking.append("unresolved_instruments")
    if ambiguous_instrument:
        blocking.append("ambiguous_instruments")
    blocking.extend(_ALWAYS_BLOCKING)

    if record_count == 0:
        status = ImportPreviewStatus.EMPTY_VALID
    elif rejected or assessment_unresolved:
        status = ImportPreviewStatus.HAS_REJECTED_RECORDS
    elif resolution_failure is not None:
        status = ImportPreviewStatus.RESOLUTION_UNAVAILABLE
    elif unresolved_instrument or ambiguous_instrument:
        status = ImportPreviewStatus.NEEDS_RESOLUTION_REVIEW
    else:
        status = ImportPreviewStatus.STRUCTURALLY_PREVIEWABLE

    return ImportPreview(
        contract_version=PREVIEW_CONTRACT_VERSION,
        status=status,
        source_key=assessment_batch.source_key,
        parser_revision=assessment_batch.parser_revision,
        file_content_sha256=assessment_batch.file_content_sha256,
        raw_manifest_sha256=assessment_batch.raw_manifest_sha256,
        parsed_manifest_sha256=assessment_batch.parsed_manifest_sha256,
        assessment_manifest_sha256=assessment_batch.assessment_manifest_sha256,
        draft_manifest_sha256=draft_manifest.draft_manifest_sha256,
        resolution_manifest_sha256=resolution_batch.resolution_manifest_sha256 if resolution_batch else None,
        record_count=record_count,
        ready_count=assessment_batch.ready_count,
        rejected_count=rejected,
        unresolved_count=assessment_unresolved,
        resolved_count=resolved,
        not_required_count=not_required,
        unresolved_instrument_count=unresolved_instrument,
        ambiguous_instrument_count=ambiguous_instrument,
        resolver_key=resolver_key,
        resolver_revision=resolver_revision,
        blocking_reasons=tuple(blocking),
        commit_authorized=False,
        persistent_state_changed=False,
        ledger_transaction_ids_generated=False,
        file_error_code=None,
        records=tuple(records),
    )
