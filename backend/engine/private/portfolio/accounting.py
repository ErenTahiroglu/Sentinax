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


def _is_exact_datetime_representation_equal(
    dt1: Optional[datetime],
    dt2: Optional[datetime],
) -> bool:
    """
    Returns True if dt1 and dt2 have the exact same wall-clock and timezone representation
    (year, month, day, hour, minute, second, microsecond, fold, and utcoffset).
    Prevents different offsets representing the same physical instant from being considered equal.
    """
    if dt1 is None and dt2 is None:
        return True
    if dt1 is None or dt2 is None:
        return False
    if not _is_aware_datetime(dt1) or not _is_aware_datetime(dt2):
        return False

    offset1 = dt1.tzinfo.utcoffset(dt1) if dt1.tzinfo else None
    offset2 = dt2.tzinfo.utcoffset(dt2) if dt2.tzinfo else None

    rep1 = (
        dt1.year,
        dt1.month,
        dt1.day,
        dt1.hour,
        dt1.minute,
        dt1.second,
        dt1.microsecond,
        dt1.fold,
        offset1,
    )
    rep2 = (
        dt2.year,
        dt2.month,
        dt2.day,
        dt2.hour,
        dt2.minute,
        dt2.second,
        dt2.microsecond,
        dt2.fold,
        offset2,
    )
    return rep1 == rep2


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

        # Cross-projection PIT representation consistency
        if not _is_exact_datetime_representation_equal(self.ledger_view.as_of_recorded_at, self.as_of_recorded_at):
            raise PortfolioAccountingError(
                f"ledger_view as_of_recorded_at {self.ledger_view.as_of_recorded_at} does not match snapshot {self.as_of_recorded_at}"
            )

        if not _is_exact_datetime_representation_equal(self.positions.as_of_recorded_at, self.as_of_recorded_at):
            raise PortfolioAccountingError(
                f"positions as_of_recorded_at {self.positions.as_of_recorded_at} does not match snapshot {self.as_of_recorded_at}"
            )

        if not _is_exact_datetime_representation_equal(self.cash.as_of_recorded_at, self.as_of_recorded_at):
            raise PortfolioAccountingError(
                f"cash as_of_recorded_at {self.cash.as_of_recorded_at} does not match snapshot {self.as_of_recorded_at}"
            )

        # Canonical projection provenance validation against ledger_view
        # Lower-layer errors (PositionProjectionError, CashProjectionError) propagate unchanged.
        canonical_positions = build_position_quantity_projection(self.ledger_view)
        if self.positions != canonical_positions:
            raise PortfolioAccountingError("positions projection is not canonical for ledger_view")

        canonical_cash = build_cash_balance_projection(self.ledger_view)
        if self.cash != canonical_cash:
            raise PortfolioAccountingError("cash projection is not canonical for ledger_view")


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
