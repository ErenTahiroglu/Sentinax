"""
backend/engine/private/portfolio/sentinax_csv_import_execution.py
=================================================================
End-to-End Canonical CSV Import Execution Orchestrator & Authoritative Execution Result (Phase 13S/13S.1).

Composes the closed Phase 13 pre-ledger import, materialization, binding, and file-level atomic
commit pipeline into one canonical execution entrypoint:

Canonical CSV bytes
  -> Phase 13M pre-ledger import (run_sentinax_canonical_csv_import_v1)
  -> Phase 13N materialization (build_import_ledger_materialization_batch)
  -> Phase 13O binding intents (build_import_ledger_binding_batch)
  -> Phase 13R file-level atomic commit (repository.commit_import_binding_batch)
  -> SentinaxCanonicalCsvImportExecutionResult

Zero duplicated financial parsing, zero SQL, zero direct RPC/table access,
zero transaction UUID generation, zero clock calls, and zero new hash calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_assessment import ImportAssessmentStatus
from backend.engine.private.portfolio.import_batch_commit import (
    ImportBatchCommitResult,
    ImportBatchCommitStatus,
)
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingBatch,
    build_import_ledger_binding_batch,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionStatus,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
)
from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerMaterializationBatch,
    build_import_ledger_materialization_batch,
)
from backend.engine.private.portfolio.repository import PortfolioRepository
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)


class SentinaxCanonicalCsvImportExecutionStatus(Enum):
    """Authoritative top-level execution outcome status."""
    RESOLUTION_BLOCKED = "resolution_blocked"
    NOOP = "noop"
    APPENDED = "appended"
    IDEMPOTENT_DUPLICATE = "idempotent_duplicate"
    CONFLICT = "conflict"
    INVALID = "invalid"


def _execution_status_from_commit_status(
    status: ImportBatchCommitStatus,
) -> SentinaxCanonicalCsvImportExecutionStatus:
    """
    Pure fail-closed converter from authoritative ImportBatchCommitStatus to SentinaxCanonicalCsvImportExecutionStatus.
    """
    if not isinstance(status, ImportBatchCommitStatus):
        raise TypeError(
            f"status must be an ImportBatchCommitStatus instance, got {type(status).__name__}"
        )
    try:
        return SentinaxCanonicalCsvImportExecutionStatus(status.value)
    except ValueError as e:
        raise ValueError(
            f"Unrecognized ImportBatchCommitStatus cannot be mapped to execution status: {status!r}"
        ) from e


@dataclass(frozen=True)
class SentinaxCanonicalCsvImportExecutionResult:
    """
    Immutable audit envelope over the authoritative closed stage objects of a Canonical CSV import.
    """
    status: SentinaxCanonicalCsvImportExecutionStatus
    resolution_batch: ImportInstrumentResolutionBatch
    materialization_batch: Optional[ImportLedgerMaterializationBatch] = None
    binding_batch: Optional[ImportLedgerBindingBatch] = None
    commit_result: Optional[ImportBatchCommitResult] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SentinaxCanonicalCsvImportExecutionStatus):
            raise TypeError(
                f"status must be a SentinaxCanonicalCsvImportExecutionStatus instance, got {type(self.status).__name__}"
            )

        if not isinstance(self.resolution_batch, ImportInstrumentResolutionBatch):
            raise TypeError(
                f"resolution_batch must be an ImportInstrumentResolutionBatch instance, got {type(self.resolution_batch).__name__}"
            )

        if self.materialization_batch is not None and not isinstance(
            self.materialization_batch, ImportLedgerMaterializationBatch
        ):
            raise TypeError(
                f"materialization_batch must be None or an ImportLedgerMaterializationBatch instance, got {type(self.materialization_batch).__name__}"
            )

        if self.binding_batch is not None and not isinstance(
            self.binding_batch, ImportLedgerBindingBatch
        ):
            raise TypeError(
                f"binding_batch must be None or an ImportLedgerBindingBatch instance, got {type(self.binding_batch).__name__}"
            )

        if self.commit_result is not None and not isinstance(
            self.commit_result, ImportBatchCommitResult
        ):
            raise TypeError(
                f"commit_result must be None or an ImportBatchCommitResult instance, got {type(self.commit_result).__name__}"
            )

        if self.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED:
            if self.resolution_batch.is_fully_resolved:
                raise ValueError(
                    "RESOLUTION_BLOCKED result requires resolution_batch.is_fully_resolved is False"
                )
            if (
                self.resolution_batch.unresolved_count == 0
                and self.resolution_batch.ambiguous_count == 0
            ):
                raise ValueError(
                    "RESOLUTION_BLOCKED result requires at least one UNRESOLVED or AMBIGUOUS resolution"
                )
            if self.materialization_batch is not None:
                raise ValueError(
                    "RESOLUTION_BLOCKED result must have materialization_batch=None"
                )
            if self.binding_batch is not None:
                raise ValueError(
                    "RESOLUTION_BLOCKED result must have binding_batch=None"
                )
            if self.commit_result is not None:
                raise ValueError(
                    "RESOLUTION_BLOCKED result must have commit_result=None"
                )

        else:
            if not self.resolution_batch.is_fully_resolved:
                raise ValueError(
                    f"{self.status.name} result requires resolution_batch.is_fully_resolved is True"
                )
            if self.materialization_batch is None:
                raise ValueError(
                    f"{self.status.name} result requires non-None materialization_batch"
                )
            if self.binding_batch is None:
                raise ValueError(
                    f"{self.status.name} result requires non-None binding_batch"
                )
            if self.commit_result is None:
                raise ValueError(
                    f"{self.status.name} result requires non-None commit_result"
                )

            # Direct constructor tamper checks for stage linkage
            if self.materialization_batch.resolution_batch != self.resolution_batch:
                raise ValueError(
                    "materialization_batch.resolution_batch does not match resolution_batch"
                )
            if self.binding_batch.materialization_batch != self.materialization_batch:
                raise ValueError(
                    "binding_batch.materialization_batch does not match materialization_batch"
                )

            # Top-level status mapping check (via non-mutable fail-closed converter)
            expected_status = _execution_status_from_commit_status(self.commit_result.status)
            if self.status != expected_status:
                raise ValueError(
                    f"Top-level status {self.status.name} does not match commit_result status {self.commit_result.status.name}"
                )

            # Cross-stage commit coherence with binding_batch
            binding_count = self.binding_batch.intent_count
            if self.commit_result.status == ImportBatchCommitStatus.NOOP:
                if binding_count != 0:
                    raise ValueError(
                        f"NOOP commit_result requires binding_batch.intent_count == 0, got {binding_count}"
                    )
            else:
                if binding_count == 0:
                    raise ValueError(
                        f"{self.commit_result.status.name} commit_result requires binding_batch.intent_count > 0, got 0"
                    )

                if self.commit_result.status in (
                    ImportBatchCommitStatus.APPENDED,
                    ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE,
                ):
                    if len(self.commit_result.transaction_ids) != binding_count:
                        raise ValueError(
                            f"{self.commit_result.status.name} transaction_ids count {len(self.commit_result.transaction_ids)} "
                            f"does not match binding_batch.intent_count {binding_count}"
                        )
                    if len(self.commit_result.item_statuses) != binding_count:
                        raise ValueError(
                            f"{self.commit_result.status.name} item_statuses count {len(self.commit_result.item_statuses)} "
                            f"does not match binding_batch.intent_count {binding_count}"
                        )

                elif self.commit_result.status in (
                    ImportBatchCommitStatus.CONFLICT,
                    ImportBatchCommitStatus.INVALID,
                ):
                    valid_ordinals = {i.record_ordinal for i in self.binding_batch.intents}
                    if self.commit_result.problem_record_ordinal not in valid_ordinals:
                        raise ValueError(
                            f"{self.commit_result.status.name} problem_record_ordinal {self.commit_result.problem_record_ordinal} "
                            f"is not present in binding_batch intent ordinals {sorted(valid_ordinals)}"
                        )

    # ─── Audit Count Properties ──────────────────────────────────────────────

    @property
    def source_record_count(self) -> int:
        """Total source records in the batch (including semantic REJECTED rows)."""
        return self.resolution_batch.draft_manifest.assessment_batch.record_count

    @property
    def ready_record_count(self) -> int:
        """Total records assessed as READY."""
        return self.resolution_batch.draft_manifest.assessment_batch.ready_count

    @property
    def rejected_record_count(self) -> int:
        """Total records assessed as REJECTED."""
        return self.resolution_batch.draft_manifest.assessment_batch.rejected_count

    @property
    def resolution_count(self) -> int:
        """Total resolution outcomes (equals ready_record_count)."""
        return self.resolution_batch.resolution_count

    @property
    def unresolved_resolution_count(self) -> int:
        """Total UNRESOLVED instrument resolution outcomes."""
        return self.resolution_batch.unresolved_count

    @property
    def ambiguous_resolution_count(self) -> int:
        """Total AMBIGUOUS instrument resolution outcomes."""
        return self.resolution_batch.ambiguous_count

    @property
    def binding_intent_count(self) -> int:
        """Total ledger binding intents (0 if unmaterialized/blocked)."""
        if self.binding_batch is None:
            return 0
        return self.binding_batch.intent_count

    @property
    def rejected_record_ordinals(self) -> Tuple[int, ...]:
        """Source record ordinals assessed as REJECTED in canonical ascending order."""
        return tuple(
            a.parsed_record.record_provenance.record_ordinal
            for a in self.resolution_batch.draft_manifest.assessment_batch.assessments
            if a.status == ImportAssessmentStatus.REJECTED
        )

    @property
    def blocked_resolution_ordinals(self) -> Tuple[int, ...]:
        """Draft record ordinals whose resolution is UNRESOLVED or AMBIGUOUS in ascending order."""
        return tuple(
            r.draft.record_ordinal
            for r in self.resolution_batch.resolutions
            if r.status
            in (
                ImportInstrumentResolutionStatus.UNRESOLVED,
                ImportInstrumentResolutionStatus.AMBIGUOUS,
            )
        )

    @property
    def transaction_ids(self) -> Tuple[UUID, ...]:
        """Committed transaction UUIDs in input order (empty on BLOCKED, NOOP, CONFLICT, or INVALID)."""
        if self.commit_result is not None:
            return self.commit_result.transaction_ids
        return ()


def execute_sentinax_canonical_csv_import_v1(
    *,
    repository: PortfolioRepository,
    portfolio_id: UUID,
    account_id: UUID,
    filename: str,
    content: bytes,
    imported_at: datetime,
    resolver: PortfolioImportInstrumentResolver,
) -> SentinaxCanonicalCsvImportExecutionResult:
    """
    Executes the complete Canonical CSV v1 import workflow.

    Args:
        repository: Authoritative PortfolioRepository.
        portfolio_id: Target portfolio UUID.
        account_id: Target account UUID within portfolio.
        filename: Original file name string.
        content: Raw immutable CSV bytes.
        imported_at: Explicit timezone-aware import timestamp.
        resolver: Source-neutral PIT instrument resolver.

    Returns:
        Authoritative SentinaxCanonicalCsvImportExecutionResult.
    """
    if not isinstance(repository, PortfolioRepository):
        raise TypeError(
            f"repository must be a PortfolioRepository instance, got {type(repository).__name__}"
        )

    resolution_batch = run_sentinax_canonical_csv_import_v1(
        portfolio_id=portfolio_id,
        account_id=account_id,
        filename=filename,
        content=content,
        imported_at=imported_at,
        resolver=resolver,
    )

    if not resolution_batch.is_fully_resolved:
        return SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED,
            resolution_batch=resolution_batch,
            materialization_batch=None,
            binding_batch=None,
            commit_result=None,
        )

    materialization_batch = build_import_ledger_materialization_batch(
        resolution_batch
    )
    binding_batch = build_import_ledger_binding_batch(
        materialization_batch
    )
    commit_result = repository.commit_import_binding_batch(
        binding_batch
    )

    top_status = _execution_status_from_commit_status(commit_result.status)

    return SentinaxCanonicalCsvImportExecutionResult(
        status=top_status,
        resolution_batch=resolution_batch,
        materialization_batch=materialization_batch,
        binding_batch=binding_batch,
        commit_result=commit_result,
    )
