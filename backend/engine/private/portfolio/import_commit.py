"""
Immutable import-commit claim and ledger-binding intent contract (Phase 13O).

This module defines the pure domain bridge from an immutable ImportLedgerMaterializationBatch (Phase 13N)
to immutable ImportLedgerBindingIntent and ImportLedgerBindingBatch objects.

Key Architectural Guarantees:
1. Exact Source-Record Claim Identity: The import claim identity is strictly anchored to immutable Phase 13A
   raw record provenance (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256).
2. Clean Separation from Ledger Identity: Staging claim identities are never mapped to ledger external_source
   or external_reference.
3. Conflict Detection Data: The expected plan SHA is bound to the claim intent so that future persistence
   can distinguish idempotent replays (same claim, same plan) from semantic conflicts (same claim, changed interpretation).
4. Pure Domain Boundary: Zero ledger transaction construction, zero UUID generation, zero clock calls,
   zero cash bucket assignment, zero database/repository persistence, and zero new hash calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Set, Tuple
from uuid import UUID

from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerMaterializationBatch,
    ImportLedgerTransactionPlan,
)


class PortfolioImportCommitError(ValueError):
    """Raised when import-commit claim or ledger-binding intent validation fails closed."""
    pass


@dataclass(frozen=True)
class ImportLedgerBindingIntent:
    """
    Immutable claim intent binding a verified pre-ledger transaction plan to its authoritative
    raw source record provenance.
    """
    plan: ImportLedgerTransactionPlan

    portfolio_id: UUID
    account_id: UUID
    source_key: str
    file_content_sha256: str
    record_ordinal: int
    record_sha256: str

    expected_plan_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ImportLedgerTransactionPlan):
            raise PortfolioImportCommitError(
                f"plan must be an ImportLedgerTransactionPlan instance, got {type(self.plan).__name__}"
            )

        # Extract authoritative Phase 13A record provenance
        rec_prov = (
            self.plan.resolution.draft.assessment.parsed_record.record_provenance
        )
        file_id = rec_prov.file_identity
        expected_port_id = file_id[0]
        expected_acc_id = file_id[1]
        expected_source_key = file_id[2]
        expected_file_sha = file_id[3]
        expected_ordinal = rec_prov.record_ordinal
        expected_record_sha = rec_prov.record_sha256
        expected_plan_sha = self.plan.plan_sha256

        # Target ID validation
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise PortfolioImportCommitError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if self.portfolio_id != expected_port_id:
            raise PortfolioImportCommitError(
                f"portfolio_id {self.portfolio_id} does not match provenance {expected_port_id}"
            )
        if self.portfolio_id != self.plan.portfolio_id:
            raise PortfolioImportCommitError(
                f"portfolio_id {self.portfolio_id} does not match plan {self.plan.portfolio_id}"
            )

        if isinstance(self.account_id, bool) or not isinstance(self.account_id, UUID):
            raise PortfolioImportCommitError(
                f"account_id must be a UUID instance, got {type(self.account_id).__name__}"
            )
        if self.account_id != expected_acc_id:
            raise PortfolioImportCommitError(
                f"account_id {self.account_id} does not match provenance {expected_acc_id}"
            )
        if self.account_id != self.plan.account_id:
            raise PortfolioImportCommitError(
                f"account_id {self.account_id} does not match plan {self.plan.account_id}"
            )

        # Source key validation
        if isinstance(self.source_key, bool) or type(self.source_key) is not str:
            raise PortfolioImportCommitError(
                f"source_key must be a str instance, got {type(self.source_key).__name__}"
            )
        if self.source_key != expected_source_key:
            raise PortfolioImportCommitError(
                f"source_key {self.source_key!r} does not match provenance {expected_source_key!r}"
            )

        # File content SHA validation
        if isinstance(self.file_content_sha256, bool) or type(self.file_content_sha256) is not str:
            raise PortfolioImportCommitError(
                f"file_content_sha256 must be a str instance, got {type(self.file_content_sha256).__name__}"
            )
        if self.file_content_sha256 != expected_file_sha:
            raise PortfolioImportCommitError(
                f"file_content_sha256 {self.file_content_sha256!r} does not match provenance {expected_file_sha!r}"
            )

        # Record ordinal validation
        if isinstance(self.record_ordinal, bool) or type(self.record_ordinal) is not int:
            raise PortfolioImportCommitError(
                f"record_ordinal must be an int instance, got {type(self.record_ordinal).__name__}"
            )
        if self.record_ordinal != expected_ordinal:
            raise PortfolioImportCommitError(
                f"record_ordinal {self.record_ordinal} does not match provenance {expected_ordinal}"
            )
        if self.record_ordinal != self.plan.record_ordinal:
            raise PortfolioImportCommitError(
                f"record_ordinal {self.record_ordinal} does not match plan {self.plan.record_ordinal}"
            )

        # Record SHA validation
        if isinstance(self.record_sha256, bool) or type(self.record_sha256) is not str:
            raise PortfolioImportCommitError(
                f"record_sha256 must be a str instance, got {type(self.record_sha256).__name__}"
            )
        if self.record_sha256 != expected_record_sha:
            raise PortfolioImportCommitError(
                f"record_sha256 {self.record_sha256!r} does not match provenance {expected_record_sha!r}"
            )

        # Expected plan SHA validation
        if isinstance(self.expected_plan_sha256, bool) or type(self.expected_plan_sha256) is not str:
            raise PortfolioImportCommitError(
                f"expected_plan_sha256 must be a str instance, got {type(self.expected_plan_sha256).__name__}"
            )
        if self.expected_plan_sha256 != expected_plan_sha:
            raise PortfolioImportCommitError(
                f"expected_plan_sha256 {self.expected_plan_sha256!r} does not match plan {expected_plan_sha!r}"
            )

    @property
    def claim_identity(self) -> Tuple[UUID, UUID, str, str, int, str]:
        """
        Canonical staged source record claim identity:
        (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256).
        """
        return (
            self.portfolio_id,
            self.account_id,
            self.source_key,
            self.file_content_sha256,
            self.record_ordinal,
            self.record_sha256,
        )

    @property
    def interpreted_claim_identity(self) -> Tuple[UUID, UUID, str, str, int, str, str]:
        """
        Diagnostic identity tuple binding raw claim identity to interpreted plan SHA:
        (*claim_identity, expected_plan_sha256).
        """
        return (
            *self.claim_identity,
            self.expected_plan_sha256,
        )


def build_import_ledger_binding_intent(
    plan: ImportLedgerTransactionPlan,
) -> ImportLedgerBindingIntent:
    """
    Constructs an immutable ImportLedgerBindingIntent from a verified ImportLedgerTransactionPlan.

    Args:
        plan: Authoritative ImportLedgerTransactionPlan.

    Returns:
        Verified ImportLedgerBindingIntent.

    Raises:
        PortfolioImportCommitError: If plan is malformed or not an ImportLedgerTransactionPlan instance.
    """
    if not isinstance(plan, ImportLedgerTransactionPlan):
        raise PortfolioImportCommitError(
            f"plan must be an ImportLedgerTransactionPlan instance, got {type(plan).__name__}"
        )

    rec_prov = plan.resolution.draft.assessment.parsed_record.record_provenance
    file_id = rec_prov.file_identity

    return ImportLedgerBindingIntent(
        plan=plan,
        portfolio_id=file_id[0],
        account_id=file_id[1],
        source_key=file_id[2],
        file_content_sha256=file_id[3],
        record_ordinal=rec_prov.record_ordinal,
        record_sha256=rec_prov.record_sha256,
        expected_plan_sha256=plan.plan_sha256,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch Layer
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ImportLedgerBindingBatch:
    """
    Immutable batch manifest proving complete binding intent coverage for all plans
    in an ImportLedgerMaterializationBatch.
    """
    materialization_batch: ImportLedgerMaterializationBatch
    intents: Tuple[ImportLedgerBindingIntent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.materialization_batch, ImportLedgerMaterializationBatch):
            raise PortfolioImportCommitError(
                f"materialization_batch must be an ImportLedgerMaterializationBatch instance, got {type(self.materialization_batch).__name__}"
            )

        if type(self.intents) is not tuple:
            raise PortfolioImportCommitError(
                f"intents must be an immutable tuple, got {type(self.intents).__name__}"
            )

        if len(self.intents) != self.materialization_batch.plan_count:
            raise PortfolioImportCommitError(
                f"intents count {len(self.intents)} does not equal plan_count {self.materialization_batch.plan_count}"
            )

        seen_claims: Set[Tuple[UUID, UUID, str, str, int, str]] = set()
        prev_ordinal: int = 0

        for idx, intent in enumerate(self.intents):
            if not isinstance(intent, ImportLedgerBindingIntent):
                raise PortfolioImportCommitError(
                    f"intents[{idx}] must be an ImportLedgerBindingIntent instance, got {type(intent).__name__}"
                )

            ordinal = intent.record_ordinal
            if ordinal <= prev_ordinal:
                raise PortfolioImportCommitError(
                    f"intents tuple is not sorted by record_ordinal ascending at index {idx} (ordinal {ordinal} after {prev_ordinal})"
                )
            prev_ordinal = ordinal

            expected_plan = self.materialization_batch.plans[idx]
            if intent.plan != expected_plan:
                raise PortfolioImportCommitError(
                    f"Intent at index {idx} (ordinal {ordinal}) is not semantically bound to "
                    f"materialization_batch.plans[{idx}] (ordinal {expected_plan.record_ordinal})"
                )

            claim = intent.claim_identity
            if claim in seen_claims:
                raise PortfolioImportCommitError(
                    f"Duplicate claim_identity detected in intents at index {idx}: {claim}"
                )
            seen_claims.add(claim)

    @property
    def intent_count(self) -> int:
        """Total count of ledger binding intents (equals materialization_batch.plan_count)."""
        return len(self.intents)


def build_import_ledger_binding_batch(
    materialization_batch: ImportLedgerMaterializationBatch,
) -> ImportLedgerBindingBatch:
    """
    Constructs an immutable ImportLedgerBindingBatch from a verified ImportLedgerMaterializationBatch.

    Args:
        materialization_batch: Authoritative ImportLedgerMaterializationBatch.

    Returns:
        Verified ImportLedgerBindingBatch.

    Raises:
        PortfolioImportCommitError: If materialization_batch is malformed.
    """
    if not isinstance(materialization_batch, ImportLedgerMaterializationBatch):
        raise PortfolioImportCommitError(
            f"materialization_batch must be an ImportLedgerMaterializationBatch instance, got {type(materialization_batch).__name__}"
        )

    intents: list[ImportLedgerBindingIntent] = []
    for plan in materialization_batch.plans:
        intent = build_import_ledger_binding_intent(plan)
        intents.append(intent)

    intents_tuple = tuple(sorted(intents, key=lambda i: i.record_ordinal))

    return ImportLedgerBindingBatch(
        materialization_batch=materialization_batch,
        intents=intents_tuple,
    )
