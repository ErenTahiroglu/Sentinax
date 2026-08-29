"""
backend/engine/private/portfolio/fee_tax.py
===========================================
Observed Explicit Fee & Tax-Withholding Event Projection Foundation (Phase 14A).

This module provides a pure, deterministic, point-in-time and reversal-aware in-memory
projection of explicitly recorded FEE and TAX_WITHHOLDING ledger events.

Key Invariants:
- Pure Python domain logic: no network, no Supabase, no SQL, no clock, no UUID generation,
  no hashlib, no tax rates, no legal rules, no FX conversion.
- Sole input authority is LedgerProjectionView.active_transactions (Phase 12C.1).
- Observed-only semantics: captures ONLY actual explicit FEE and TAX_WITHHOLDING events.
  Does NOT calculate tax liability, does NOT estimate future tax, does NOT infer fees,
  and does NOT attribute charges to trades via heuristics.
- Exact object preservation: preserves original PortfolioTransaction instances by object identity (is).
- Preserves upstream canonical ordering from LedgerProjectionView.active_transactions.
- Zero monetary aggregation / arithmetic in Phase 14A: multi-currency amounts remain distinct.
- Strict direct-constructor tamper rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Tuple
from uuid import UUID

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import PortfolioTransaction
from backend.engine.private.portfolio.projection import LedgerProjectionView


class FeeTaxProjectionError(ValueError):
    """Raised when fee/tax projection encounters invalid state or integrity tampering."""
    pass


def _is_aware_datetime(dt: Optional[datetime]) -> bool:
    """Returns True if dt is a non-bool datetime instance with tzinfo and a non-None utcoffset."""
    if dt is None or isinstance(dt, bool) or not isinstance(dt, datetime):
        return False
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


@dataclass(frozen=True)
class ObservedFeeTaxProjection:
    """
    Immutable point-in-time projection of observed explicit FEE and TAX_WITHHOLDING ledger events.

    This projection represents strictly actual recorded charge events known to the ledger view.
    It does NOT compute estimated tax liabilities, tax rates, or synthetic fee estimates.
    """
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    ledger_view: LedgerProjectionView
    events: Tuple[PortfolioTransaction, ...]

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise FeeTaxProjectionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.mode, bool) or not isinstance(self.mode, PortfolioMode):
            raise FeeTaxProjectionError(
                f"mode must be a PortfolioMode instance, got {type(self.mode).__name__}"
            )
        if self.as_of_recorded_at is not None:
            if isinstance(self.as_of_recorded_at, bool) or not isinstance(self.as_of_recorded_at, datetime):
                raise FeeTaxProjectionError(
                    f"as_of_recorded_at must be None or datetime, got {type(self.as_of_recorded_at).__name__}"
                )
            if not _is_aware_datetime(self.as_of_recorded_at):
                raise FeeTaxProjectionError(
                    f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got {self.as_of_recorded_at}"
                )

        if isinstance(self.ledger_view, bool) or not isinstance(self.ledger_view, LedgerProjectionView):
            raise FeeTaxProjectionError(
                f"ledger_view must be a LedgerProjectionView instance, got {type(self.ledger_view).__name__}"
            )

        if not isinstance(self.events, tuple):
            raise FeeTaxProjectionError(
                f"events must be a tuple, got {type(self.events).__name__}"
            )

        # Metadata matching with attached ledger view
        if self.portfolio_id != self.ledger_view.portfolio_id:
            raise FeeTaxProjectionError(
                f"portfolio_id {self.portfolio_id} does not match ledger_view.portfolio_id {self.ledger_view.portfolio_id}"
            )
        if self.mode != self.ledger_view.mode:
            raise FeeTaxProjectionError(
                f"mode {self.mode} does not match ledger_view.mode {self.ledger_view.mode}"
            )
        if self.as_of_recorded_at != self.ledger_view.as_of_recorded_at:
            raise FeeTaxProjectionError(
                f"as_of_recorded_at {self.as_of_recorded_at} does not match ledger_view.as_of_recorded_at {self.ledger_view.as_of_recorded_at}"
            )

        # Canonical expected events from ledger_view
        canonical_expected: Tuple[PortfolioTransaction, ...] = tuple(
            tx
            for tx in self.ledger_view.active_transactions
            if tx.transaction_type in (
                TransactionType.FEE,
                TransactionType.TAX_WITHHOLDING,
            )
        )

        if len(self.events) != len(canonical_expected):
            raise FeeTaxProjectionError(
                f"events count {len(self.events)} does not match canonical filtered event count {len(canonical_expected)}"
            )

        # Exact object identity and position verification
        for idx, (actual, expected) in enumerate(zip(self.events, canonical_expected)):
            if actual is not expected:
                raise FeeTaxProjectionError(
                    f"Event at index {idx} failed exact object-identity check with authoritative ledger view event"
                )

        # Strict boundary revalidation on each event
        for idx, tx in enumerate(self.events):
            if isinstance(tx, bool) or not isinstance(tx, PortfolioTransaction):
                raise FeeTaxProjectionError(
                    f"Event at index {idx} must be a PortfolioTransaction, got {type(tx).__name__}"
                )
            if tx.portfolio_id != self.portfolio_id:
                raise FeeTaxProjectionError(
                    f"Event {tx.id} portfolio_id {tx.portfolio_id} does not match projection {self.portfolio_id}"
                )
            if isinstance(tx.transaction_type, bool) or not isinstance(tx.transaction_type, TransactionType):
                raise FeeTaxProjectionError(
                    f"Event {tx.id} transaction_type must be a TransactionType, got {type(tx.transaction_type).__name__}"
                )
            if tx.transaction_type not in (TransactionType.FEE, TransactionType.TAX_WITHHOLDING):
                raise FeeTaxProjectionError(
                    f"Event {tx.id} has non-charge transaction_type: {tx.transaction_type}"
                )
            if isinstance(tx.cash_amount, bool) or not isinstance(tx.cash_amount, Decimal):
                raise FeeTaxProjectionError(
                    f"Event {tx.id} cash_amount must be a Decimal, got {type(tx.cash_amount).__name__}"
                )
            if not tx.cash_amount.is_finite() or tx.cash_amount <= Decimal("0"):
                raise FeeTaxProjectionError(
                    f"Event {tx.id} cash_amount must be a finite positive Decimal, got {tx.cash_amount}"
                )
            if isinstance(tx.cash_currency, bool) or not isinstance(tx.cash_currency, Currency):
                raise FeeTaxProjectionError(
                    f"Event {tx.id} cash_currency must be a Currency, got {type(tx.cash_currency).__name__}"
                )
            if tx.instrument_id is not None:
                if isinstance(tx.instrument_id, bool) or not isinstance(tx.instrument_id, UUID):
                    raise FeeTaxProjectionError(
                        f"Event {tx.id} instrument_id must be a UUID or None, got {type(tx.instrument_id).__name__}"
                    )

    @property
    def event_count(self) -> int:
        """Total number of observed fee and tax withholding events."""
        return len(self.events)

    @property
    def fee_events(self) -> Tuple[PortfolioTransaction, ...]:
        """Observed explicit FEE transactions."""
        return tuple(tx for tx in self.events if tx.transaction_type == TransactionType.FEE)

    @property
    def tax_withholding_events(self) -> Tuple[PortfolioTransaction, ...]:
        """Observed explicit TAX_WITHHOLDING transactions."""
        return tuple(tx for tx in self.events if tx.transaction_type == TransactionType.TAX_WITHHOLDING)

    @property
    def fee_count(self) -> int:
        """Count of observed explicit FEE transactions."""
        return len(self.fee_events)

    @property
    def tax_withholding_count(self) -> int:
        """Count of observed explicit TAX_WITHHOLDING transactions."""
        return len(self.tax_withholding_events)

    @property
    def instrument_linked_events(self) -> Tuple[PortfolioTransaction, ...]:
        """Observed fee/tax events linked explicitly to an instrument."""
        return tuple(tx for tx in self.events if tx.instrument_id is not None)

    @property
    def account_level_events(self) -> Tuple[PortfolioTransaction, ...]:
        """Observed fee/tax events at the account level (no instrument linkage)."""
        return tuple(tx for tx in self.events if tx.instrument_id is None)


def build_observed_fee_tax_projection(
    view: LedgerProjectionView,
) -> ObservedFeeTaxProjection:
    """
    Derives an immutable point-in-time projection of observed FEE and TAX_WITHHOLDING events.

    Args:
        view: Authoritative LedgerProjectionView from Phase 12C.1.

    Returns:
        ObservedFeeTaxProjection containing filtered charge events and delegated read-only views.

    Raises:
        TypeError: If view is not an instance of LedgerProjectionView.
        FeeTaxProjectionError: If view metadata or events violate structural invariants.
    """
    if isinstance(view, bool) or not isinstance(view, LedgerProjectionView):
        raise TypeError(f"view must be an instance of LedgerProjectionView, got {type(view).__name__}")

    events = tuple(
        tx
        for tx in view.active_transactions
        if tx.transaction_type in (
            TransactionType.FEE,
            TransactionType.TAX_WITHHOLDING,
        )
    )

    return ObservedFeeTaxProjection(
        portfolio_id=view.portfolio_id,
        mode=view.mode,
        as_of_recorded_at=view.as_of_recorded_at,
        ledger_view=view,
        events=events,
    )
