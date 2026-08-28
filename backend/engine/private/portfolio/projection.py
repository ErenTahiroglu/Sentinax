"""
backend/engine/private/portfolio/projection.py
==============================================
Pure, Reversal-Aware Point-in-Time Ledger View Foundation (Phase 12C.1).

This module provides a pure, deterministic, in-memory projection layer that:
1. Evaluates authoritative portfolio history at a system-knowledge cutoff (`as_of_recorded_at`).
2. Identifies all known transactions (including audit REVERSAL events).
3. Resolves known reversal references to derive base transaction state (`ProjectedTransactionState`).
4. Determines which non-reversal economic events are active (`active_transactions`).

Invariants:
- Pure Python domain logic: no network, no Supabase, no SQL, no mutable state.
- System-Knowledge cutoff uses `recorded_at` physical UTC instants only.
- Effective date and executed_at are economic fields and do NOT gate system knowledge.
- Future-known reversals NEVER retroactively alter earlier PIT snapshots.
- Fail-closed on corrupted history (cross-portfolio/account reversals, double reversals,
  reversals of reversals, missing targets, duplicate physical IDs, or duplicate persisted external IDs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.domain import PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.normalization import (
    normalize_external_reference,
    normalize_external_source,
)


class PortfolioProjectionError(ValueError):
    """Raised when authoritative transaction history fails structural integrity checks."""
    pass


@dataclass(frozen=True)
class ProjectedTransactionState:
    """Reversal-aware state for a known non-reversal base economic transaction."""
    transaction: PortfolioTransaction
    is_reversed: bool
    reversal_transaction_id: Optional[UUID] = None


@dataclass(frozen=True)
class LedgerProjectionView:
    """Immutable point-in-time projection of portfolio transaction history."""
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    known_transactions: Tuple[PortfolioTransaction, ...]
    transaction_states: Tuple[ProjectedTransactionState, ...]
    active_transactions: Tuple[PortfolioTransaction, ...]


def _canonical_economic_sort_key(tx: PortfolioTransaction) -> Tuple[Any, ...]:
    _min_dt = datetime.min.replace(tzinfo=timezone.utc)
    exec_dt = tx.executed_at.astimezone(timezone.utc) if tx.executed_at is not None else _min_dt
    rec_dt = tx.recorded_at.astimezone(timezone.utc)
    return (
        tx.effective_date,
        exec_dt,
        rec_dt,
        str(tx.id),
    )


def _canonical_audit_sort_key(tx: PortfolioTransaction) -> Tuple[datetime, str]:
    return (
        tx.recorded_at.astimezone(timezone.utc),
        str(tx.id),
    )


def build_ledger_projection_view(
    portfolio: Portfolio,
    transactions: Sequence[PortfolioTransaction],
    as_of_recorded_at: Optional[datetime] = None,
) -> LedgerProjectionView:
    """
    Constructs an immutable, reversal-aware point-in-time projection view of the ledger.

    Args:
        portfolio: Authoritative root Portfolio aggregate.
        transactions: Authoritative persisted sequence of PortfolioTransaction records.
        as_of_recorded_at: Optional system-knowledge cutoff instant. If provided, must be timezone-aware.

    Returns:
        LedgerProjectionView containing known transactions, transaction states, and active transactions.

    Raises:
        TypeError: If arguments are of invalid types.
        ValueError: If as_of_recorded_at is timezone-naive.
        PortfolioProjectionError: If history contains cross-portfolio, duplicate, or corrupted reversal relations.
    """
    if not isinstance(portfolio, Portfolio):
        raise TypeError(f"portfolio must be an instance of Portfolio, got {type(portfolio).__name__}")

    if not isinstance(transactions, (list, tuple, Sequence)):
        raise TypeError("transactions must be a sequence of PortfolioTransaction")

    cutoff_utc: Optional[datetime] = None
    if as_of_recorded_at is not None:
        if isinstance(as_of_recorded_at, bool) or not isinstance(as_of_recorded_at, datetime):
            raise TypeError(f"as_of_recorded_at must be a datetime, got {type(as_of_recorded_at).__name__}")
        if as_of_recorded_at.tzinfo is None or as_of_recorded_at.tzinfo.utcoffset(as_of_recorded_at) is None:
            raise ValueError("as_of_recorded_at must be timezone-aware")
        cutoff_utc = as_of_recorded_at.astimezone(timezone.utc)

    # 1. Authoritative history validation (Fail-closed on corrupted persisted history)
    seen_ids: set[UUID] = set()
    seen_external_keys: set[Tuple[UUID, UUID, str, str]] = set()

    for tx in transactions:
        if not isinstance(tx, PortfolioTransaction):
            raise TypeError(f"All transactions must be PortfolioTransaction instances, got {type(tx).__name__}")

        if tx.portfolio_id != portfolio.id:
            raise PortfolioProjectionError(
                f"Transaction {tx.id} portfolio_id {tx.portfolio_id} does not match portfolio.id {portfolio.id}"
            )

        if tx.id in seen_ids:
            raise PortfolioProjectionError(
                f"Duplicate physical transaction ID detected in authoritative history: {tx.id}"
            )
        seen_ids.add(tx.id)

        if tx.external_source is not None and tx.external_reference is not None:
            norm_src = normalize_external_source(tx.external_source)
            norm_ref = normalize_external_reference(tx.external_reference)
            ext_key = (tx.portfolio_id, tx.account_id, norm_src, norm_ref)
            if ext_key in seen_external_keys:
                raise PortfolioProjectionError(
                    f"Duplicate persisted canonical external identity detected: {ext_key}"
                )
            seen_external_keys.add(ext_key)

    # 2. Filter known events at system knowledge cutoff (recorded_at <= as_of_recorded_at)
    known_raw: List[PortfolioTransaction] = []
    for tx in transactions:
        rec_utc = tx.recorded_at.astimezone(timezone.utc)
        if cutoff_utc is None or rec_utc <= cutoff_utc:
            known_raw.append(tx)

    # 3. Reference-based reversal resolution on known history
    known_by_id: Dict[UUID, PortfolioTransaction] = {tx.id: tx for tx in known_raw}
    reversals_map: Dict[UUID, UUID] = {}  # target_id -> reversal_id

    for tx in known_raw:
        if tx.transaction_type == TransactionType.REVERSAL:
            target_id = tx.reverses_transaction_id
            if target_id is None:
                raise PortfolioProjectionError(f"Reversal transaction {tx.id} missing target ID")

            if target_id == tx.id:
                raise PortfolioProjectionError(f"Self-reversal rejected on transaction {tx.id}")

            if target_id not in known_by_id:
                raise PortfolioProjectionError(
                    f"Reversal transaction {tx.id} references unknown target transaction {target_id} "
                    f"(not known in authoritative history as of cutoff {as_of_recorded_at})"
                )

            target_tx = known_by_id[target_id]
            if target_tx.portfolio_id != tx.portfolio_id:
                raise PortfolioProjectionError(
                    f"Cross-portfolio reversal rejected: reversal {tx.id} portfolio {tx.portfolio_id} "
                    f"!= target {target_id} portfolio {target_tx.portfolio_id}"
                )

            if target_tx.account_id != tx.account_id:
                raise PortfolioProjectionError(
                    f"Cross-account reversal rejected: reversal {tx.id} account {tx.account_id} "
                    f"!= target {target_id} account {target_tx.account_id}"
                )

            if target_tx.transaction_type == TransactionType.REVERSAL:
                raise PortfolioProjectionError(
                    f"Reversal of reversal rejected: target {target_id} is itself a REVERSAL"
                )

            if target_id in reversals_map:
                raise PortfolioProjectionError(
                    f"Double reversal rejected: target {target_id} already reversed by {reversals_map[target_id]}"
                )

            reversals_map[target_id] = tx.id

    # 4. Construct sorted known transactions (system knowledge audit ordering)
    sorted_known = sorted(known_raw, key=_canonical_audit_sort_key)

    # 5. Construct transaction states and active transactions for non-reversal economic events
    non_reversals = [tx for tx in known_raw if tx.transaction_type != TransactionType.REVERSAL]
    sorted_non_reversals = sorted(non_reversals, key=_canonical_economic_sort_key)

    states: List[ProjectedTransactionState] = []
    active: List[PortfolioTransaction] = []

    for tx in sorted_non_reversals:
        is_rev = tx.id in reversals_map
        rev_id = reversals_map.get(tx.id)
        states.append(
            ProjectedTransactionState(
                transaction=tx,
                is_reversed=is_rev,
                reversal_transaction_id=rev_id,
            )
        )
        if not is_rev:
            active.append(tx)

    return LedgerProjectionView(
        portfolio_id=portfolio.id,
        mode=portfolio.mode,
        as_of_recorded_at=as_of_recorded_at,
        known_transactions=tuple(sorted_known),
        transaction_states=tuple(states),
        active_transactions=tuple(active),
    )
