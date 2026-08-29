"""
backend/engine/private/portfolio/fee_tax_attribution_service.py
===============================================================
Owner-Bound Persisted Attribution Semantic Query Service (Phase 14K).

This service connects the owner-scoped persistence layer (`PortfolioRepository`)
with the pure persisted attribution semantic binding pipeline:
`build_ledger_projection_view` + `build_persisted_fee_tax_attribution_history_view`
-> `bind_persisted_fee_tax_attribution_history` -> `PersistedFeeTaxAttributionSemanticView`.

Key Architectural Invariants:
1. Owner Scoping:
   - Inherits owner isolation strictly from the bound PortfolioRepository.
   - No owner_id, user_id, or credential arguments in service methods.
2. Narrow Repository Dependency Port:
   - Requires structural satisfaction of PortfolioFeeTaxAttributionRepositoryPort
     (get_portfolio, list_transactions, list_fee_tax_attribution_events).
   - Fails closed at constructor if repository is missing, malformed, or non-callable.
   - Clock dependency validated as callable at constructor.
3. Response Identity & Collection Integrity:
   - Validates that repository.get_portfolio(id) returns an actual Portfolio whose id matches requested_id.
   - Fails closed if repository returns wrong portfolio or invalid object (list calls are not executed).
   - Validates that repository transaction and attribution collections are valid non-generator Sequences.
4. Explicit Knowledge Cutoff:
   - get_current_attribution_view: captures system clock exactly once per query, validates timezone awareness,
     normalizes to UTC, and uses that exact instant for both ledger and attribution-history projection cutoffs.
   - get_attribution_view_as_of: validates caller's timezone-aware datetime and preserves exact representation.
5. Single Execution Path:
   - get_current_attribution_view and get_attribution_view_as_of delegate to private _build_attribution_view.
6. Fail-Closed Error Propagation:
   - Operational repository errors and lower-level projection/binding errors propagate unchanged.
   - Missing portfolio or contract failures raise PortfolioFeeTaxAttributionQueryError.
7. Pure Read-Only Query Service:
   - No write methods, no internal caching, no state mutation.
   - No fee/tax calculation, no FX conversion, no tax law or liability rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Sequence
from uuid import UUID

from backend.engine.private.portfolio.fee_tax_attribution_binding import (
    PersistedFeeTaxAttributionSemanticView,
    bind_persisted_fee_tax_attribution_history,
)
from backend.engine.private.portfolio.fee_tax_attribution_history import (
    build_persisted_fee_tax_attribution_history_view,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionPersistenceEvent,
)
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.projection import build_ledger_projection_view


class PortfolioFeeTaxAttributionQueryError(ValueError):
    """Raised when portfolio fee/tax attribution query encounters invalid parameters, missing portfolios, or contract violations."""
    pass


class PortfolioFeeTaxAttributionRepositoryPort(Protocol):
    """Narrow structural protocol for owner-bound portfolio fee/tax attribution persistence queries."""

    def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
        ...

    def list_transactions(self, portfolio_id: UUID) -> Sequence[PortfolioTransaction]:
        ...

    def list_fee_tax_attribution_events(
        self,
        portfolio_id: UUID,
        account_id: Optional[UUID] = None,
        as_of_recorded_at: Optional[datetime] = None,
    ) -> Sequence[FeeTaxAttributionPersistenceEvent]:
        ...


def _validate_repository_dependency(repo: Any) -> PortfolioFeeTaxAttributionRepositoryPort:
    """Validates that repository is non-None and structurally satisfies the repository query port."""
    if repo is None:
        raise PortfolioFeeTaxAttributionQueryError("repository must not be None")
    if not hasattr(repo, "get_portfolio") or not callable(getattr(repo, "get_portfolio")):
        raise PortfolioFeeTaxAttributionQueryError(
            "repository must provide a callable get_portfolio(portfolio_id) method"
        )
    if not hasattr(repo, "list_transactions") or not callable(getattr(repo, "list_transactions")):
        raise PortfolioFeeTaxAttributionQueryError(
            "repository must provide a callable list_transactions(portfolio_id) method"
        )
    if not hasattr(repo, "list_fee_tax_attribution_events") or not callable(getattr(repo, "list_fee_tax_attribution_events")):
        raise PortfolioFeeTaxAttributionQueryError(
            "repository must provide a callable list_fee_tax_attribution_events(portfolio_id, account_id, as_of_recorded_at) method"
        )
    return repo


def _validate_clock_dependency(clock: Optional[Callable[[], datetime]]) -> Callable[[], datetime]:
    """Validates that injected clock is either None (defaults to UTC clock) or a callable."""
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    if isinstance(clock, bool) or not callable(clock):
        raise PortfolioFeeTaxAttributionQueryError(
            f"clock must be a callable returning datetime, got {type(clock).__name__}"
        )
    return clock


def _validate_portfolio_id(val: Any) -> UUID:
    """Validates that portfolio_id is an actual non-bool UUID instance."""
    if isinstance(val, bool) or not isinstance(val, UUID):
        raise PortfolioFeeTaxAttributionQueryError(
            f"portfolio_id must be a UUID instance, got {type(val).__name__}"
        )
    return val


def _validate_as_of_recorded_at(val: Any) -> datetime:
    """Validates that as_of_recorded_at is an actual non-bool timezone-aware datetime with non-null utcoffset."""
    if isinstance(val, bool) or not isinstance(val, datetime):
        raise PortfolioFeeTaxAttributionQueryError(
            f"as_of_recorded_at must be a datetime instance, got {type(val).__name__}"
        )
    if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
        raise PortfolioFeeTaxAttributionQueryError(
            f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got: {val}"
        )
    return val


def _resolve_current_clock(clock: Callable[[], datetime]) -> datetime:
    """Invokes system clock, validates timezone awareness, and normalizes to UTC."""
    raw = clock()
    if isinstance(raw, bool) or not isinstance(raw, datetime):
        raise PortfolioFeeTaxAttributionQueryError(
            f"clock must return a datetime instance, got {type(raw).__name__}"
        )
    if raw.tzinfo is None or raw.tzinfo.utcoffset(raw) is None:
        raise PortfolioFeeTaxAttributionQueryError(
            f"clock return value must be timezone-aware with non-null utcoffset, got: {raw}"
        )
    return raw.astimezone(timezone.utc)


class PortfolioFeeTaxAttributionQueryService:
    """
    Owner-bound query service for retrieving persisted fee/tax attribution semantic views.
    """

    def __init__(
        self,
        repository: PortfolioFeeTaxAttributionRepositoryPort,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._repository = _validate_repository_dependency(repository)
        self._clock = _validate_clock_dependency(clock)

    def get_current_attribution_view(
        self,
        portfolio_id: UUID,
    ) -> PersistedFeeTaxAttributionSemanticView:
        """
        Retrieves persisted fee/tax attribution semantic view for the specified portfolio with an explicit
        system knowledge cutoff captured from the clock at query time and normalized to UTC.
        """
        p_id = _validate_portfolio_id(portfolio_id)
        cutoff = _resolve_current_clock(self._clock)
        return self._build_attribution_view(p_id, cutoff)

    def get_attribution_view_as_of(
        self,
        portfolio_id: UUID,
        as_of_recorded_at: datetime,
    ) -> PersistedFeeTaxAttributionSemanticView:
        """
        Retrieves persisted fee/tax attribution semantic view for the specified portfolio
        as of the exact provided recorded_at cutoff timestamp, preserving caller representation.
        """
        p_id = _validate_portfolio_id(portfolio_id)
        cutoff = _validate_as_of_recorded_at(as_of_recorded_at)
        return self._build_attribution_view(p_id, cutoff)

    def _build_attribution_view(
        self,
        portfolio_id: UUID,
        resolved_cutoff: datetime,
    ) -> PersistedFeeTaxAttributionSemanticView:
        """
        Internal shared pipeline: loads portfolio, complete transaction history, and attribution history
        from repository, constructs ledger projection view, derives persisted attribution history view,
        and rebinds semantic attribution view.
        """
        portfolio = self._repository.get_portfolio(portfolio_id)
        if portfolio is None:
            raise PortfolioFeeTaxAttributionQueryError(
                f"Portfolio {portfolio_id} does not exist under the bound owner."
            )

        if not isinstance(portfolio, Portfolio):
            raise PortfolioFeeTaxAttributionQueryError(
                f"Repository returned invalid portfolio object of type {type(portfolio).__name__}"
            )

        if isinstance(portfolio.id, bool) or not isinstance(portfolio.id, UUID) or portfolio.id != portfolio_id:
            raise PortfolioFeeTaxAttributionQueryError(
                f"Repository returned portfolio {portfolio.id} for requested portfolio {portfolio_id}."
            )

        raw_txs = self._repository.list_transactions(portfolio_id)
        if (
            raw_txs is None
            or isinstance(raw_txs, (str, bytes, bytearray, dict, set))
            or not isinstance(raw_txs, Sequence)
        ):
            raise PortfolioFeeTaxAttributionQueryError(
                f"Repository returned invalid transaction collection: {type(raw_txs).__name__}"
            )

        raw_events = self._repository.list_fee_tax_attribution_events(
            portfolio_id=portfolio_id,
            account_id=None,
            as_of_recorded_at=resolved_cutoff,
        )
        if (
            raw_events is None
            or isinstance(raw_events, (str, bytes, bytearray, dict, set))
            or not isinstance(raw_events, Sequence)
        ):
            raise PortfolioFeeTaxAttributionQueryError(
                f"Repository returned invalid attribution events collection: {type(raw_events).__name__}"
            )

        ledger_view = build_ledger_projection_view(
            portfolio=portfolio,
            transactions=raw_txs,
            as_of_recorded_at=resolved_cutoff,
        )

        persisted_history = build_persisted_fee_tax_attribution_history_view(
            portfolio_id=portfolio_id,
            events=raw_events,
            as_of_recorded_at=resolved_cutoff,
        )

        return bind_persisted_fee_tax_attribution_history(
            ledger_view=ledger_view,
            persisted_history=persisted_history,
        )
