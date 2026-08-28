"""
backend/engine/private/portfolio/accounting_service.py
======================================================
Owner-Bound Persisted Accounting Snapshot Query Service (Phase 12C.5 / 12C.5.1).

This service connects the owner-scoped persistence layer (`PortfolioRepository`)
with the pure accounting projection engine (`build_ledger_projection_view` and
`build_portfolio_accounting_snapshot`).

Key Architectural Invariants:
1. Owner Scoping:
   - Inherits owner isolation strictly from the bound PortfolioRepository.
   - No owner_id, user_id, or credential arguments in service methods.
2. Narrow Repository Dependency Port:
   - Requires structural satisfaction of PortfolioAccountingRepositoryPort (get_portfolio, list_transactions).
   - Fails closed at constructor if repository is missing, malformed, or non-callable.
   - Clock dependency validated as callable at constructor.
3. Response Identity & Collection Integrity:
   - Validates that repository.get_portfolio(id) returns an actual Portfolio whose id matches requested_id.
   - Fails closed if repository returns wrong portfolio or invalid object (list_transactions is not called).
   - Validates that repository.list_transactions(id) returns a valid sequence (not None, str, dict, scalar).
4. Explicit Knowledge Cutoff:
   - get_current_snapshot: captures system clock exactly once per query, validates timezone awareness,
     normalizes to UTC, and uses that exact instant as the ledger projection cutoff.
   - get_snapshot_as_of: validates caller's timezone-aware datetime and preserves exact representation.
5. Single Execution Path:
   - get_current_snapshot and get_snapshot_as_of delegate to private _build_snapshot.
6. Fail-Closed Error Propagation:
   - Operational repository errors and lower-level projection errors propagate unchanged.
   - Missing portfolio raises PortfolioAccountingQueryError.
7. Pure Read-Only Query Service:
   - No write methods, no internal caching, no state mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Sequence
from uuid import UUID

from backend.engine.private.portfolio.accounting import (
    PortfolioAccountingSnapshot,
    build_portfolio_accounting_snapshot,
)
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.projection import build_ledger_projection_view


class PortfolioAccountingQueryError(ValueError):
    """Raised when portfolio accounting query encounters invalid parameters, missing portfolios, or contract violations."""
    pass


class PortfolioAccountingRepositoryPort(Protocol):
    """Narrow structural protocol for owner-bound portfolio persistence queries."""

    def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
        ...

    def list_transactions(self, portfolio_id: UUID) -> Sequence[PortfolioTransaction]:
        ...


def _validate_repository_dependency(repo: Any) -> PortfolioAccountingRepositoryPort:
    """Validates that repository is non-None and structurally satisfies the repository query port."""
    if repo is None:
        raise PortfolioAccountingQueryError("repository must not be None")
    if not hasattr(repo, "get_portfolio") or not callable(getattr(repo, "get_portfolio")):
        raise PortfolioAccountingQueryError(
            "repository must provide a callable get_portfolio(portfolio_id) method"
        )
    if not hasattr(repo, "list_transactions") or not callable(getattr(repo, "list_transactions")):
        raise PortfolioAccountingQueryError(
            "repository must provide a callable list_transactions(portfolio_id) method"
        )
    return repo


def _validate_clock_dependency(clock: Optional[Callable[[], datetime]]) -> Callable[[], datetime]:
    """Validates that injected clock is either None (defaults to UTC clock) or a callable."""
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    if isinstance(clock, bool) or not callable(clock):
        raise PortfolioAccountingQueryError(
            f"clock must be a callable returning datetime, got {type(clock).__name__}"
        )
    return clock


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
        repository: PortfolioAccountingRepositoryPort,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._repository = _validate_repository_dependency(repository)
        self._clock = _validate_clock_dependency(clock)

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

        if not isinstance(portfolio, Portfolio):
            raise PortfolioAccountingQueryError(
                f"Repository returned invalid portfolio object of type {type(portfolio).__name__}"
            )

        if isinstance(portfolio.id, bool) or not isinstance(portfolio.id, UUID) or portfolio.id != portfolio_id:
            raise PortfolioAccountingQueryError(
                f"Repository returned portfolio {portfolio.id} for requested portfolio {portfolio_id}."
            )

        raw_txs = self._repository.list_transactions(portfolio_id)
        if raw_txs is None or isinstance(raw_txs, (str, bytes, dict)) or not isinstance(raw_txs, (list, tuple, Sequence)):
            raise PortfolioAccountingQueryError(
                f"Repository returned invalid transaction collection: {type(raw_txs).__name__}"
            )

        ledger_view = build_ledger_projection_view(
            portfolio=portfolio,
            transactions=raw_txs,
            as_of_recorded_at=resolved_cutoff,
        )

        return build_portfolio_accounting_snapshot(ledger_view)
