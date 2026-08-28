"""
backend/engine/private/portfolio/import_draft_batch.py
======================================================
Immutable Economic Draft Batch Manifest & Complete READY-Coverage Integrity (Phase 13I).

This module establishes the batch-composition layer that proves one exact Phase 13G
ImportAssessmentBatch has exactly one Phase 13H economic draft for every READY record,
and zero drafts for UNRESOLVED/REJECTED records.

Key Architectural Invariants:
1. One-Draft-Per-READY Contract:
   - READY records: exactly one ImportTransactionDraft per record ordinal.
   - UNRESOLVED records: zero drafts, no bypass.
   - REJECTED records: zero drafts, no bypass.
   - No READY record may be omitted. No READY record may have two drafts.
2. Exact Assessment Batch Binding:
   - Every draft's assessment_batch must be the SAME object (identity-equal) as the
     batch in ImportDraftBatchManifest. Drafts from foreign batches fail closed.
3. Deterministic Canonical Ordering:
   - Drafts are canonically sorted by record_ordinal ascending, regardless of input order.
   - Builder input order is irrelevant; output is always deterministic.
4. Deterministic Draft Manifest Preimage & Digest:
   - draft_manifest_sha256 is computed from compact JSON:
     [str(portfolio_id), str(account_id), source_key, file_content_sha256,
      raw_manifest_sha256, parser_revision, parsed_manifest_sha256,
      assessment_manifest_sha256, [[record_ordinal, parsed_sha256, draft_sha256], ...]]
   - Entries sorted by record_ordinal ascending.
   - Timestamps, filenames, byte counts, instrument UUIDs, and ledger identities excluded.
5. Immutability:
   - ImportDraftBatchManifest is frozen=True. No mutable collections.
6. Pre-Ledger Boundary:
   - Zero PortfolioTransaction. Zero instrument_id UUIDs. Zero external_source/reference.
   - Zero cash_bucket attribution. Zero persistence. Zero ledger mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, List, Sequence, Set, Tuple, Union

from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentStatus,
)
from backend.engine.private.portfolio.import_draft import ImportTransactionDraft

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PortfolioImportDraftBatchError(ValueError):
    """Raised when draft batch integrity validation fails closed."""
    pass


def _compute_draft_manifest_sha256(
    assessment_batch: ImportAssessmentBatch,
    sorted_drafts: Tuple[ImportTransactionDraft, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for the draft batch manifest preimage:
    [str(portfolio_id), str(account_id), source_key, file_content_sha256,
     raw_manifest_sha256, parser_revision, parsed_manifest_sha256,
     assessment_manifest_sha256, [[record_ordinal, parsed_sha256, draft_sha256], ...]]
    Draft entries are sorted by record_ordinal ascending.
    """
    preimage: Any = [
        str(assessment_batch.portfolio_id),
        str(assessment_batch.account_id),
        assessment_batch.source_key,
        assessment_batch.file_content_sha256,
        assessment_batch.raw_manifest_sha256,
        assessment_batch.parser_revision,
        assessment_batch.parsed_manifest_sha256,
        assessment_batch.assessment_manifest_sha256,
        [
            [
                d.record_ordinal,
                d.assessment.parsed_record.parsed_sha256,
                d.draft_sha256,
            ]
            for d in sorted_drafts
        ],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


def _validate_draft_batch_invariants(
    assessment_batch: ImportAssessmentBatch,
    drafts: Tuple[ImportTransactionDraft, ...],
) -> None:
    """
    Independently validates all batch invariants:
    - assessment_batch is a genuine ImportAssessmentBatch.
    - drafts is a tuple.
    - Each element is an ImportTransactionDraft.
    - Each draft's assessment_batch is semantically equal to the manifest batch (full dataclass
      equality, not object identity). Semantically equal but distinct in-memory objects are accepted.
      Only genuinely different assessment state fails closed.
    - No duplicate record ordinals.
    - Every draft ordinal is READY in the authoritative assessment batch.
    - No UNRESOLVED/REJECTED ordinals are drafted.
    - drafts are sorted by record_ordinal ascending.
    - Exact READY coverage: every READY ordinal has exactly one draft, no extras.
    """
    if not isinstance(assessment_batch, ImportAssessmentBatch):
        raise PortfolioImportDraftBatchError(
            f"assessment_batch must be an ImportAssessmentBatch instance, "
            f"got {type(assessment_batch).__name__}"
        )

    if type(drafts) is not tuple:
        raise PortfolioImportDraftBatchError(
            f"drafts must be an immutable tuple, got {type(drafts).__name__}"
        )

    # Derive authoritative READY ordinals from the assessment batch.
    ready_ordinals: Set[int] = {
        a.parsed_record.record_provenance.record_ordinal
        for a in assessment_batch.assessments
        if a.status == ImportAssessmentStatus.READY
    }

    seen_ordinals: Set[int] = set()
    prev_ordinal: int = 0

    for idx, draft in enumerate(drafts):
        # Type check
        if not isinstance(draft, ImportTransactionDraft):
            raise PortfolioImportDraftBatchError(
                f"Draft at index {idx} must be an ImportTransactionDraft instance, "
                f"got {type(draft).__name__}"
            )

        # Binding: full semantic equality required.
        # Object identity is NOT the authority. Semantically equal but distinct in-memory
        # ImportAssessmentBatch instances (e.g. reconstructed after serialization/hydration)
        # must be accepted. Only a genuinely different assessment state (different portfolio,
        # account, file, statuses, diagnostics, parsed records, or digest) fails closed.
        if draft.assessment_batch != assessment_batch:
            raise PortfolioImportDraftBatchError(
                f"Draft at index {idx} (ordinal {draft.record_ordinal}) is bound to a "
                f"semantically different assessment batch. All drafts must be bound to an "
                f"ImportAssessmentBatch that is semantically equal to the manifest's batch."
            )

        ordinal = draft.record_ordinal

        # Duplicate ordinal detection (explicit, no silent dedup)
        if ordinal in seen_ordinals:
            raise PortfolioImportDraftBatchError(
                f"Duplicate draft record_ordinal detected: {ordinal}. "
                f"Each READY record ordinal may appear in at most one draft."
            )
        seen_ordinals.add(ordinal)

        # Ascending order check (required for direct constructor)
        if ordinal <= prev_ordinal:
            raise PortfolioImportDraftBatchError(
                f"drafts tuple is not sorted by record_ordinal ascending. "
                f"Found ordinal {ordinal} after {prev_ordinal} at index {idx}."
            )
        prev_ordinal = ordinal

        # Ordinal must be in READY set
        if ordinal not in ready_ordinals:
            raise PortfolioImportDraftBatchError(
                f"Draft at index {idx} has record_ordinal {ordinal} which is not READY "
                f"in the authoritative assessment batch. Only READY records may be drafted."
            )

    # Complete READY coverage check
    draft_ordinals = seen_ordinals
    missing = ready_ordinals - draft_ordinals
    if missing:
        raise PortfolioImportDraftBatchError(
            f"Incomplete READY coverage: missing drafts for READY record ordinals {sorted(missing)}. "
            f"Every READY record must have exactly one economic draft."
        )


@dataclass(frozen=True)
class ImportDraftBatchManifest:
    """
    Immutable batch manifest proving complete 1:1 economic draft coverage for all READY
    records in one ImportAssessmentBatch. UNRESOLVED/REJECTED records have zero drafts.

    Fields:
        assessment_batch: Authoritative Phase 13G assessment batch (sole identity authority).
        drafts: Immutable tuple of ImportTransactionDraft, canonically sorted ascending
                by record_ordinal.
        draft_manifest_sha256: Deterministic 64-char lowercase hex SHA-256 of the
                               canonical draft batch preimage.
    """
    assessment_batch: ImportAssessmentBatch
    drafts: Tuple[ImportTransactionDraft, ...]
    draft_manifest_sha256: str

    def __post_init__(self) -> None:
        # 1. Validate all structural batch invariants independently.
        _validate_draft_batch_invariants(self.assessment_batch, self.drafts)

        # 2. Validate draft_manifest_sha256 type and format.
        if isinstance(self.draft_manifest_sha256, bool) or not isinstance(self.draft_manifest_sha256, str):
            raise PortfolioImportDraftBatchError(
                f"draft_manifest_sha256 must be a str instance, "
                f"got {type(self.draft_manifest_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.draft_manifest_sha256):
            raise PortfolioImportDraftBatchError(
                f"draft_manifest_sha256 must be a 64-character lowercase hex string, "
                f"got {self.draft_manifest_sha256!r}"
            )

        # 3. Recompute canonical digest and require exact equality (no fake hashes).
        expected_sha = _compute_draft_manifest_sha256(self.assessment_batch, self.drafts)
        if self.draft_manifest_sha256 != expected_sha:
            raise PortfolioImportDraftBatchError(
                f"draft_manifest_sha256 digest mismatch: "
                f"computed {expected_sha}, declared {self.draft_manifest_sha256}"
            )

    # ─── Derived count properties ────────────────────────────────────────────

    @property
    def draft_count(self) -> int:
        """Number of economic drafts. Equals assessment_batch.ready_count for a valid batch."""
        return len(self.drafts)

    @property
    def record_count(self) -> int:
        """Total assessed records (derived from assessment_batch)."""
        return self.assessment_batch.record_count

    @property
    def ready_count(self) -> int:
        """Count of READY records (derived from assessment_batch)."""
        return self.assessment_batch.ready_count

    @property
    def unresolved_count(self) -> int:
        """Count of UNRESOLVED records (derived from assessment_batch)."""
        return self.assessment_batch.unresolved_count

    @property
    def rejected_count(self) -> int:
        """Count of REJECTED records (derived from assessment_batch)."""
        return self.assessment_batch.rejected_count

    # ─── Manifest identity ───────────────────────────────────────────────────

    @property
    def draft_manifest_identity(self) -> Tuple[Any, ...]:
        """
        Immutable composite staging identity extending assessment_manifest_identity:
        (*assessment_manifest_identity, draft_manifest_sha256)
        Not a ledger external identity. No UUID generated.
        """
        return (
            *self.assessment_batch.assessment_manifest_identity,
            self.draft_manifest_sha256,
        )


def build_import_draft_batch_manifest(
    assessment_batch: ImportAssessmentBatch,
    drafts: Union[List[ImportTransactionDraft], Tuple[ImportTransactionDraft, ...]],
) -> ImportDraftBatchManifest:
    """
    Constructs an immutable ImportDraftBatchManifest from an assessment batch and draft collection.

    Builder:
    - Accepts drafts only as a materialized list or tuple (generators, sets, dicts, etc. rejected).
    - Validates each item type.
    - Validates that every draft is semantically bound to the same assessment_batch content
      (full dataclass equality, not object identity).
    - Detects duplicate record ordinals explicitly (no silent dedup).
    - Derives authoritative READY ordinals from assessment_batch.
    - Proves exact READY coverage.
    - Sorts drafts by record_ordinal ascending (caller input order irrelevant).
    - Freezes into a tuple.
    - Computes and verifies the canonical draft manifest SHA-256.
    """
    # 1. Validate assessment_batch type.
    if not isinstance(assessment_batch, ImportAssessmentBatch):
        raise PortfolioImportDraftBatchError(
            f"assessment_batch must be an ImportAssessmentBatch instance, "
            f"got {type(assessment_batch).__name__}"
        )

    # 2. Validate drafts collection type: only list or tuple accepted.
    if type(drafts) not in (list, tuple):
        raise PortfolioImportDraftBatchError(
            f"drafts must be a list or tuple, got {type(drafts).__name__}. "
            f"Generators, sets, dicts, and arbitrary iterators are not accepted."
        )

    # 3. Validate items, collect, check for duplicates and binding.
    seen_ordinals: Set[int] = set()
    draft_list: List[ImportTransactionDraft] = []

    for idx, draft in enumerate(drafts):
        if not isinstance(draft, ImportTransactionDraft):
            raise PortfolioImportDraftBatchError(
                f"Draft at index {idx} must be an ImportTransactionDraft instance, "
                f"got {type(draft).__name__}"
            )

        # Binding: full semantic equality required.
        # Object identity is NOT the authority. Semantically equal but distinct in-memory
        # ImportAssessmentBatch instances must be accepted.
        if draft.assessment_batch != assessment_batch:
            raise PortfolioImportDraftBatchError(
                f"Draft at index {idx} (ordinal {draft.record_ordinal}) is bound to a "
                f"semantically different assessment batch. All drafts must be bound to an "
                f"ImportAssessmentBatch that is semantically equal to the manifest's batch."
            )

        ordinal = draft.record_ordinal
        if ordinal in seen_ordinals:
            raise PortfolioImportDraftBatchError(
                f"Duplicate draft record_ordinal detected: {ordinal}. "
                f"Each READY record ordinal may appear in at most one draft."
            )
        seen_ordinals.add(ordinal)
        draft_list.append(draft)

    # 4. Derive authoritative READY ordinals from assessment_batch.
    ready_ordinals: Set[int] = {
        a.parsed_record.record_provenance.record_ordinal
        for a in assessment_batch.assessments
        if a.status == ImportAssessmentStatus.READY
    }

    # 5. Check for non-READY ordinals.
    non_ready = seen_ordinals - ready_ordinals
    if non_ready:
        raise PortfolioImportDraftBatchError(
            f"Draft(s) reference non-READY record ordinal(s) {sorted(non_ready)}. "
            f"Only READY records may be drafted."
        )

    # 6. Check for missing READY ordinals.
    missing = ready_ordinals - seen_ordinals
    if missing:
        raise PortfolioImportDraftBatchError(
            f"Incomplete READY coverage: missing drafts for READY record ordinals {sorted(missing)}. "
            f"Every READY record must have exactly one economic draft."
        )

    # 7. Canonical sort by record_ordinal ascending (stable, preserves original objects).
    sorted_drafts: Tuple[ImportTransactionDraft, ...] = tuple(
        sorted(draft_list, key=lambda d: d.record_ordinal)
    )

    # 8. Compute canonical manifest digest.
    manifest_sha = _compute_draft_manifest_sha256(assessment_batch, sorted_drafts)

    # 9. Construct and return the frozen manifest.
    return ImportDraftBatchManifest(
        assessment_batch=assessment_batch,
        drafts=sorted_drafts,
        draft_manifest_sha256=manifest_sha,
    )
