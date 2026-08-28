"""
backend/engine/private/portfolio/accounting.py
==============================================
Canonical Portfolio Accounting Snapshot Composition (Phase 12C.4).

This module provides the top-level pure in-memory composition layer that combines
the authoritative `LedgerProjectionView`, exact `PositionQuantityProjection`, and exact
`CashBalanceProjection` into one immutable, cross-projection consistent `PortfolioAccountingSnapshot`.

Invariants:
- Pure Python domain composition: no network, no Supabase, no SQL, no mutable state.
- Input authority is strictly `LedgerProjectionView`.
- Reuses closed builders: `build_position_quantity_projection(view)` and `build_cash_balance_projection(view)`.
- No duplicated financial or economic calculations.
- Strict cross-projection metadata consistency: portfolio_id, mode, and as_of_recorded_at
  must match identically across the ledger view, position projection, and cash projection.
- Exact object binding: preserves original view, positions, and cash instances without copying.
- Fail-closed error propagation: lower-level projection errors propagate unchanged; no partial snapshot is ever produced.
- Fully immutable frozen dataclass representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from backend.engine.private.domain import PortfolioMode
from backend.engine.private.portfolio.cash import (
    CashBalanceProjection,
    build_cash_balance_projection,
)
from backend.engine.private.portfolio.positions import (
    PositionQuantityProjection,
    build_position_quantity_projection,
)
from backend.engine.private.portfolio.projection import LedgerProjectionView


class PortfolioAccountingError(ValueError):
    """Raised when portfolio accounting snapshot composition encounters invalid or inconsistent projections."""
    pass


def _is_aware_datetime(dt: Optional[datetime]) -> bool:
    """
    Returns True if dt is a non-bool datetime instance with tzinfo and a non-None utcoffset.
    """
    if dt is None or isinstance(dt, bool) or not isinstance(dt, datetime):
        return False
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


@dataclass(frozen=True)
class PortfolioAccountingSnapshot:
    """
    Authoritative immutable accounting snapshot composing ledger view,
    position quantities, and account cash balances.
    """
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    ledger_view: LedgerProjectionView
    positions: PositionQuantityProjection
    cash: CashBalanceProjection

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise PortfolioAccountingError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )

        if isinstance(self.mode, bool) or not isinstance(self.mode, PortfolioMode):
            raise PortfolioAccountingError(
                f"mode must be a PortfolioMode instance, got {type(self.mode).__name__}"
            )

        if self.as_of_recorded_at is not None:
            if isinstance(self.as_of_recorded_at, bool) or not isinstance(self.as_of_recorded_at, datetime):
                raise PortfolioAccountingError(
                    f"as_of_recorded_at must be None or datetime, got {type(self.as_of_recorded_at).__name__}"
                )
            if not _is_aware_datetime(self.as_of_recorded_at):
                raise PortfolioAccountingError(
                    f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got naive or null-offset: {self.as_of_recorded_at}"
                )

        if not isinstance(self.ledger_view, LedgerProjectionView):
            raise PortfolioAccountingError(
                f"ledger_view must be a LedgerProjectionView instance, got {type(self.ledger_view).__name__}"
            )

        if not isinstance(self.positions, PositionQuantityProjection):
            raise PortfolioAccountingError(
                f"positions must be a PositionQuantityProjection instance, got {type(self.positions).__name__}"
            )

        if not isinstance(self.cash, CashBalanceProjection):
            raise PortfolioAccountingError(
                f"cash must be a CashBalanceProjection instance, got {type(self.cash).__name__}"
            )

        # Cross-projection identity consistency
        if self.ledger_view.portfolio_id != self.portfolio_id:
            raise PortfolioAccountingError(
                f"ledger_view portfolio_id {self.ledger_view.portfolio_id} does not match snapshot {self.portfolio_id}"
            )

        if self.positions.portfolio_id != self.portfolio_id:
            raise PortfolioAccountingError(
                f"positions portfolio_id {self.positions.portfolio_id} does not match snapshot {self.portfolio_id}"
            )

        if self.cash.portfolio_id != self.portfolio_id:
            raise PortfolioAccountingError(
                f"cash portfolio_id {self.cash.portfolio_id} does not match snapshot {self.portfolio_id}"
            )

        # Cross-projection mode consistency
        if self.ledger_view.mode != self.mode:
            raise PortfolioAccountingError(
                f"ledger_view mode {self.ledger_view.mode} does not match snapshot {self.mode}"
            )

        if self.positions.mode != self.mode:
            raise PortfolioAccountingError(
                f"positions mode {self.positions.mode} does not match snapshot {self.mode}"
            )

        if self.cash.mode != self.mode:
            raise PortfolioAccountingError(
                f"cash mode {self.cash.mode} does not match snapshot {self.mode}"
            )

        # Cross-projection PIT consistency
        if self.ledger_view.as_of_recorded_at != self.as_of_recorded_at:
            raise PortfolioAccountingError(
                f"ledger_view as_of_recorded_at {self.ledger_view.as_of_recorded_at} does not match snapshot {self.as_of_recorded_at}"
            )

        if self.positions.as_of_recorded_at != self.as_of_recorded_at:
            raise PortfolioAccountingError(
                f"positions as_of_recorded_at {self.positions.as_of_recorded_at} does not match snapshot {self.as_of_recorded_at}"
            )

        if self.cash.as_of_recorded_at != self.as_of_recorded_at:
            raise PortfolioAccountingError(
                f"cash as_of_recorded_at {self.cash.as_of_recorded_at} does not match snapshot {self.as_of_recorded_at}"
            )


def build_portfolio_accounting_snapshot(
    view: LedgerProjectionView,
) -> PortfolioAccountingSnapshot:
    """
    Composes a canonical accounting snapshot from an authoritative LedgerProjectionView.

    Executes closed PositionQuantityProjection and CashBalanceProjection builders
    against the supplied view and encapsulates them in a cross-validated, immutable snapshot.

    Args:
        view: Authoritative LedgerProjectionView from Phase 12C.1.

    Returns:
        PortfolioAccountingSnapshot containing the original ledger view, derived positions, and derived cash.

    Raises:
        TypeError: If view is not a LedgerProjectionView instance.
        PositionProjectionError: If position quantity derivation fails closed.
        CashProjectionError: If cash balance derivation fails closed.
        PortfolioAccountingError: If internal cross-projection consistency checks fail.
    """
    if not isinstance(view, LedgerProjectionView):
        raise TypeError(f"view must be an instance of LedgerProjectionView, got {type(view).__name__}")

    positions = build_position_quantity_projection(view)
    cash = build_cash_balance_projection(view)

    return PortfolioAccountingSnapshot(
        portfolio_id=view.portfolio_id,
        mode=view.mode,
        as_of_recorded_at=view.as_of_recorded_at,
        ledger_view=view,
        positions=positions,
        cash=cash,
    )
