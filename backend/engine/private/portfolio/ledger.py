"""
backend/engine/private/portfolio/ledger.py
===========================================
Authoritative In-Memory Immutable Ledger & Consistency Validator.

Core Invariants:
    - Ledger binds directly to root `Portfolio` aggregate (eliminates mode split-brain).
    - Transactions are APPEND-ONLY. No edits, no deletes, no in-place modifications.
    - Corrections are executed via REVERSAL events referencing `reverses_transaction_id`.
    - A transaction may be reversed at most once in authoritative history.
    - No self-reversal, cross-portfolio reversal, cross-account reversal, or reversal of reversal.
    - External source idempotency:
        * Same external ref + same economics -> IDEMPOTENT_DUPLICATE (safe replay)
        * Same external ref + different economics -> CONFLICT (rejected)
        * Missing external ref -> UUID is unique event identity
    - Deterministic sort order: (effective_date, executed_at, recorded_at, id)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.domain import PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)


class AppendStatus(Enum):
    """Result status of attempting to append a transaction to the ledger."""
    APPENDED = "appended"
    IDEMPOTENT_DUPLICATE = "idempotent_duplicate"
    CONFLICT = "conflict"
    INVALID = "invalid"


@dataclass(frozen=True)
class AppendResult:
    """Outcome of an append operation on PortfolioLedger."""
    status: AppendStatus
    transaction_id: Optional[UUID] = None
    diagnostics: Tuple[str, ...] = ()

    @property
    def is_success(self) -> bool:
        return self.status in (AppendStatus.APPENDED, AppendStatus.IDEMPOTENT_DUPLICATE)


class PortfolioLedgerValidator:
    """
    Pure validation functions for cross-entity consistency.
    """

    @staticmethod
    def validate_transaction_portfolio_consistency(
        transaction: PortfolioTransaction,
        portfolio: Portfolio,
        account: PortfolioAccount,
        cash_bucket: Optional[CashBucket] = None,
    ) -> None:
        """Enforces that a transaction, its account, and optional cash bucket belong to the same portfolio."""
        if transaction.portfolio_id != portfolio.id:
            raise ValueError(
                f"Transaction portfolio_id {transaction.portfolio_id} does not match "
                f"portfolio.id {portfolio.id}."
            )
        if account.portfolio_id != portfolio.id:
            raise ValueError(
                f"Account portfolio_id {account.portfolio_id} does not match "
                f"portfolio.id {portfolio.id}."
            )
        if transaction.account_id != account.id:
            raise ValueError(
                f"Transaction account_id {transaction.account_id} does not match "
                f"account.id {account.id}."
            )
        if cash_bucket is not None:
            if cash_bucket.portfolio_id != portfolio.id:
                raise ValueError(
                    f"CashBucket portfolio_id {cash_bucket.portfolio_id} does not match "
                    f"portfolio.id {portfolio.id}."
                )
            if cash_bucket.account_id is not None and cash_bucket.account_id != account.id:
                raise ValueError(
                    f"CashBucket account_id {cash_bucket.account_id} does not match "
                    f"transaction account_id {account.id}."
                )

    @staticmethod
    def validate_goal_consistency(goal: InvestmentGoal, portfolio: Portfolio) -> None:
        """Enforces that a goal belongs to the target portfolio."""
        if goal.portfolio_id != portfolio.id:
            raise ValueError(
                f"Goal portfolio_id {goal.portfolio_id} does not match portfolio.id {portfolio.id}."
            )

    @staticmethod
    def validate_contribution_consistency(
        contribution: PlannedContribution,
        portfolio: Portfolio,
        goal: Optional[InvestmentGoal] = None,
        cash_bucket: Optional[CashBucket] = None,
    ) -> None:
        """Enforces that a planned contribution, its goal, and cash bucket belong to the same portfolio."""
        if contribution.portfolio_id != portfolio.id:
            raise ValueError(
                f"Contribution portfolio_id {contribution.portfolio_id} does not match portfolio.id {portfolio.id}."
            )
        if goal is not None:
            if goal.portfolio_id != portfolio.id:
                raise ValueError(
                    f"Goal portfolio_id {goal.portfolio_id} does not match portfolio.id {portfolio.id}."
                )
            if contribution.goal_id != goal.id:
                raise ValueError(
                    f"Contribution goal_id {contribution.goal_id} does not match goal.id {goal.id}."
                )
        if cash_bucket is not None:
            if cash_bucket.portfolio_id != portfolio.id:
                raise ValueError(
                    f"CashBucket portfolio_id {cash_bucket.portfolio_id} does not match portfolio.id {portfolio.id}."
                )
            if contribution.cash_bucket_id != cash_bucket.id:
                raise ValueError(
                    f"Contribution cash_bucket_id {contribution.cash_bucket_id} does not match cash_bucket.id {cash_bucket.id}."
                )


class PortfolioLedger:
    """
    In-memory immutable transaction ledger for a single Portfolio context.

    Enforces:
        - Binds to root `Portfolio` aggregate (deriving `portfolio_id` and `mode`).
        - Append-only semantics.
        - Strict external source idempotency / conflict detection.
        - Strict reversal validation (no self-reversal, cross-portfolio, cross-account, double reversal).
        - Deterministic audit sorting.
    """

    def __init__(self, portfolio: Portfolio) -> None:
        if not isinstance(portfolio, Portfolio):
            raise TypeError(f"portfolio must be an instance of Portfolio, got {type(portfolio).__name__}")
        self._portfolio: Portfolio = portfolio
        self._portfolio_id: UUID = portfolio.id
        self._mode: PortfolioMode = portfolio.mode
        self._transactions: List[PortfolioTransaction] = []
        self._tx_by_id: Dict[UUID, PortfolioTransaction] = {}
        # Map (portfolio_id, account_id, external_source_norm, external_ref) -> transaction
        self._external_refs: Dict[Tuple[UUID, UUID, str, str], PortfolioTransaction] = {}
        # Map reversed_transaction_id -> reversal_transaction_id
        self._reversals: Dict[UUID, UUID] = {}

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    @property
    def portfolio_id(self) -> UUID:
        return self._portfolio_id

    @property
    def mode(self) -> PortfolioMode:
        return self._mode

    def __len__(self) -> int:
        return len(self._transactions)

    def get_by_id(self, tx_id: UUID) -> Optional[PortfolioTransaction]:
        """Retrieves a transaction by physical ID."""
        return self._tx_by_id.get(tx_id)

    def append(self, tx: PortfolioTransaction) -> AppendResult:
        """
        Attempts to append an immutable transaction to the ledger.
        Enforces idempotency, conflict detection, and reversal invariants.
        """
        # 1. Portfolio boundary check
        if tx.portfolio_id != self._portfolio_id:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=tx.id,
                diagnostics=(
                    f"Transaction portfolio_id {tx.portfolio_id} does not match ledger portfolio_id {self._portfolio_id}.",
                ),
            )

        # 2. Duplicate internal ID check
        if tx.id in self._tx_by_id:
            existing = self._tx_by_id[tx.id]
            if existing.economic_fingerprint() == tx.economic_fingerprint():
                return AppendResult(
                    status=AppendStatus.IDEMPOTENT_DUPLICATE,
                    transaction_id=tx.id,
                    diagnostics=("Transaction with identical internal ID and economics already recorded.",),
                )
            return AppendResult(
                status=AppendStatus.CONFLICT,
                transaction_id=tx.id,
                diagnostics=("Conflict: Physical transaction ID reused with different economics.",),
            )

        # 3. External Reference Idempotency & Conflict Check
        if tx.external_source and tx.external_reference:
            ext_key = (
                tx.portfolio_id,
                tx.account_id,
                tx.external_source.strip().upper(),
                tx.external_reference.strip(),
            )
            if ext_key in self._external_refs:
                existing = self._external_refs[ext_key]
                if existing.economic_fingerprint() == tx.economic_fingerprint():
                    return AppendResult(
                        status=AppendStatus.IDEMPOTENT_DUPLICATE,
                        transaction_id=existing.id,
                        diagnostics=(
                            f"Idempotent duplicate: Event from external source {tx.external_source} "
                            f"ref '{tx.external_reference}' already recorded.",
                        ),
                    )
                return AppendResult(
                    status=AppendStatus.CONFLICT,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Conflict: External reference '{tx.external_reference}' from {tx.external_source} "
                        f"already exists with differing economics.",
                    ),
                )

        # 4. Reversal Validation
        if tx.transaction_type == TransactionType.REVERSAL:
            target_id = tx.reverses_transaction_id
            assert target_id is not None  # Enforced by PortfolioTransaction.__post_init__

            # Target must exist in this ledger
            if target_id not in self._tx_by_id:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(f"Reversal target transaction {target_id} not found in this ledger.",),
                )

            target_tx = self._tx_by_id[target_id]

            # Cross-portfolio reversal rejected
            if target_tx.portfolio_id != tx.portfolio_id:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Cross-portfolio reversal rejected: target portfolio {target_tx.portfolio_id} "
                        f"!= reversal portfolio {tx.portfolio_id}.",
                    ),
                )

            # Cross-account reversal rejected
            if target_tx.account_id != tx.account_id:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Cross-account reversal rejected: target account {target_tx.account_id} "
                        f"!= reversal account {tx.account_id}.",
                    ),
                )

            # Reversal of a reversal rejected
            if target_tx.transaction_type == TransactionType.REVERSAL:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=("Reversal of a reversal transaction is strictly forbidden.",),
                )

            # Double reversal rejected (at most one reversal per transaction)
            if target_id in self._reversals:
                prior_reversal_id = self._reversals[target_id]
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Double reversal rejected: Transaction {target_id} was already reversed "
                        f"by transaction {prior_reversal_id}.",
                    ),
                )

        # 5. Append transaction
        self._transactions.append(tx)
        self._tx_by_id[tx.id] = tx

        if tx.external_source and tx.external_reference:
            ext_key = (
                tx.portfolio_id,
                tx.account_id,
                tx.external_source.strip().upper(),
                tx.external_reference.strip(),
            )
            self._external_refs[ext_key] = tx

        if tx.transaction_type == TransactionType.REVERSAL and tx.reverses_transaction_id is not None:
            self._reversals[tx.reverses_transaction_id] = tx.id

        return AppendResult(
            status=AppendStatus.APPENDED,
            transaction_id=tx.id,
            diagnostics=(),
        )

    def list_transactions(self) -> List[PortfolioTransaction]:
        """
        Returns all transactions in deterministic audit order:
        (effective_date, executed_at or UTC min, recorded_at, id).
        """
        _min_dt = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(
            self._transactions,
            key=lambda t: (
                t.effective_date,
                t.executed_at or _min_dt,
                t.recorded_at,
                str(t.id),
            ),
        )

    def is_reversed(self, tx_id: UUID) -> bool:
        """Returns True if the transaction has been reversed in this ledger."""
        return tx_id in self._reversals

    def get_reversal_transaction_id(self, tx_id: UUID) -> Optional[UUID]:
        """Returns the ID of the reversal transaction that reversed `tx_id`, if any."""
        return self._reversals.get(tx_id)
