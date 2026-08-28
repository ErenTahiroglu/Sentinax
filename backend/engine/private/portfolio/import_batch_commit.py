# backend/engine/private/portfolio/import_batch_commit.py
"""Domain contracts for file-level atomic binding-batch commit (Phase 13R).

Defines:
- ImportBatchCommitStatus
- ImportBatchItemCommitStatus
- ImportBatchCommitResult
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple
from uuid import UUID


class ImportBatchCommitStatus(Enum):
    """Authoritative outcome status of a file-level import binding batch commit."""
    NOOP = "noop"
    APPENDED = "appended"
    IDEMPOTENT_DUPLICATE = "idempotent_duplicate"
    CONFLICT = "conflict"
    INVALID = "invalid"


class ImportBatchItemCommitStatus(Enum):
    """Authoritative outcome status of an individual item within a successful batch."""
    APPENDED = "appended"
    IDEMPOTENT_DUPLICATE = "idempotent_duplicate"


@dataclass(frozen=True)
class ImportBatchCommitResult:
    """Immutable domain result of committing an ImportLedgerBindingBatch."""
    status: ImportBatchCommitStatus

    transaction_ids: Tuple[UUID, ...] = ()
    item_statuses: Tuple[ImportBatchItemCommitStatus, ...] = ()

    problem_record_ordinal: Optional[int] = None
    conflict_transaction_id: Optional[UUID] = None

    diagnostics: Tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.status, ImportBatchCommitStatus):
            raise TypeError(f"status must be an ImportBatchCommitStatus, got {type(self.status)}")

        if not isinstance(self.transaction_ids, tuple):
            raise TypeError(f"transaction_ids must be a tuple of UUIDs, got {type(self.transaction_ids)}")
        for tx_id in self.transaction_ids:
            if not isinstance(tx_id, UUID):
                raise TypeError(f"All transaction_ids elements must be UUID instances, got {type(tx_id)}")

        if not isinstance(self.item_statuses, tuple):
            raise TypeError(f"item_statuses must be a tuple of ImportBatchItemCommitStatus, got {type(self.item_statuses)}")
        for it_st in self.item_statuses:
            if not isinstance(it_st, ImportBatchItemCommitStatus):
                raise TypeError(f"All item_statuses elements must be ImportBatchItemCommitStatus instances, got {type(it_st)}")

        if not isinstance(self.diagnostics, tuple):
            raise TypeError(f"diagnostics must be a tuple of strings, got {type(self.diagnostics)}")
        for d in self.diagnostics:
            if not isinstance(d, str):
                raise TypeError(f"All diagnostics elements must be strings, got {type(d)}")

        if self.problem_record_ordinal is not None:
            if isinstance(self.problem_record_ordinal, bool) or not isinstance(self.problem_record_ordinal, int):
                raise TypeError(f"problem_record_ordinal must be an integer, got {type(self.problem_record_ordinal)}")
            if self.problem_record_ordinal < 1:
                raise ValueError(f"problem_record_ordinal must be >= 1, got {self.problem_record_ordinal}")

        if self.conflict_transaction_id is not None:
            if not isinstance(self.conflict_transaction_id, UUID):
                raise TypeError(f"conflict_transaction_id must be a UUID instance, got {type(self.conflict_transaction_id)}")

        # Status invariant validations
        if self.status == ImportBatchCommitStatus.NOOP:
            if self.transaction_ids != ():
                raise ValueError("NOOP result must have empty transaction_ids")
            if self.item_statuses != ():
                raise ValueError("NOOP result must have empty item_statuses")
            if self.problem_record_ordinal is not None:
                raise ValueError("NOOP result must have problem_record_ordinal=None")
            if self.conflict_transaction_id is not None:
                raise ValueError("NOOP result must have conflict_transaction_id=None")
            if self.diagnostics != ():
                raise ValueError("NOOP result must have empty diagnostics")

        elif self.status == ImportBatchCommitStatus.APPENDED:
            if len(self.transaction_ids) == 0:
                raise ValueError("APPENDED result must have non-empty transaction_ids")
            if len(self.item_statuses) != len(self.transaction_ids):
                raise ValueError(f"Length mismatch: {len(self.item_statuses)} item_statuses vs {len(self.transaction_ids)} transaction_ids")
            if not any(s == ImportBatchItemCommitStatus.APPENDED for s in self.item_statuses):
                raise ValueError("APPENDED batch result must contain at least one APPENDED item status")
            if self.problem_record_ordinal is not None:
                raise ValueError("APPENDED result must have problem_record_ordinal=None")
            if self.conflict_transaction_id is not None:
                raise ValueError("APPENDED result must have conflict_transaction_id=None")

        elif self.status == ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE:
            if len(self.transaction_ids) == 0:
                raise ValueError("IDEMPOTENT_DUPLICATE result must have non-empty transaction_ids")
            if len(self.item_statuses) != len(self.transaction_ids):
                raise ValueError(f"Length mismatch: {len(self.item_statuses)} item_statuses vs {len(self.transaction_ids)} transaction_ids")
            if not all(s == ImportBatchItemCommitStatus.IDEMPOTENT_DUPLICATE for s in self.item_statuses):
                raise ValueError("IDEMPOTENT_DUPLICATE batch result must consist exclusively of IDEMPOTENT_DUPLICATE item statuses")
            if self.problem_record_ordinal is not None:
                raise ValueError("IDEMPOTENT_DUPLICATE result must have problem_record_ordinal=None")
            if self.conflict_transaction_id is not None:
                raise ValueError("IDEMPOTENT_DUPLICATE result must have conflict_transaction_id=None")

        elif self.status == ImportBatchCommitStatus.CONFLICT:
            if self.transaction_ids != ():
                raise ValueError("CONFLICT result must have empty transaction_ids")
            if self.item_statuses != ():
                raise ValueError("CONFLICT result must have empty item_statuses")
            if self.problem_record_ordinal is None:
                raise ValueError("CONFLICT result must have problem_record_ordinal >= 1")
            if self.conflict_transaction_id is None:
                raise ValueError("CONFLICT result must have conflict_transaction_id specified as UUID")
            if len(self.diagnostics) == 0:
                raise ValueError("CONFLICT result must contain non-empty diagnostics")

        elif self.status == ImportBatchCommitStatus.INVALID:
            if self.transaction_ids != ():
                raise ValueError("INVALID result must have empty transaction_ids")
            if self.item_statuses != ():
                raise ValueError("INVALID result must have empty item_statuses")
            if self.problem_record_ordinal is None:
                raise ValueError("INVALID result must have problem_record_ordinal >= 1")
            if self.conflict_transaction_id is not None:
                raise ValueError("INVALID result must have conflict_transaction_id=None")
            if len(self.diagnostics) == 0:
                raise ValueError("INVALID result must contain non-empty diagnostics")
