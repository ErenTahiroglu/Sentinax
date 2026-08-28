"""
backend/engine/private/portfolio/import_instrument_resolution.py
================================================================
Immutable PIT-Safe Instrument Resolution Outcome & Complete Draft-Coverage Manifest (Phase 13J).

This module establishes the authoritative instrument-resolution outcome contract that a future
resolver execution layer must produce for every Phase 13I economic transaction draft.

Key Architectural Invariants:
1. Four-State Closed Outcome:
   - Every draft must explicitly end in exactly one state:
     * NOT_REQUIRED: Legitimate non-instrument event (e.g. simple cash deposit/withdrawal, FX).
     * RESOLVED: Exactly one canonical instrument UUID resolved.
     * UNRESOLVED: Instrument reference exists but no canonical instrument could be established.
     * AMBIGUOUS: Instrument reference exists with multiple (>= 2) canonical candidates.
   - Zero missing state. Zero implicit default.
2. Point-In-Time (PIT) Date Binding:
   - resolution_as_of_date must be an exact built-in date instance.
   - MUST equal draft.effective_date exactly. No caller override, no date.today() default.
3. Resolver Key & Revision Pairing:
   - When resolution is attempted, resolver_key and resolver_revision must be present together.
   - NOT_REQUIRED must have both as None.
4. Complete Batch Coverage:
   - For N drafts in ImportDraftBatchManifest, exactly N resolution outcomes are required.
   - Canonical ordering by draft.record_ordinal ascending.
   - One-to-one semantic correspondence with draft_manifest.drafts.
5. Deterministic Cryptographic Digests:
   - resolution_sha256 binds to draft_sha256, record_ordinal, instrument_reference,
     resolution_as_of_date, status, resolver_key, resolver_revision, instrument_id,
     canonical candidate UUIDs, and canonical diagnostics.
   - resolution_manifest_sha256 binds to full draft manifest metadata and all resolution records.
6. Pure Domain & Pre-Ledger Boundary:
   - Pure Python only: no network, DB, file I/O, or identity service execution.
   - Zero PortfolioTransaction, zero external identity derivation, zero persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import re
from typing import Any, List, Optional, Sequence, Set, Tuple, Union
from uuid import UUID

from backend.engine.private.domain import TransactionType
from backend.engine.private.portfolio.import_draft import ImportTransactionDraft
from backend.engine.private.portfolio.import_draft_batch import ImportDraftBatchManifest

_DIAGNOSTIC_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESOLVER_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PortfolioImportInstrumentResolutionError(ValueError):
    """Raised when instrument resolution outcome or batch validation fails closed."""
    pass


class ImportInstrumentResolutionStatus(str, Enum):
    """Authoritative four-state instrument resolution outcome."""
    NOT_REQUIRED = "not_required"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ImportInstrumentResolutionDiagnostic:
    """
    Immutable resolution diagnostic for UNRESOLVED and AMBIGUOUS outcomes.
    """
    code: str
    message: str

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or type(self.code) is not str:
            raise PortfolioImportInstrumentResolutionError(
                f"Diagnostic code must be a str instance, got {type(self.code).__name__}"
            )
        if not _DIAGNOSTIC_CODE_PATTERN.fullmatch(self.code):
            raise PortfolioImportInstrumentResolutionError(
                f"Diagnostic code must match '^[a-z][a-z0-9_]{{0,63}}$', got {self.code!r}"
            )

        if isinstance(self.message, bool) or type(self.message) is not str:
            raise PortfolioImportInstrumentResolutionError(
                f"Diagnostic message must be a str instance, got {type(self.message).__name__}"
            )
        if len(self.message) < 1 or len(self.message) > 2048:
            raise PortfolioImportInstrumentResolutionError(
                f"Diagnostic message length must be between 1 and 2048, got {len(self.message)}"
            )
        if not self.message.strip():
            raise PortfolioImportInstrumentResolutionError(
                "Diagnostic message must not be empty or whitespace-only"
            )


def _compute_resolution_sha256(
    draft: ImportTransactionDraft,
    status: ImportInstrumentResolutionStatus,
    resolution_as_of_date: date,
    resolver_key: Optional[str],
    resolver_revision: Optional[int],
    instrument_id: Optional[UUID],
    candidate_instrument_ids: Tuple[UUID, ...],
    diagnostics: Tuple[ImportInstrumentResolutionDiagnostic, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for an instrument resolution record:
    [
      draft.draft_sha256,
      draft.record_ordinal,
      draft.instrument_reference,
      resolution_as_of_date.isoformat(),
      status.value,
      resolver_key,
      resolver_revision,
      str(instrument_id) if instrument_id is not None else None,
      [str(u) for u in candidate_instrument_ids],
      [[d.code, d.message] for d in diagnostics]
    ]
    """
    preimage: Any = [
        draft.draft_sha256,
        draft.record_ordinal,
        draft.instrument_reference,
        resolution_as_of_date.isoformat(),
        status.value,
        resolver_key,
        resolver_revision,
        str(instrument_id) if instrument_id is not None else None,
        [str(u) for u in candidate_instrument_ids],
        [[d.code, d.message] for d in diagnostics],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


def _validate_resolution_record_fields(
    draft: ImportTransactionDraft,
    status: ImportInstrumentResolutionStatus,
    resolution_as_of_date: date,
    resolver_key: Optional[str],
    resolver_revision: Optional[int],
    instrument_id: Optional[UUID],
    candidate_instrument_ids: Tuple[UUID, ...],
    diagnostics: Tuple[ImportInstrumentResolutionDiagnostic, ...],
) -> None:
    """Independently validates all record-level resolution fields and eligibility rules."""
    # 1. Draft type check
    if not isinstance(draft, ImportTransactionDraft):
        raise PortfolioImportInstrumentResolutionError(
            f"draft must be an ImportTransactionDraft instance, got {type(draft).__name__}"
        )

    # 2. Status type check (strictly Enum instance)
    if isinstance(status, bool) or not isinstance(status, ImportInstrumentResolutionStatus):
        raise PortfolioImportInstrumentResolutionError(
            f"status must be an ImportInstrumentResolutionStatus enum member, got {type(status).__name__}: {status!r}"
        )

    # 3. PIT date check: strict date instance (reject datetime) and must match draft.effective_date
    if type(resolution_as_of_date) is not date:
        raise PortfolioImportInstrumentResolutionError(
            f"resolution_as_of_date must be strictly a datetime.date instance, got {type(resolution_as_of_date).__name__}: {resolution_as_of_date!r}"
        )
    if resolution_as_of_date != draft.effective_date:
        raise PortfolioImportInstrumentResolutionError(
            f"resolution_as_of_date ({resolution_as_of_date}) must equal draft.effective_date ({draft.effective_date})"
        )

    # 4. Resolver key / revision pairing and contracts
    if resolver_key is None and resolver_revision is not None:
        raise PortfolioImportInstrumentResolutionError(
            "resolver_revision specified without resolver_key. Resolver metadata must be all-or-none."
        )
    if resolver_key is not None and resolver_revision is None:
        raise PortfolioImportInstrumentResolutionError(
            "resolver_key specified without resolver_revision. Resolver metadata must be all-or-none."
        )

    if resolver_key is not None:
        if isinstance(resolver_key, bool) or type(resolver_key) is not str:
            raise PortfolioImportInstrumentResolutionError(
                f"resolver_key must be a str instance, got {type(resolver_key).__name__}"
            )
        if not _RESOLVER_KEY_PATTERN.fullmatch(resolver_key):
            raise PortfolioImportInstrumentResolutionError(
                f"resolver_key must match '^[a-z0-9][a-z0-9._-]{{0,63}}$', got {resolver_key!r}"
            )

    if resolver_revision is not None:
        if isinstance(resolver_revision, bool) or type(resolver_revision) is not int:
            raise PortfolioImportInstrumentResolutionError(
                f"resolver_revision must be an int instance, got {type(resolver_revision).__name__}"
            )
        if resolver_revision < 1:
            raise PortfolioImportInstrumentResolutionError(
                f"resolver_revision must be >= 1, got {resolver_revision}"
            )

    # 5. Candidate UUIDs collection check (exact tuple, unique, sorted ascending by str(u))
    if type(candidate_instrument_ids) is not tuple:
        raise PortfolioImportInstrumentResolutionError(
            f"candidate_instrument_ids must be an immutable tuple, got {type(candidate_instrument_ids).__name__}"
        )
    seen_candidates: Set[UUID] = set()
    prev_candidate_str: str = ""
    for idx, cand in enumerate(candidate_instrument_ids):
        if not isinstance(cand, UUID):
            raise PortfolioImportInstrumentResolutionError(
                f"candidate_instrument_ids[{idx}] must be a UUID instance, got {type(cand).__name__}"
            )
        if cand in seen_candidates:
            raise PortfolioImportInstrumentResolutionError(
                f"Duplicate candidate UUID detected in candidate_instrument_ids: {cand}"
            )
        seen_candidates.add(cand)

        cand_str = str(cand)
        if cand_str <= prev_candidate_str:
            raise PortfolioImportInstrumentResolutionError(
                f"candidate_instrument_ids is not canonically sorted by str(uuid) ascending at index {idx}"
            )
        prev_candidate_str = cand_str

    # 6. Diagnostics collection check (exact tuple, unique codes, sorted ascending by (code, message))
    if type(diagnostics) is not tuple:
        raise PortfolioImportInstrumentResolutionError(
            f"diagnostics must be an immutable tuple, got {type(diagnostics).__name__}"
        )
    seen_diag_codes: Set[str] = set()
    prev_diag_key: Tuple[str, str] = ("", "")
    for idx, diag in enumerate(diagnostics):
        if not isinstance(diag, ImportInstrumentResolutionDiagnostic):
            raise PortfolioImportInstrumentResolutionError(
                f"diagnostics[{idx}] must be an ImportInstrumentResolutionDiagnostic instance, got {type(diag).__name__}"
            )
        if diag.code in seen_diag_codes:
            raise PortfolioImportInstrumentResolutionError(
                f"Duplicate diagnostic code detected in diagnostics: {diag.code!r}"
            )
        seen_diag_codes.add(diag.code)

        diag_key = (diag.code, diag.message)
        if diag_key <= prev_diag_key:
            raise PortfolioImportInstrumentResolutionError(
                f"diagnostics is not canonically sorted by (code, message) ascending at index {idx}"
            )
        prev_diag_key = diag_key

    # 7. Transaction type eligibility & status-specific rules
    tx_type = draft.transaction_type

    if status == ImportInstrumentResolutionStatus.NOT_REQUIRED:
        # NOT_REQUIRED is valid ONLY when draft has NO instrument_reference
        if draft.instrument_reference is not None:
            raise PortfolioImportInstrumentResolutionError(
                f"NOT_REQUIRED status is invalid for draft with instrument_reference {draft.instrument_reference!r}"
            )
        if tx_type in (TransactionType.BUY, TransactionType.SELL):
            raise PortfolioImportInstrumentResolutionError(
                f"NOT_REQUIRED status is invalid for {tx_type.name} transactions"
            )

        if resolver_key is not None or resolver_revision is not None:
            raise PortfolioImportInstrumentResolutionError(
                "resolver_key and resolver_revision must be None for NOT_REQUIRED status"
            )
        if instrument_id is not None:
            raise PortfolioImportInstrumentResolutionError(
                "instrument_id must be None for NOT_REQUIRED status"
            )
        if candidate_instrument_ids != ():
            raise PortfolioImportInstrumentResolutionError(
                "candidate_instrument_ids must be empty for NOT_REQUIRED status"
            )
        if diagnostics != ():
            raise PortfolioImportInstrumentResolutionError(
                "diagnostics must be empty for NOT_REQUIRED status"
            )

    elif status == ImportInstrumentResolutionStatus.RESOLVED:
        if draft.instrument_reference is None:
            raise PortfolioImportInstrumentResolutionError(
                "RESOLVED status requires draft to have an instrument_reference"
            )
        if tx_type in (
            TransactionType.CASH_DEPOSIT,
            TransactionType.CASH_WITHDRAWAL,
            TransactionType.FX_CONVERSION,
        ):
            raise PortfolioImportInstrumentResolutionError(
                f"RESOLVED status is invalid for {tx_type.name} transactions"
            )

        if resolver_key is None or resolver_revision is None:
            raise PortfolioImportInstrumentResolutionError(
                "RESOLVED status requires resolver_key and resolver_revision metadata"
            )
        if instrument_id is None or not isinstance(instrument_id, UUID):
            raise PortfolioImportInstrumentResolutionError(
                f"RESOLVED status requires an authoritative UUID instrument_id, got {type(instrument_id).__name__}"
            )
        if candidate_instrument_ids != ():
            raise PortfolioImportInstrumentResolutionError(
                "RESOLVED status must not carry candidate_instrument_ids"
            )
        if diagnostics != ():
            raise PortfolioImportInstrumentResolutionError(
                "RESOLVED status must not carry diagnostics"
            )

    elif status == ImportInstrumentResolutionStatus.UNRESOLVED:
        if draft.instrument_reference is None:
            raise PortfolioImportInstrumentResolutionError(
                "UNRESOLVED status requires draft to have an instrument_reference"
            )
        if tx_type in (
            TransactionType.CASH_DEPOSIT,
            TransactionType.CASH_WITHDRAWAL,
            TransactionType.FX_CONVERSION,
        ):
            raise PortfolioImportInstrumentResolutionError(
                f"UNRESOLVED status is invalid for {tx_type.name} transactions"
            )

        if resolver_key is None or resolver_revision is None:
            raise PortfolioImportInstrumentResolutionError(
                "UNRESOLVED status requires resolver_key and resolver_revision metadata"
            )
        if instrument_id is not None:
            raise PortfolioImportInstrumentResolutionError(
                "UNRESOLVED status must not carry an instrument_id"
            )
        if candidate_instrument_ids != ():
            raise PortfolioImportInstrumentResolutionError(
                "UNRESOLVED status must not carry candidate_instrument_ids (use AMBIGUOUS for candidates)"
            )
        if len(diagnostics) == 0:
            raise PortfolioImportInstrumentResolutionError(
                "UNRESOLVED status requires at least one diagnostic"
            )

    elif status == ImportInstrumentResolutionStatus.AMBIGUOUS:
        if draft.instrument_reference is None:
            raise PortfolioImportInstrumentResolutionError(
                "AMBIGUOUS status requires draft to have an instrument_reference"
            )
        if tx_type in (
            TransactionType.CASH_DEPOSIT,
            TransactionType.CASH_WITHDRAWAL,
            TransactionType.FX_CONVERSION,
        ):
            raise PortfolioImportInstrumentResolutionError(
                f"AMBIGUOUS status is invalid for {tx_type.name} transactions"
            )

        if resolver_key is None or resolver_revision is None:
            raise PortfolioImportInstrumentResolutionError(
                "AMBIGUOUS status requires resolver_key and resolver_revision metadata"
            )
        if instrument_id is not None:
            raise PortfolioImportInstrumentResolutionError(
                "AMBIGUOUS status must not carry a final instrument_id"
            )
        if len(candidate_instrument_ids) < 2:
            raise PortfolioImportInstrumentResolutionError(
                f"AMBIGUOUS status requires at least two candidate_instrument_ids, got {len(candidate_instrument_ids)}"
            )
        if len(diagnostics) == 0:
            raise PortfolioImportInstrumentResolutionError(
                "AMBIGUOUS status requires at least one diagnostic"
            )


@dataclass(frozen=True)
class ImportInstrumentResolution:
    """
    Immutable point-in-time instrument resolution outcome for one economic transaction draft.
    """
    draft: ImportTransactionDraft
    status: ImportInstrumentResolutionStatus
    resolution_as_of_date: date
    resolver_key: Optional[str] = None
    resolver_revision: Optional[int] = None
    instrument_id: Optional[UUID] = None
    candidate_instrument_ids: Tuple[UUID, ...] = ()
    diagnostics: Tuple[ImportInstrumentResolutionDiagnostic, ...] = ()
    resolution_sha256: str = ""

    def __post_init__(self) -> None:
        _validate_resolution_record_fields(
            draft=self.draft,
            status=self.status,
            resolution_as_of_date=self.resolution_as_of_date,
            resolver_key=self.resolver_key,
            resolver_revision=self.resolver_revision,
            instrument_id=self.instrument_id,
            candidate_instrument_ids=self.candidate_instrument_ids,
            diagnostics=self.diagnostics,
        )

        # Direct constructor SHA validation
        if isinstance(self.resolution_sha256, bool) or not isinstance(self.resolution_sha256, str):
            raise PortfolioImportInstrumentResolutionError(
                f"resolution_sha256 must be a str instance, got {type(self.resolution_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.resolution_sha256):
            raise PortfolioImportInstrumentResolutionError(
                f"resolution_sha256 must be a 64-character lowercase hex string, got {self.resolution_sha256!r}"
            )

        expected_sha = _compute_resolution_sha256(
            draft=self.draft,
            status=self.status,
            resolution_as_of_date=self.resolution_as_of_date,
            resolver_key=self.resolver_key,
            resolver_revision=self.resolver_revision,
            instrument_id=self.instrument_id,
            candidate_instrument_ids=self.candidate_instrument_ids,
            diagnostics=self.diagnostics,
        )
        if self.resolution_sha256 != expected_sha:
            raise PortfolioImportInstrumentResolutionError(
                f"resolution_sha256 digest mismatch: computed {expected_sha}, declared {self.resolution_sha256}"
            )

    @property
    def record_ordinal(self) -> int:
        """Derived record ordinal from underlying draft."""
        return self.draft.record_ordinal

    @property
    def resolution_identity(self) -> Tuple[Any, ...]:
        """
        Immutable composite staging identity extending draft_identity:
        (*draft.draft_identity, resolution_sha256)
        """
        return (
            *self.draft.draft_identity,
            self.resolution_sha256,
        )


def build_import_instrument_resolution(
    draft: ImportTransactionDraft,
    status: ImportInstrumentResolutionStatus,
    resolution_as_of_date: date,
    resolver_key: Optional[str] = None,
    resolver_revision: Optional[int] = None,
    instrument_id: Optional[UUID] = None,
    candidate_instrument_ids: Union[List[UUID], Tuple[UUID, ...]] = (),
    diagnostics: Union[List[ImportInstrumentResolutionDiagnostic], Tuple[ImportInstrumentResolutionDiagnostic, ...]] = (),
) -> ImportInstrumentResolution:
    """
    Constructs an authoritative, verified, and canonically sorted ImportInstrumentResolution.

    Builder:
    - Accepts candidate_instrument_ids as materialized list or tuple.
    - Rejects duplicate UUIDs.
    - Sorts candidate_instrument_ids canonically by str(uuid) ascending.
    - Accepts diagnostics as materialized list or tuple.
    - Rejects duplicate diagnostic codes.
    - Sorts diagnostics canonically by (code, message) ascending.
    - Computes and verifies the canonical resolution_sha256.
    """
    if type(candidate_instrument_ids) not in (list, tuple):
        raise PortfolioImportInstrumentResolutionError(
            f"candidate_instrument_ids must be a list or tuple, got {type(candidate_instrument_ids).__name__}"
        )
    seen_cand: Set[UUID] = set()
    cand_list: List[UUID] = []
    for idx, cand in enumerate(candidate_instrument_ids):
        if not isinstance(cand, UUID):
            raise PortfolioImportInstrumentResolutionError(
                f"candidate_instrument_ids[{idx}] must be a UUID instance, got {type(cand).__name__}"
            )
        if cand in seen_cand:
            raise PortfolioImportInstrumentResolutionError(
                f"Duplicate candidate UUID detected in candidate_instrument_ids: {cand}"
            )
        seen_cand.add(cand)
        cand_list.append(cand)

    sorted_candidates: Tuple[UUID, ...] = tuple(
        sorted(cand_list, key=str)
    )

    if type(diagnostics) not in (list, tuple):
        raise PortfolioImportInstrumentResolutionError(
            f"diagnostics must be a list or tuple, got {type(diagnostics).__name__}"
        )
    seen_codes: Set[str] = set()
    diag_list: List[ImportInstrumentResolutionDiagnostic] = []
    for idx, diag in enumerate(diagnostics):
        if not isinstance(diag, ImportInstrumentResolutionDiagnostic):
            raise PortfolioImportInstrumentResolutionError(
                f"diagnostics[{idx}] must be an ImportInstrumentResolutionDiagnostic instance, got {type(diag).__name__}"
            )
        if diag.code in seen_codes:
            raise PortfolioImportInstrumentResolutionError(
                f"Duplicate diagnostic code detected in diagnostics: {diag.code!r}"
            )
        seen_codes.add(diag.code)
        diag_list.append(diag)

    sorted_diagnostics: Tuple[ImportInstrumentResolutionDiagnostic, ...] = tuple(
        sorted(diag_list, key=lambda d: (d.code, d.message))
    )

    # 5. Authoritative validation before any digest computation
    _validate_resolution_record_fields(
        draft=draft,
        status=status,
        resolution_as_of_date=resolution_as_of_date,
        resolver_key=resolver_key,
        resolver_revision=resolver_revision,
        instrument_id=instrument_id,
        candidate_instrument_ids=sorted_candidates,
        diagnostics=sorted_diagnostics,
    )

    # 6. Compute canonical resolution digest
    resolution_sha = _compute_resolution_sha256(
        draft=draft,
        status=status,
        resolution_as_of_date=resolution_as_of_date,
        resolver_key=resolver_key,
        resolver_revision=resolver_revision,
        instrument_id=instrument_id,
        candidate_instrument_ids=sorted_candidates,
        diagnostics=sorted_diagnostics,
    )

    return ImportInstrumentResolution(
        draft=draft,
        status=status,
        resolution_as_of_date=resolution_as_of_date,
        resolver_key=resolver_key,
        resolver_revision=resolver_revision,
        instrument_id=instrument_id,
        candidate_instrument_ids=sorted_candidates,
        diagnostics=sorted_diagnostics,
        resolution_sha256=resolution_sha,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch Manifest Layer
# ─────────────────────────────────────────────────────────────────────────────

def _compute_resolution_manifest_sha256(
    draft_manifest: ImportDraftBatchManifest,
    sorted_resolutions: Tuple[ImportInstrumentResolution, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for the resolution batch manifest preimage:
    [
      str(portfolio_id),
      str(account_id),
      source_key,
      file_content_sha256,
      raw_manifest_sha256,
      parser_revision,
      parsed_manifest_sha256,
      assessment_manifest_sha256,
      draft_manifest_sha256,
      [
        [record_ordinal, draft_sha256, resolution_sha256],
        ...
      ]
    ]
    Sorted by record_ordinal ascending.
    """
    ass_batch = draft_manifest.assessment_batch
    preimage: Any = [
        str(ass_batch.portfolio_id),
        str(ass_batch.account_id),
        ass_batch.source_key,
        ass_batch.file_content_sha256,
        ass_batch.raw_manifest_sha256,
        ass_batch.parser_revision,
        ass_batch.parsed_manifest_sha256,
        ass_batch.assessment_manifest_sha256,
        draft_manifest.draft_manifest_sha256,
        [
            [
                r.draft.record_ordinal,
                r.draft.draft_sha256,
                r.resolution_sha256,
            ]
            for r in sorted_resolutions
        ],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


def _validate_resolution_batch_invariants(
    draft_manifest: ImportDraftBatchManifest,
    resolutions: Tuple[ImportInstrumentResolution, ...],
) -> None:
    """
    Independently validates batch invariants:
    - draft_manifest is a genuine ImportDraftBatchManifest.
    - resolutions is an immutable tuple.
    - Complete 1:1 correspondence with draft_manifest.drafts.
    - Canonical sorting by record_ordinal ascending.
    - Each resolution's draft is semantically equal to draft_manifest.drafts[i].
    """
    if not isinstance(draft_manifest, ImportDraftBatchManifest):
        raise PortfolioImportInstrumentResolutionError(
            f"draft_manifest must be an ImportDraftBatchManifest instance, got {type(draft_manifest).__name__}"
        )

    if type(resolutions) is not tuple:
        raise PortfolioImportInstrumentResolutionError(
            f"resolutions must be an immutable tuple, got {type(resolutions).__name__}"
        )

    expected_count = draft_manifest.draft_count
    if len(resolutions) != expected_count:
        raise PortfolioImportInstrumentResolutionError(
            f"Resolution count mismatch: expected {expected_count} from draft manifest, got {len(resolutions)}"
        )

    seen_ordinals: Set[int] = set()
    prev_ordinal: int = 0

    for idx, resolution in enumerate(resolutions):
        if not isinstance(resolution, ImportInstrumentResolution):
            raise PortfolioImportInstrumentResolutionError(
                f"Resolution at index {idx} must be an ImportInstrumentResolution instance, got {type(resolution).__name__}"
            )

        ordinal = resolution.draft.record_ordinal

        if ordinal in seen_ordinals:
            raise PortfolioImportInstrumentResolutionError(
                f"Duplicate resolution record_ordinal detected: {ordinal}"
            )
        seen_ordinals.add(ordinal)

        if ordinal <= prev_ordinal:
            raise PortfolioImportInstrumentResolutionError(
                f"resolutions tuple is not sorted by record_ordinal ascending at index {idx} (ordinal {ordinal} after {prev_ordinal})"
            )
        prev_ordinal = ordinal

        expected_draft = draft_manifest.drafts[idx]
        if resolution.draft != expected_draft:
            raise PortfolioImportInstrumentResolutionError(
                f"Resolution at index {idx} (ordinal {ordinal}) is not semantically bound to "
                f"draft_manifest.drafts[{idx}] (ordinal {expected_draft.record_ordinal})"
            )


@dataclass(frozen=True)
class ImportInstrumentResolutionBatch:
    """
    Immutable batch manifest proving complete instrument-resolution outcome coverage
    for all economic drafts in an ImportDraftBatchManifest.
    """
    draft_manifest: ImportDraftBatchManifest
    resolutions: Tuple[ImportInstrumentResolution, ...]
    resolution_manifest_sha256: str

    def __post_init__(self) -> None:
        _validate_resolution_batch_invariants(
            draft_manifest=self.draft_manifest,
            resolutions=self.resolutions,
        )

        if isinstance(self.resolution_manifest_sha256, bool) or not isinstance(self.resolution_manifest_sha256, str):
            raise PortfolioImportInstrumentResolutionError(
                f"resolution_manifest_sha256 must be a str instance, got {type(self.resolution_manifest_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.resolution_manifest_sha256):
            raise PortfolioImportInstrumentResolutionError(
                f"resolution_manifest_sha256 must be a 64-character lowercase hex string, got {self.resolution_manifest_sha256!r}"
            )

        expected_sha = _compute_resolution_manifest_sha256(
            draft_manifest=self.draft_manifest,
            sorted_resolutions=self.resolutions,
        )
        if self.resolution_manifest_sha256 != expected_sha:
            raise PortfolioImportInstrumentResolutionError(
                f"resolution_manifest_sha256 digest mismatch: computed {expected_sha}, declared {self.resolution_manifest_sha256}"
            )

    # ─── Derived counts ──────────────────────────────────────────────────────

    @property
    def resolution_count(self) -> int:
        """Total count of resolution outcomes (equals draft_manifest.draft_count)."""
        return len(self.resolutions)

    @property
    def not_required_count(self) -> int:
        """Count of NOT_REQUIRED resolution outcomes."""
        return sum(1 for r in self.resolutions if r.status == ImportInstrumentResolutionStatus.NOT_REQUIRED)

    @property
    def resolved_count(self) -> int:
        """Count of RESOLVED resolution outcomes."""
        return sum(1 for r in self.resolutions if r.status == ImportInstrumentResolutionStatus.RESOLVED)

    @property
    def unresolved_count(self) -> int:
        """Count of UNRESOLVED resolution outcomes."""
        return sum(1 for r in self.resolutions if r.status == ImportInstrumentResolutionStatus.UNRESOLVED)

    @property
    def ambiguous_count(self) -> int:
        """Count of AMBIGUOUS resolution outcomes."""
        return sum(1 for r in self.resolutions if r.status == ImportInstrumentResolutionStatus.AMBIGUOUS)

    @property
    def is_fully_resolved(self) -> bool:
        """
        True iff all drafts are in RESOLVED or NOT_REQUIRED states (zero UNRESOLVED, zero AMBIGUOUS).
        Signifies instrument resolution stage completion; does not yet imply ledger readiness.
        """
        return self.unresolved_count == 0 and self.ambiguous_count == 0

    # ─── Delegated manifest metadata ─────────────────────────────────────────

    @property
    def portfolio_id(self) -> UUID:
        return self.draft_manifest.assessment_batch.portfolio_id

    @property
    def account_id(self) -> UUID:
        return self.draft_manifest.assessment_batch.account_id

    @property
    def source_key(self) -> str:
        return self.draft_manifest.assessment_batch.source_key

    @property
    def file_content_sha256(self) -> str:
        return self.draft_manifest.assessment_batch.file_content_sha256

    @property
    def raw_manifest_sha256(self) -> str:
        return self.draft_manifest.assessment_batch.raw_manifest_sha256

    @property
    def parser_revision(self) -> int:
        return self.draft_manifest.assessment_batch.parser_revision

    @property
    def parsed_manifest_sha256(self) -> str:
        return self.draft_manifest.assessment_batch.parsed_manifest_sha256

    @property
    def assessment_manifest_sha256(self) -> str:
        return self.draft_manifest.assessment_batch.assessment_manifest_sha256

    @property
    def draft_manifest_sha256(self) -> str:
        return self.draft_manifest.draft_manifest_sha256

    # ─── Staging Identity ────────────────────────────────────────────────────

    @property
    def resolution_manifest_identity(self) -> Tuple[Any, ...]:
        """
        Immutable composite staging identity extending draft_manifest_identity:
        (*draft_manifest.draft_manifest_identity, resolution_manifest_sha256)
        """
        return (
            *self.draft_manifest.draft_manifest_identity,
            self.resolution_manifest_sha256,
        )


def build_import_instrument_resolution_batch(
    draft_manifest: ImportDraftBatchManifest,
    resolutions: Union[List[ImportInstrumentResolution], Tuple[ImportInstrumentResolution, ...]],
) -> ImportInstrumentResolutionBatch:
    """
    Constructs an immutable ImportInstrumentResolutionBatch from a draft manifest and resolution collection.

    Builder:
    - Accepts resolutions only as a materialized list or tuple (generators, sets, dicts rejected).
    - Validates each item type.
    - Detects duplicate record ordinals explicitly.
    - Sorts resolutions by draft.record_ordinal ascending.
    - Verifies complete 1:1 correspondence with draft_manifest.drafts.
    - Computes and verifies canonical resolution_manifest_sha256.
    """
    if not isinstance(draft_manifest, ImportDraftBatchManifest):
        raise PortfolioImportInstrumentResolutionError(
            f"draft_manifest must be an ImportDraftBatchManifest instance, got {type(draft_manifest).__name__}"
        )

    if type(resolutions) not in (list, tuple):
        raise PortfolioImportInstrumentResolutionError(
            f"resolutions must be a list or tuple, got {type(resolutions).__name__}. "
            f"Generators, sets, dicts, and arbitrary iterators are not accepted."
        )

    seen_ordinals: Set[int] = set()
    res_list: List[ImportInstrumentResolution] = []

    for idx, resolution in enumerate(resolutions):
        if not isinstance(resolution, ImportInstrumentResolution):
            raise PortfolioImportInstrumentResolutionError(
                f"Resolution at index {idx} must be an ImportInstrumentResolution instance, got {type(resolution).__name__}"
            )

        ordinal = resolution.draft.record_ordinal
        if ordinal in seen_ordinals:
            raise PortfolioImportInstrumentResolutionError(
                f"Duplicate resolution record_ordinal detected: {ordinal}"
            )
        seen_ordinals.add(ordinal)
        res_list.append(resolution)

    # Sort canonically by draft.record_ordinal ascending
    sorted_resolutions: Tuple[ImportInstrumentResolution, ...] = tuple(
        sorted(res_list, key=lambda r: r.draft.record_ordinal)
    )

    expected_count = draft_manifest.draft_count
    if len(sorted_resolutions) != expected_count:
        raise PortfolioImportInstrumentResolutionError(
            f"Resolution count mismatch: expected {expected_count} from draft manifest, got {len(sorted_resolutions)}"
        )

    for idx, resolution in enumerate(sorted_resolutions):
        expected_draft = draft_manifest.drafts[idx]
        if resolution.draft != expected_draft:
            raise PortfolioImportInstrumentResolutionError(
                f"Resolution at index {idx} (ordinal {resolution.draft.record_ordinal}) is not semantically bound to "
                f"draft_manifest.drafts[{idx}] (ordinal {expected_draft.record_ordinal})"
            )

    manifest_sha = _compute_resolution_manifest_sha256(draft_manifest, sorted_resolutions)

    return ImportInstrumentResolutionBatch(
        draft_manifest=draft_manifest,
        resolutions=sorted_resolutions,
        resolution_manifest_sha256=manifest_sha,
    )
