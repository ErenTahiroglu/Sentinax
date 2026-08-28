"""
backend/engine/private/portfolio/import_assessment.py
====================================================
Immutable Import Interpretation Assessment & Batch Review Foundation (Phase 13G).

This module establishes the parser-neutral assessment layer that classifies each Phase 13C
ParsedImportRecord as READY, UNRESOLVED, or REJECTED, and cryptographically proves complete
one-to-one assessment coverage for an entire ParsedImportBatchManifest.

Key Architectural Invariants:
1. Pure Assessment Contract:
   - Evaluates textual parsed records without creating financial transaction drafts.
   - Assigns no transaction types, currencies, instruments, or accounting numbers.
   - Operates strictly as a pre-draft semantic triage and diagnostic staging boundary.
2. Strict Status Partitioning:
   - READY: Record has no diagnostics; eligible to proceed to future canonical transaction-draft stage.
   - UNRESOLVED: Record requires additional user/system resolution; must contain at least 1 diagnostic.
   - REJECTED: Record is ineligible for draft construction; must contain at least 1 diagnostic.
3. Diagnostic Lexical & Field Binding:
   - Diagnostic code: strict grammar ^[a-z][a-z0-9_]{0,63}$.
   - Diagnostic message: non-empty, non-whitespace-only, max 2048 chars.
   - Diagnostic field_key: None (record-level) or must exist in parsed_record.fields.
   - Unique (code, field_key) pairs per record assessment; canonically sorted ascending.
4. Complete One-to-One Batch Coverage:
   - For N parsed records, exactly N record assessments are required in canonical record order.
   - Omissions, extras, duplicates, or foreign parsed records fail closed immediately.
5. Deterministic Assessment Manifest Preimage & Digest:
   - assessment_manifest_sha256 is computed from compact JSON:
     [str(portfolio_id), str(account_id), source_key, file_content_sha256, raw_manifest_sha256,
      parser_revision, parsed_manifest_sha256, [[ord, parsed_sha, status, [[code, field_key, msg], ...]], ...]]
   - Timestamps, filenames, byte counts, and ledger external identities are strictly excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_parsed_batch import ParsedImportBatchManifest
from backend.engine.private.portfolio.import_parsing import ParsedImportRecord

_DIAGNOSTIC_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FIELD_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_DIAGNOSTIC_MESSAGE_LENGTH = 2048


class PortfolioImportAssessmentError(ValueError):
    """Raised when import assessment or assessment batch validation fails closed."""
    pass


class ImportAssessmentStatus(str, Enum):
    """Strict tri-state assessment status."""
    READY = "ready"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ImportAssessmentDiagnostic:
    """
    Immutable explanatory diagnostic staging metadata attached to an assessment.
    """
    code: str
    message: str
    field_key: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or not isinstance(self.code, str):
            raise PortfolioImportAssessmentError(
                f"code must be a str instance, got {type(self.code).__name__}"
            )
        if not _DIAGNOSTIC_CODE_PATTERN.fullmatch(self.code):
            raise PortfolioImportAssessmentError(
                f"code must match pattern '^[a-z][a-z0-9_]{{0,63}}$', got {self.code!r}"
            )

        if isinstance(self.message, bool) or not isinstance(self.message, str):
            raise PortfolioImportAssessmentError(
                f"message must be a str instance, got {type(self.message).__name__}"
            )
        if len(self.message) == 0 or len(self.message.strip()) == 0:
            raise PortfolioImportAssessmentError(
                "message must be a non-empty, non-whitespace-only string"
            )
        if len(self.message) > _MAX_DIAGNOSTIC_MESSAGE_LENGTH:
            raise PortfolioImportAssessmentError(
                f"message exceeds maximum length of {_MAX_DIAGNOSTIC_MESSAGE_LENGTH} characters, got {len(self.message)}"
            )

        if self.field_key is not None:
            if isinstance(self.field_key, bool) or not isinstance(self.field_key, str):
                raise PortfolioImportAssessmentError(
                    f"field_key must be None or a str instance, got {type(self.field_key).__name__}"
                )
            if not _FIELD_KEY_PATTERN.fullmatch(self.field_key):
                raise PortfolioImportAssessmentError(
                    f"field_key must match pattern '^[a-z][a-z0-9_]{{0,63}}$', got {self.field_key!r}"
                )


@dataclass(frozen=True)
class ImportRecordAssessment:
    """
    Immutable record-level assessment binding one ParsedImportRecord to status and diagnostics.
    """
    parsed_record: ParsedImportRecord
    status: ImportAssessmentStatus
    diagnostics: Tuple[ImportAssessmentDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.parsed_record, ParsedImportRecord):
            raise PortfolioImportAssessmentError(
                f"parsed_record must be a ParsedImportRecord instance, got {type(self.parsed_record).__name__}"
            )

        if isinstance(self.status, bool) or not isinstance(self.status, ImportAssessmentStatus):
            raise PortfolioImportAssessmentError(
                f"status must be an ImportAssessmentStatus enum member, got {self.status!r}"
            )

        if type(self.diagnostics) is not tuple:
            raise PortfolioImportAssessmentError(
                f"diagnostics must be an immutable tuple, got {type(self.diagnostics).__name__}"
            )

        parsed_field_keys: Set[str] = {f.field_key for f in self.parsed_record.fields}
        seen_identities: Set[Tuple[str, Optional[str]]] = set()

        for idx, diag in enumerate(self.diagnostics):
            if not isinstance(diag, ImportAssessmentDiagnostic):
                raise PortfolioImportAssessmentError(
                    f"Diagnostic at index {idx} must be an ImportAssessmentDiagnostic instance, got {type(diag).__name__}"
                )

            # Check canonical ordering: (code, field_key or "", message) ascending
            if idx > 0:
                prev = self.diagnostics[idx - 1]
                prev_key = (prev.code, prev.field_key or "", prev.message)
                curr_key = (diag.code, diag.field_key or "", diag.message)
                if curr_key < prev_key:
                    raise PortfolioImportAssessmentError(
                        f"Diagnostics must be canonically sorted by (code, field_key or '', message) ascending. "
                        f"Found {curr_key!r} after {prev_key!r} at index {idx}"
                    )

            # Check uniqueness of (code, field_key)
            diag_identity = (diag.code, diag.field_key)
            if diag_identity in seen_identities:
                raise PortfolioImportAssessmentError(
                    f"Duplicate diagnostic identity detected: code={diag.code!r}, field_key={diag.field_key!r}"
                )
            seen_identities.add(diag_identity)

            # Check field-key context against parsed_record.fields
            if diag.field_key is not None and diag.field_key not in parsed_field_keys:
                raise PortfolioImportAssessmentError(
                    f"Diagnostic references non-existent field_key {diag.field_key!r} in parsed record "
                    f"(available fields: {sorted(parsed_field_keys)})"
                )

        # Status vs diagnostics constraints
        if self.status == ImportAssessmentStatus.READY:
            if len(self.diagnostics) > 0:
                raise PortfolioImportAssessmentError(
                    f"READY assessment must not contain diagnostics, got {len(self.diagnostics)}"
                )
        else:
            if len(self.diagnostics) == 0:
                raise PortfolioImportAssessmentError(
                    f"{self.status.name} assessment must contain at least one diagnostic"
                )

    @property
    def record_ordinal(self) -> int:
        """Convenience property for record ordinal."""
        return self.parsed_record.record_provenance.record_ordinal

    @property
    def assessment_identity(self) -> Tuple[Any, ...]:
        """Deterministic tuple identity for record assessment."""
        return (
            *self.parsed_record.parsed_identity,
            self.status.value,
            tuple(
                (d.code, d.field_key, d.message)
                for d in self.diagnostics
            ),
        )


def build_import_record_assessment(
    parsed_record: ParsedImportRecord,
    status: ImportAssessmentStatus,
    diagnostics: Optional[Sequence[ImportAssessmentDiagnostic]] = None,
) -> ImportRecordAssessment:
    """
    Constructs an immutable ImportRecordAssessment, validating items and canonicalizing diagnostics.
    """
    if not isinstance(parsed_record, ParsedImportRecord):
        raise PortfolioImportAssessmentError(
            f"parsed_record must be a ParsedImportRecord instance, got {type(parsed_record).__name__}"
        )

    if isinstance(status, bool) or not isinstance(status, ImportAssessmentStatus):
        raise PortfolioImportAssessmentError(
            f"status must be an ImportAssessmentStatus enum member, got {status!r}"
        )

    if diagnostics is None:
        diag_seq: Sequence[ImportAssessmentDiagnostic] = ()
    elif type(diagnostics) in (list, tuple):
        diag_seq = diagnostics
    else:
        raise PortfolioImportAssessmentError(
            f"diagnostics must be a list or tuple if provided, got {type(diagnostics).__name__}"
        )

    seen_identities: Set[Tuple[str, Optional[str]]] = set()
    diag_list: List[ImportAssessmentDiagnostic] = []
    for idx, diag in enumerate(diag_seq):
        if not isinstance(diag, ImportAssessmentDiagnostic):
            raise PortfolioImportAssessmentError(
                f"Diagnostic at index {idx} must be an ImportAssessmentDiagnostic instance, got {type(diag).__name__}"
            )
        diag_identity = (diag.code, diag.field_key)
        if diag_identity in seen_identities:
            raise PortfolioImportAssessmentError(
                f"Duplicate diagnostic identity detected: code={diag.code!r}, field_key={diag.field_key!r}"
            )
        seen_identities.add(diag_identity)
        diag_list.append(diag)

    # Canonical sort: (code, field_key or "", message) ascending
    sorted_diagnostics = tuple(
        sorted(diag_list, key=lambda d: (d.code, d.field_key or "", d.message))
    )

    return ImportRecordAssessment(
        parsed_record=parsed_record,
        status=status,
        diagnostics=sorted_diagnostics,
    )


def _compute_assessment_manifest_sha256(
    parsed_manifest: ParsedImportBatchManifest,
    assessments: Tuple[ImportRecordAssessment, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for an assessment batch manifest preimage:
    [str(portfolio_id), str(account_id), source_key, file_content_sha256, raw_manifest_sha256,
     parser_revision, parsed_manifest_sha256, [[ord, parsed_sha, status, [[code, field_key, msg], ...]], ...]]
    """
    raw_manifest = parsed_manifest.raw_manifest
    file_prov = raw_manifest.file_provenance
    preimage = [
        str(file_prov.portfolio_id),
        str(file_prov.account_id),
        file_prov.source_key,
        file_prov.content_sha256,
        raw_manifest.manifest_sha256,
        parsed_manifest.parser_revision,
        parsed_manifest.parsed_manifest_sha256,
        [
            [
                a.parsed_record.record_provenance.record_ordinal,
                a.parsed_record.parsed_sha256,
                a.status.value,
                [
                    [d.code, d.field_key, d.message]
                    for d in a.diagnostics
                ],
            ]
            for a in assessments
        ],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImportAssessmentBatch:
    """
    Immutable assessment batch proving complete 1:1 assessment coverage for one ParsedImportBatchManifest.
    """
    parsed_manifest: ParsedImportBatchManifest
    assessments: Tuple[ImportRecordAssessment, ...]
    assessment_manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.parsed_manifest, ParsedImportBatchManifest):
            raise PortfolioImportAssessmentError(
                f"parsed_manifest must be a ParsedImportBatchManifest instance, got {type(self.parsed_manifest).__name__}"
            )

        if type(self.assessments) is not tuple:
            raise PortfolioImportAssessmentError(
                f"assessments must be an immutable tuple, got {type(self.assessments).__name__}"
            )

        if isinstance(self.assessment_manifest_sha256, bool) or not isinstance(self.assessment_manifest_sha256, str):
            raise PortfolioImportAssessmentError(
                f"assessment_manifest_sha256 must be a str instance, got {type(self.assessment_manifest_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.assessment_manifest_sha256):
            raise PortfolioImportAssessmentError(
                f"assessment_manifest_sha256 must be a 64-character lowercase hex string, got {self.assessment_manifest_sha256!r}"
            )

        # Coverage check: count
        if len(self.assessments) != self.parsed_manifest.record_count:
            raise PortfolioImportAssessmentError(
                f"Assessment count mismatch: expected {self.parsed_manifest.record_count} from parsed manifest, got {len(self.assessments)}"
            )

        # Exact 1:1 correspondence and ordering
        for i, assessment in enumerate(self.assessments):
            if not isinstance(assessment, ImportRecordAssessment):
                raise PortfolioImportAssessmentError(
                    f"Assessment at index {i} must be an ImportRecordAssessment instance, got {type(assessment).__name__}"
                )
            expected_parsed_rec = self.parsed_manifest.parsed_records[i]
            if assessment.parsed_record != expected_parsed_rec:
                raise PortfolioImportAssessmentError(
                    f"Assessment at index {i} parsed_record does not match parsed manifest record at index {i}. "
                    f"Expected ordinal {expected_parsed_rec.record_provenance.record_ordinal}, got {assessment.parsed_record.record_provenance.record_ordinal}"
                )

        # Digest validation
        computed_sha = _compute_assessment_manifest_sha256(self.parsed_manifest, self.assessments)
        if computed_sha != self.assessment_manifest_sha256:
            raise PortfolioImportAssessmentError(
                f"assessment_manifest_sha256 digest mismatch: computed {computed_sha}, declared {self.assessment_manifest_sha256}"
            )

    @property
    def record_count(self) -> int:
        """Total assessed records, identical to parsed_manifest.record_count."""
        return len(self.assessments)

    @property
    def ready_count(self) -> int:
        """Count of records assessed as READY."""
        return sum(1 for a in self.assessments if a.status == ImportAssessmentStatus.READY)

    @property
    def unresolved_count(self) -> int:
        """Count of records assessed as UNRESOLVED."""
        return sum(1 for a in self.assessments if a.status == ImportAssessmentStatus.UNRESOLVED)

    @property
    def rejected_count(self) -> int:
        """Count of records assessed as REJECTED."""
        return sum(1 for a in self.assessments if a.status == ImportAssessmentStatus.REJECTED)

    @property
    def portfolio_id(self) -> UUID:
        """Target portfolio UUID."""
        return self.parsed_manifest.raw_manifest.file_provenance.portfolio_id

    @property
    def account_id(self) -> UUID:
        """Target portfolio account UUID."""
        return self.parsed_manifest.raw_manifest.file_provenance.account_id

    @property
    def source_key(self) -> str:
        """Canonical source identifier."""
        return self.parsed_manifest.raw_manifest.file_provenance.source_key

    @property
    def file_content_sha256(self) -> str:
        """Source file content SHA-256."""
        return self.parsed_manifest.raw_manifest.file_provenance.content_sha256

    @property
    def raw_manifest_sha256(self) -> str:
        """Raw manifest SHA-256."""
        return self.parsed_manifest.raw_manifest.manifest_sha256

    @property
    def parser_revision(self) -> int:
        """Parser contract revision."""
        return self.parsed_manifest.parser_revision

    @property
    def parsed_manifest_sha256(self) -> str:
        """Parsed manifest SHA-256."""
        return self.parsed_manifest.parsed_manifest_sha256

    @property
    def assessment_manifest_identity(self) -> Tuple[UUID, UUID, str, str, str, int, str, str]:
        """Immutable composite identity tuple for the assessed batch."""
        return (
            *self.parsed_manifest.parsed_manifest_identity,
            self.assessment_manifest_sha256,
        )


def build_import_assessment_batch(
    parsed_manifest: ParsedImportBatchManifest,
    assessments: Sequence[ImportRecordAssessment],
) -> ImportAssessmentBatch:
    """
    Constructs an immutable ImportAssessmentBatch, enforcing complete 1:1 coverage and computing manifest hash.
    """
    if not isinstance(parsed_manifest, ParsedImportBatchManifest):
        raise PortfolioImportAssessmentError(
            f"parsed_manifest must be a ParsedImportBatchManifest instance, got {type(parsed_manifest).__name__}"
        )

    if type(assessments) not in (list, tuple):
        raise PortfolioImportAssessmentError(
            f"assessments must be a list or tuple, got {type(assessments).__name__}"
        )

    seen_ordinals: Set[int] = set()
    ass_list: List[ImportRecordAssessment] = []

    for idx, a in enumerate(assessments):
        if not isinstance(a, ImportRecordAssessment):
            raise PortfolioImportAssessmentError(
                f"Assessment at index {idx} must be an ImportRecordAssessment instance, got {type(a).__name__}"
            )
        ord_val = a.parsed_record.record_provenance.record_ordinal
        if ord_val in seen_ordinals:
            raise PortfolioImportAssessmentError(
                f"Duplicate record ordinal {ord_val} in assessment collection"
            )
        seen_ordinals.add(ord_val)
        ass_list.append(a)

    # Sort by record_ordinal ascending
    sorted_assessments = tuple(
        sorted(ass_list, key=lambda a: a.parsed_record.record_provenance.record_ordinal)
    )

    # Verify count
    if len(sorted_assessments) != parsed_manifest.record_count:
        raise PortfolioImportAssessmentError(
            f"Assessment count mismatch: expected {parsed_manifest.record_count} from parsed manifest, got {len(sorted_assessments)}"
        )

    # Verify exact 1:1 match against parsed_manifest.parsed_records
    for i, a in enumerate(sorted_assessments):
        expected_rec = parsed_manifest.parsed_records[i]
        if a.parsed_record != expected_rec:
            raise PortfolioImportAssessmentError(
                f"Assessment at index {i} does not match parsed record in manifest. "
                f"Expected ordinal {expected_rec.record_provenance.record_ordinal}, got {a.parsed_record.record_provenance.record_ordinal}"
            )

    manifest_sha = _compute_assessment_manifest_sha256(parsed_manifest, sorted_assessments)

    return ImportAssessmentBatch(
        parsed_manifest=parsed_manifest,
        assessments=sorted_assessments,
        assessment_manifest_sha256=manifest_sha,
    )
