"""
backend/engine/private/portfolio/fee_tax_attribution_command.py
===============================================================
Owner-Bound Explicit Fee/Tax Allocation Command Service (Phase 14M).

This module implements the application-command orchestration layer for explicit
user/system fee and tax charge allocations.

Workflow:
1. Validates explicit charge ID, target ID, and exact Decimal allocated amount.
2. Rejects self-attribution immediately (charge_id == target_id).
3. Captures the command clock once and normalizes to UTC (T).
4. Generates a unique persistence event UUID once.
5. Queries authoritative semantic state AS OF T via Phase 14K (PortfolioFeeTaxAttributionQueryService).
6. Constructs candidate FeeTaxAttributionIntent and combines with existing active intents.
7. Revalidates entire active intent set via canonical Phase 14D build_observed_fee_tax_attribution_set.
8. Constructs canonical immutable Phase 14E ALLOCATION persistence event via build_allocation_persistence_event.
9. Appends the event via Phase 14L PortfolioRepository.append_fee_tax_attribution_event.
10. Returns the verified persisted FeeTaxAttributionPersistenceEvent instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionIntent,
    build_observed_fee_tax_attribution_set,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
)
from backend.engine.private.portfolio.fee_tax_attribution_service import (
    PortfolioFeeTaxAttributionQueryService,
)
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction


class PortfolioFeeTaxAttributionCommandError(ValueError):
    """Raised when fee/tax attribution command encounters invalid arguments, dependencies, or contract violations."""
    pass


class PortfolioFeeTaxAttributionCommandRepositoryPort(Protocol):
    """Narrow structural protocol for owner-bound portfolio fee/tax attribution commands."""

    def get_portfolio(
        self,
        portfolio_id: UUID,
    ) -> Optional[Portfolio]:
        ...

    def list_transactions(
        self,
        portfolio_id: UUID,
    ) -> Sequence[PortfolioTransaction]:
        ...

    def list_fee_tax_attribution_events(
        self,
        portfolio_id: UUID,
        account_id: Optional[UUID] = None,
        as_of_recorded_at: Optional[datetime] = None,
    ) -> Sequence[FeeTaxAttributionPersistenceEvent]:
        ...

    def append_fee_tax_attribution_event(
        self,
        event: FeeTaxAttributionPersistenceEvent,
    ) -> FeeTaxAttributionPersistenceEvent:
        ...


def _validate_repository_dependency(repo: Any) -> PortfolioFeeTaxAttributionCommandRepositoryPort:
    """Validates that repository is non-None and structurally satisfies the command repository port."""
    if repo is None:
        raise PortfolioFeeTaxAttributionCommandError("repository must not be None")
    for method in (
        "get_portfolio",
        "list_transactions",
        "list_fee_tax_attribution_events",
        "append_fee_tax_attribution_event",
    ):
        if not hasattr(repo, method) or not callable(getattr(repo, method)):
            raise PortfolioFeeTaxAttributionCommandError(
                f"repository must provide a callable {method} method"
            )
    return repo


def _validate_clock_dependency(clock: Optional[Callable[[], datetime]]) -> Callable[[], datetime]:
    """Validates that clock dependency is callable or defaults to UTC now."""
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    if not callable(clock):
        raise PortfolioFeeTaxAttributionCommandError("clock must be a callable returning an aware datetime")
    return clock


def _validate_event_id_factory_dependency(factory: Optional[Callable[[], UUID]]) -> Callable[[], UUID]:
    """Validates that event_id_factory dependency is callable or defaults to uuid4."""
    if factory is None:
        return uuid4
    if not callable(factory):
        raise PortfolioFeeTaxAttributionCommandError("event_id_factory must be a callable returning a UUID")
    return factory


def _validate_uuid_argument(val: Any, field_name: str) -> UUID:
    """Strictly validates that argument is a non-bool UUID instance."""
    if val is None or isinstance(val, bool) or not isinstance(val, UUID):
        raise PortfolioFeeTaxAttributionCommandError(
            f"{field_name} must be a non-bool UUID instance, got {type(val).__name__}: {val!r}"
        )
    return val


def _validate_allocated_amount(amount: Any) -> Decimal:
    """Strictly validates that allocated_amount is a finite, strictly positive Decimal instance (> 0)."""
    if amount is None or isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise PortfolioFeeTaxAttributionCommandError(
            f"allocated_amount must be a Decimal instance, got {type(amount).__name__}: {amount!r}"
        )
    if not amount.is_finite():
        raise PortfolioFeeTaxAttributionCommandError(
            f"allocated_amount must be finite, got: {amount}"
        )
    if amount <= Decimal("0"):
        raise PortfolioFeeTaxAttributionCommandError(
            f"allocated_amount must be strictly positive (> 0), got: {amount}"
        )
    return amount


def _resolve_command_clock(clock: Callable[[], datetime]) -> datetime:
    """Invokes clock once, validates awareness, and normalizes to UTC."""
    try:
        raw_clock = clock()
    except Exception as e:
        if isinstance(e, PortfolioFeeTaxAttributionCommandError):
            raise
        raise PortfolioFeeTaxAttributionCommandError(f"Clock invocation failed: {e}") from e

    if raw_clock is None or isinstance(raw_clock, bool) or not isinstance(raw_clock, datetime):
        raise PortfolioFeeTaxAttributionCommandError(
            f"Clock must return an aware datetime instance, got {type(raw_clock).__name__}: {raw_clock!r}"
        )
    if raw_clock.tzinfo is None or raw_clock.tzinfo.utcoffset(raw_clock) is None:
        raise PortfolioFeeTaxAttributionCommandError(
            f"Clock returned naive datetime or null utcoffset: {raw_clock!r}"
        )
    return raw_clock.astimezone(timezone.utc)


def _resolve_event_id(factory: Callable[[], UUID]) -> UUID:
    """Invokes event-ID factory once and validates UUID return."""
    try:
        val = factory()
    except Exception as e:
        if isinstance(e, PortfolioFeeTaxAttributionCommandError):
            raise
        raise PortfolioFeeTaxAttributionCommandError(f"Event ID factory invocation failed: {e}") from e

    if val is None or isinstance(val, bool) or not isinstance(val, UUID):
        raise PortfolioFeeTaxAttributionCommandError(
            f"Event ID factory must return a non-bool UUID instance, got {type(val).__name__}: {val!r}"
        )
    return val


class PortfolioFeeTaxAttributionCommandService:
    """
    Owner-bound application-command service for explicit fee/tax charge allocation.
    Revalidates candidate allocation against authoritative current ledger & attribution state,
    builds canonical immutable Phase 14E ALLOCATION persistence event, and persists it via Phase 14L repository.
    """

    def __init__(
        self,
        repository: PortfolioFeeTaxAttributionCommandRepositoryPort,
        clock: Optional[Callable[[], datetime]] = None,
        event_id_factory: Optional[Callable[[], UUID]] = None,
    ) -> None:
        self._repo = _validate_repository_dependency(repository)
        self._clock = _validate_clock_dependency(clock)
        self._event_id_factory = _validate_event_id_factory_dependency(event_id_factory)
        self._query_service = PortfolioFeeTaxAttributionQueryService(self._repo)

    def allocate(
        self,
        portfolio_id: UUID,
        charge_transaction_id: UUID,
        target_transaction_id: UUID,
        allocated_amount: Decimal,
    ) -> FeeTaxAttributionPersistenceEvent:
        """
        Executes an explicit fee/tax allocation command:
        1. Validates public arguments strictly.
        2. Rejects self-attribution immediately (charge_id == target_id).
        3. Captures command clock once and normalizes to UTC (T).
        4. Generates unique event ID once.
        5. Queries semantic attribution view AS OF T via Phase 14K.
        6. Constructs candidate FeeTaxAttributionIntent.
        7. Revalidates complete intent set (existing active + candidate) via Phase 14D build_observed_fee_tax_attribution_set.
        8. Builds canonical FeeTaxAttributionPersistenceEvent via Phase 14E build_allocation_persistence_event.
        9. Appends event via Phase 14L append_fee_tax_attribution_event.
        10. Validates returned persisted event and returns it.
        """
        # Step 1 & 2: Public argument strictness
        p_id = _validate_uuid_argument(portfolio_id, "portfolio_id")
        c_id = _validate_uuid_argument(charge_transaction_id, "charge_transaction_id")
        t_id = _validate_uuid_argument(target_transaction_id, "target_transaction_id")
        amount = _validate_allocated_amount(allocated_amount)

        if c_id == t_id:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Self-attribution rejected: charge_transaction_id {c_id} equals target_transaction_id {t_id}"
            )

        # Step 3: Capture single command clock and normalize to UTC
        recorded_at = _resolve_command_clock(self._clock)

        # Step 4: Generate event ID once
        event_id = _resolve_event_id(self._event_id_factory)

        # Step 5: Query authoritative semantic view AS OF T (Phase 14K)
        semantic_view = self._query_service.get_attribution_view_as_of(p_id, recorded_at)

        # Step 6: Construct candidate intent
        candidate_intent = FeeTaxAttributionIntent(
            charge_transaction_id=c_id,
            target_transaction_id=t_id,
            allocated_amount=amount,
        )

        # Step 7: Combine existing active intents + candidate in exact order
        combined_intents = semantic_view.attribution_set.intents + (candidate_intent,)

        # Revalidate via canonical Phase 14D attribution builder
        candidate_set = build_observed_fee_tax_attribution_set(
            semantic_view.observed_projection,
            combined_intents,
        )

        if len(candidate_set.attributions) != len(semantic_view.attribution_set.attributions) + 1:
            raise PortfolioFeeTaxAttributionCommandError(
                "Candidate attribution resolution count mismatch after canonical build."
            )

        resolved_candidate = candidate_set.attributions[-1]

        # Verify candidate binding order
        if (
            resolved_candidate.charge_transaction.id != c_id
            or resolved_candidate.target_transaction.id != t_id
            or resolved_candidate.allocated_amount.as_tuple() != amount.as_tuple()
        ):
            raise PortfolioFeeTaxAttributionCommandError(
                "Resolved candidate attribution does not match requested charge, target, or allocated amount."
            )

        # Step 8: Build canonical Phase 14E ALLOCATION persistence event
        candidate_event = build_allocation_persistence_event(
            event_id=event_id,
            recorded_at=recorded_at,
            attribution=resolved_candidate,
        )

        # Step 9: Append through Phase 14L repository write primitive
        persisted = self._repo.append_fee_tax_attribution_event(candidate_event)

        # Step 10: Defense-in-depth verification of returned persisted event
        if isinstance(persisted, bool) or not isinstance(persisted, FeeTaxAttributionPersistenceEvent):
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned invalid event type: {type(persisted).__name__}"
            )
        if persisted.event_type != FeeTaxAttributionEventType.ALLOCATION:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned wrong event type: {persisted.event_type}"
            )
        if persisted.id != event_id:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned mismatched event ID: expected {event_id}, got {persisted.id}"
            )
        if (
            persisted.portfolio_id != candidate_event.portfolio_id
            or persisted.account_id != candidate_event.account_id
            or persisted.charge_transaction_id != candidate_event.charge_transaction_id
            or persisted.target_transaction_id != candidate_event.target_transaction_id
            or persisted.allocated_amount.as_tuple() != candidate_event.allocated_amount.as_tuple()
        ):
            raise PortfolioFeeTaxAttributionCommandError(
                "Persisted event economic contents do not match candidate allocation."
            )
        if persisted.recorded_at.astimezone(timezone.utc) != candidate_event.recorded_at.astimezone(timezone.utc):
            raise PortfolioFeeTaxAttributionCommandError(
                "Persisted event physical timestamp does not match command recorded_at."
            )

        return persisted
