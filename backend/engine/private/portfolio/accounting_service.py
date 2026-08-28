"""
backend/engine/private/portfolio/accounting_service.py
======================================================
Owner-Bound Persisted Accounting Snapshot Query Service (Phase 12C.5).

This service connects the owner-scoped persistence layer (`PortfolioRepository`)
with the pure accounting projection engine (`build_ledger_projection_view` and
`build_portfolio_accounting_snapshot`).

Key Architectural Invariants:
1. Owner Scoping:
   - Inherits owner isolation strictly from the bound PortfolioRepository.
   - No owner_id, user_id, or credential arguments in service methods.
2. Complete Portfolio Projection:
   - Loads complete immutable transaction history for the portfolio via repository.list_transactions(portfolio_id).
   - No account-filtering parameter; projections represent whole portfolios.
3. Explicit Knowledge Cutoff:
   - get_current_snapshot: captures system clock exactly once per query, validates timezone awareness,
     normalizes to UTC, and uses that exact instant as the ledger projection cutoff.
   - get_snapshot_as_of: validates caller's timezone-aware datetime and preserves exact representation.
4. Single Execution Path:
   - get_current_snapshot and get_snapshot_as_of delegate to private _build_snapshot.
5. Fail-Closed Error Propagation:
   - Repository errors and lower-level projection errors propagate unchanged.
   - Missing portfolio raises PortfolioAccountingQueryError.
6. Pure Read-Only Query Service:
   - No write methods, no internal caching, no state mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from backend.engine.private.portfolio.accounting import (
    PortfolioAccountingSnapshot,
    build_portfolio_accounting_snapshot,
)
from backend.engine.private.portfolio.projection import build_ledger_projection_view


class PortfolioAccountingQueryError(ValueError):
    """Raised when portfolio accounting query encounters invalid parameters or missing portfolios."""
    pass


def _validate_portfolio_id(val: Any) -> UUID:
    """Validates that portfolio_id is an actual non-bool UUID instance."""
    if isinstance(val, bool) or not isinstance(val, UUID):
        raise PortfolioAccountingQueryError(
            f"portfolio_id must be a UUID instance, got {type(val).__name__}"
        )
    return val


def _validate_as_of_recorded_at(val: Any) -> datetime:
    """Validates that as_of_recorded_at is an actual non-bool timezone-aware datetime with non-null utcoffset."""
    if isinstance(val, bool) or not isinstance(val, datetime):
        raise PortfolioAccountingQueryError(
            f"as_of_recorded_at must be a datetime instance, got {type(val).__name__}"
        )
    if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
        raise PortfolioAccountingQueryError(
            f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got: {val}"
        )
    return val


def _resolve_current_clock(clock: Callable[[], datetime]) -> datetime:
    """Invokes system clock, validates timezone awareness, and normalizes to UTC."""
    raw = clock()
    if isinstance(raw, bool) or not isinstance(raw, datetime):
        raise PortfolioAccountingQueryError(
            f"clock must return a datetime instance, got {type(raw).__name__}"
        )
    if raw.tzinfo is None or raw.tzinfo.utcoffset(raw) is None:
        raise PortfolioAccountingQueryError(
            f"clock return value must be timezone-aware with non-null utcoffset, got: {raw}"
        )
    return raw.astimezone(timezone.utc)


class PortfolioAccountingQueryService:
    """
    Owner-bound query service for retrieving canonical portfolio accounting snapshots.
    """

    def __init__(
        self,
        repository: Any,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if repository is None:
            raise TypeError("repository must not be None")
        self._repository = repository
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def get_current_snapshot(self, portfolio_id: UUID) -> PortfolioAccountingSnapshot:
        """
        Retrieves a canonical accounting snapshot for the specified portfolio with an explicit
        system knowledge cutoff captured from the system clock at query time.
        """
        p_id = _validate_portfolio_id(portfolio_id)
        cutoff = _resolve_current_clock(self._clock)
        return self._build_snapshot(p_id, cutoff)

    def get_snapshot_as_of(
        self,
        portfolio_id: UUID,
        as_of_recorded_at: datetime,
    ) -> PortfolioAccountingSnapshot:
        """
        Retrieves a point-in-time canonical accounting snapshot for the specified portfolio
        as of the exact provided recorded_at cutoff timestamp.
        """
        p_id = _validate_portfolio_id(portfolio_id)
        cutoff = _validate_as_of_recorded_at(as_of_recorded_at)
        return self._build_snapshot(p_id, cutoff)

    def _build_snapshot(
        self,
        portfolio_id: UUID,
        resolved_cutoff: datetime,
    ) -> PortfolioAccountingSnapshot:
        """
        Internal shared pipeline: loads portfolio and complete transaction history from repository,
        constructs ledger projection view, and returns composed accounting snapshot.
        """
        portfolio = self._repository.get_portfolio(portfolio_id)
        if portfolio is None:
            raise PortfolioAccountingQueryError(
                f"Portfolio {portfolio_id} does not exist under the bound owner."
            )

        transactions = self._repository.list_transactions(portfolio.id)

        ledger_view = build_ledger_projection_view(
            portfolio=portfolio,
            transactions=transactions,
            as_of_recorded_at=resolved_cutoff,
        )

        return build_portfolio_accounting_snapshot(ledger_view)
