"""
backend/engine/private/portfolio/fee_tax_attribution_command.py
===============================================================
Owner-Bound Explicit Fee/Tax Allocation & Reversal Command Service (Phase 14M / 14M.1 / 14N).

This module implements the application-command orchestration layer for explicit
user/system fee and tax charge allocations and attribution reversals with retry-safe command idempotency.

Allocation Workflow (Phase 14M / 14M.1):
1. Validates caller-supplied stable command_id (UUID), charge ID, target ID, and exact Decimal allocated amount.
2. Rejects self-attribution immediately (charge_id == target_id).
3. Pre-reads existing event by (portfolio_id, command_id) before clock invocation.
   - If exact logical command matches existing persisted event, returns it immediately (safe sequential retry).
   - If existing event has different semantics or family, fails closed with command conflict.
4. For genuinely new commands, captures command clock once and normalizes to UTC (T).
5. Queries authoritative semantic state AS OF T via Phase 14K (PortfolioFeeTaxAttributionQueryService).
6. Constructs candidate FeeTaxAttributionIntent and combines with existing active intents.
7. Revalidates entire active intent set via canonical Phase 14D build_observed_fee_tax_attribution_set.
8. Constructs canonical immutable Phase 14E ALLOCATION persistence event with event_id=command_id.
9. Appends the event via Phase 14L PortfolioRepository.append_fee_tax_attribution_event.
   - On error, executes post-error idempotency recovery against (portfolio_id, command_id) to handle concurrent same-command races.
10. Validates and returns the durable persisted FeeTaxAttributionPersistenceEvent instance.

Reversal Workflow (Phase 14N):
1. Validates caller-supplied stable command_id (UUID), portfolio ID, and allocation_event_id.
2. Rejects self-reversal immediately (command_id == allocation_event_id).
3. Pre-reads existing event by (portfolio_id, command_id) before clock invocation.
   - If exact logical reversal matches existing persisted event, returns it immediately (safe sequential retry).
   - If existing event has different semantics or family, fails closed with command conflict.
4. For genuinely new reversals, captures command clock once and normalizes to UTC (T).
5. Queries authoritative semantic state AS OF T via Phase 14K (PortfolioFeeTaxAttributionQueryService).
6. Locates referenced allocation event in Phase 14I history:
   - Proves referenced allocation exists and was recorded at or before T.
   - Proves referenced allocation is an ALLOCATION event and is currently ACTIVE.
   - Verifies one-to-one correspondence against Phase 14J authoritative semantic attribution binding.
7. Constructs canonical immutable Phase 14E REVERSAL persistence event with event_id=command_id.
8. Appends the event via Phase 14L PortfolioRepository.append_fee_tax_attribution_event.
   - On error, executes post-error idempotency recovery against (portfolio_id, command_id) to handle concurrent same-command races.
9. Validates and returns the durable persisted FeeTaxAttributionPersistenceEvent instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Protocol, Sequence
from uuid import UUID

from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionIntent,
    build_observed_fee_tax_attribution_set,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
    build_attribution_reversal_persistence_event,
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

    def get_fee_tax_attribution_event(
        self,
        portfolio_id: UUID,
        event_id: UUID,
    ) -> Optional[FeeTaxAttributionPersistenceEvent]:
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
        "get_fee_tax_attribution_event",
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


def _allocation_event_matches_command(
    event: Any,
    *,
    command_id: UUID,
    portfolio_id: UUID,
    charge_transaction_id: UUID,
    target_transaction_id: UUID,
    allocated_amount: Decimal,
) -> bool:
    """
    Checks whether an existing persisted event represents the exact logical ALLOCATION command.
    Matches physical command ID, portfolio ID, charge ID, target ID, exact Decimal representation (.as_tuple()),
    and ensures it is a non-reversal ALLOCATION event.
    Note: recorded_at and account_id are owned by the first durable commit and are not part of retry matching.
    """
    if event is None or isinstance(event, bool) or not isinstance(event, FeeTaxAttributionPersistenceEvent):
        return False
    if event.id != command_id:
        return False
    if event.portfolio_id != portfolio_id:
        return False
    if event.event_type != FeeTaxAttributionEventType.ALLOCATION:
        return False
    if event.charge_transaction_id != charge_transaction_id:
        return False
    if event.target_transaction_id != target_transaction_id:
        return False
    if event.allocated_amount is None or isinstance(event.allocated_amount, bool) or not isinstance(event.allocated_amount, Decimal):
        return False
    if event.allocated_amount.as_tuple() != allocated_amount.as_tuple():
        return False
    if event.reverses_attribution_event_id is not None:
        return False
    return True


def _reversal_event_matches_command(
    event: Any,
    *,
    command_id: UUID,
    portfolio_id: UUID,
    allocation_event_id: UUID,
) -> bool:
    """
    Checks whether an existing persisted event represents the exact logical REVERSAL command.
    Matches physical command ID, portfolio ID, referenced allocation ID, and ensures it is a valid REVERSAL event.
    Note: recorded_at and account_id are owned by the first durable commit and are not part of retry matching.
    """
    if event is None or isinstance(event, bool) or not isinstance(event, FeeTaxAttributionPersistenceEvent):
        return False
    if event.id != command_id:
        return False
    if event.portfolio_id != portfolio_id:
        return False
    if event.event_type != FeeTaxAttributionEventType.REVERSAL:
        return False
    if event.charge_transaction_id is not None:
        return False
    if event.target_transaction_id is not None:
        return False
    if event.allocated_amount is not None:
        return False
    if event.reverses_attribution_event_id != allocation_event_id:
        return False
    return True


class PortfolioFeeTaxAttributionCommandService:
    """
    Owner-bound application-command service for explicit fee/tax charge allocation and reversal with retry-safe idempotency.
    Revalidates candidate allocation/reversal against authoritative current ledger & attribution state,
    builds canonical immutable Phase 14E persistence events, and persists them via Phase 14L repository.
    """

    def __init__(
        self,
        repository: PortfolioFeeTaxAttributionCommandRepositoryPort,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._repo = _validate_repository_dependency(repository)
        self._clock = _validate_clock_dependency(clock)
        self._query_service = PortfolioFeeTaxAttributionQueryService(self._repo)

    def allocate(
        self,
        command_id: UUID,
        portfolio_id: UUID,
        charge_transaction_id: UUID,
        target_transaction_id: UUID,
        allocated_amount: Decimal,
    ) -> FeeTaxAttributionPersistenceEvent:
        """
        Executes an explicit fee/tax allocation command:
        1. Validates public arguments strictly.
        2. Rejects self-attribution immediately (charge_id == target_id).
        3. Pre-reads existing event by (portfolio_id, command_id) before clock invocation.
           - If exact logical command match: returns existing event immediately (first commit wins).
           - If command conflict: raises PortfolioFeeTaxAttributionCommandError.
        4. Captures command clock once and normalizes to UTC (T).
        5. Queries semantic attribution view AS OF T via Phase 14K.
        6. Constructs candidate FeeTaxAttributionIntent.
        7. Revalidates complete intent set (existing active + candidate) via Phase 14D build_observed_fee_tax_attribution_set.
        8. Builds canonical FeeTaxAttributionPersistenceEvent with event_id=command_id.
        9. Appends event via Phase 14L append_fee_tax_attribution_event with post-error race recovery.
        10. Validates returned persisted event and returns it.
        """
        # Step 1 & 2: Public argument strictness
        cmd_id = _validate_uuid_argument(command_id, "command_id")
        p_id = _validate_uuid_argument(portfolio_id, "portfolio_id")
        c_id = _validate_uuid_argument(charge_transaction_id, "charge_transaction_id")
        t_id = _validate_uuid_argument(target_transaction_id, "target_transaction_id")
        amount = _validate_allocated_amount(allocated_amount)

        if c_id == t_id:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Self-attribution rejected: charge_transaction_id {c_id} equals target_transaction_id {t_id}"
            )

        # Step 3: Pre-read idempotency check before clock
        existing = self._repo.get_fee_tax_attribution_event(p_id, cmd_id)
        if existing is not None:
            if _allocation_event_matches_command(
                existing,
                command_id=cmd_id,
                portfolio_id=p_id,
                charge_transaction_id=c_id,
                target_transaction_id=t_id,
                allocated_amount=amount,
            ):
                return existing
            raise PortfolioFeeTaxAttributionCommandError(
                f"Command ID conflict: Fee/tax attribution event {cmd_id} already exists with different semantics."
            )

        # Step 4: Capture single command clock and normalize to UTC
        recorded_at = _resolve_command_clock(self._clock)

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

        # Step 8: Build canonical Phase 14E ALLOCATION persistence event with event_id=command_id
        candidate_event = build_allocation_persistence_event(
            event_id=cmd_id,
            recorded_at=recorded_at,
            attribution=resolved_candidate,
        )

        # Step 9: Append through Phase 14L with post-error idempotency recovery
        try:
            persisted = self._repo.append_fee_tax_attribution_event(candidate_event)
        except Exception as orig_exc:
            try:
                existing_after_error = self._repo.get_fee_tax_attribution_event(p_id, cmd_id)
            except Exception:
                raise orig_exc from None

            if existing_after_error is not None:
                if _allocation_event_matches_command(
                    existing_after_error,
                    command_id=cmd_id,
                    portfolio_id=p_id,
                    charge_transaction_id=c_id,
                    target_transaction_id=t_id,
                    allocated_amount=amount,
                ):
                    return existing_after_error
                raise PortfolioFeeTaxAttributionCommandError(
                    f"Command ID conflict: Fee/tax attribution event {cmd_id} already exists with different semantics."
                ) from orig_exc

            raise orig_exc

        # Step 10: Defense-in-depth verification of returned persisted event
        if isinstance(persisted, bool) or not isinstance(persisted, FeeTaxAttributionPersistenceEvent):
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned invalid event type: {type(persisted).__name__}"
            )
        if persisted.event_type != FeeTaxAttributionEventType.ALLOCATION:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned wrong event type: {persisted.event_type}"
            )
        if persisted.id != cmd_id:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned mismatched event ID: expected {cmd_id}, got {persisted.id}"
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

    def reverse_allocation(
        self,
        command_id: UUID,
        portfolio_id: UUID,
        allocation_event_id: UUID,
    ) -> FeeTaxAttributionPersistenceEvent:
        """
        Executes an explicit fee/tax attribution reversal command (Phase 14N):
        1. Validates public arguments strictly.
        2. Rejects self-reversal immediately (command_id == allocation_event_id).
        3. Pre-reads existing event by (portfolio_id, command_id) before clock invocation.
           - If exact logical reversal match: returns existing event immediately (first commit wins).
           - If command conflict: raises PortfolioFeeTaxAttributionCommandError.
        4. Captures command clock once and normalizes to UTC (T).
        5. Queries semantic attribution view AS OF T via Phase 14K.
        6. Locates referenced allocation event in Phase 14I history:
           - Verifies allocation exists and was recorded at or before T.
           - Verifies allocation is an ALLOCATION event and is currently ACTIVE.
           - Defensively verifies active correspondence against Phase 14J semantic attribution graph.
        7. Builds canonical Phase 14E REVERSAL persistence event with event_id=command_id.
        8. Appends event via Phase 14L append_fee_tax_attribution_event with post-error race recovery.
        9. Validates returned persisted event and returns it.
        """
        # Step 1 & 2: Public argument strictness
        cmd_id = _validate_uuid_argument(command_id, "command_id")
        p_id = _validate_uuid_argument(portfolio_id, "portfolio_id")
        alloc_id = _validate_uuid_argument(allocation_event_id, "allocation_event_id")

        if cmd_id == alloc_id:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Self-reversal rejected: command_id {cmd_id} equals allocation_event_id {alloc_id}"
            )

        # Step 3: Pre-read idempotency check before clock
        existing = self._repo.get_fee_tax_attribution_event(p_id, cmd_id)
        if existing is not None:
            if _reversal_event_matches_command(
                existing,
                command_id=cmd_id,
                portfolio_id=p_id,
                allocation_event_id=alloc_id,
            ):
                return existing
            raise PortfolioFeeTaxAttributionCommandError(
                f"Command ID conflict: Fee/tax attribution event {cmd_id} already exists with different semantics."
            )

        # Step 4: Capture single command clock and normalize to UTC
        recorded_at = _resolve_command_clock(self._clock)

        # Step 5: Query authoritative semantic view AS OF T (Phase 14K)
        semantic_view = self._query_service.get_attribution_view_as_of(p_id, recorded_at)

        # Step 6: Locate referenced allocation event in Phase 14I history
        history = semantic_view.persisted_history
        alloc_event: Optional[FeeTaxAttributionPersistenceEvent] = None
        for ev in history.allocation_events:
            if ev.id == alloc_id:
                alloc_event = ev
                break

        if alloc_event is None:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Allocation event {alloc_id} not found in persisted attribution history as of {recorded_at.isoformat()}"
            )

        if alloc_event.event_type != FeeTaxAttributionEventType.ALLOCATION:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Target event {alloc_id} is not an ALLOCATION event (got {alloc_event.event_type})"
            )

        # Step 7: Check allocation is currently active in history
        if not history.is_allocation_active(alloc_id):
            existing_rev = history.reversal_for_allocation(alloc_id)
            rev_info = f" (reversed by event {existing_rev.id})" if existing_rev is not None else ""
            raise PortfolioFeeTaxAttributionCommandError(
                f"Allocation event {alloc_id} is not active at PIT cutoff {recorded_at.isoformat()}{rev_info}"
            )

        # Step 8: Defense-in-depth: one-to-one active history and semantic attribution correspondence
        active_allocs = history.active_allocation_events
        try:
            active_idx = active_allocs.index(alloc_event)
        except ValueError:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Active allocation event {alloc_id} missing from active allocation list."
            )

        if active_idx >= len(semantic_view.attribution_set.attributions):
            raise PortfolioFeeTaxAttributionCommandError(
                "Semantic attribution index out of bounds for active allocation event."
            )

        resolved_attr = semantic_view.attribution_set.attributions[active_idx]
        if (
            resolved_attr.charge_transaction.id != alloc_event.charge_transaction_id
            or resolved_attr.target_transaction.id != alloc_event.target_transaction_id
            or (
                alloc_event.allocated_amount is not None
                and resolved_attr.allocated_amount.as_tuple() != alloc_event.allocated_amount.as_tuple()
            )
            or resolved_attr.charge_transaction.portfolio_id != alloc_event.portfolio_id
            or resolved_attr.charge_transaction.account_id != alloc_event.account_id
            or resolved_attr.target_transaction.portfolio_id != alloc_event.portfolio_id
            or resolved_attr.target_transaction.account_id != alloc_event.account_id
        ):
            raise PortfolioFeeTaxAttributionCommandError(
                "Authoritative semantic attribution binding mismatch for allocation event."
            )

        # Step 9: Build canonical Phase 14E REVERSAL persistence event
        candidate_reversal = build_attribution_reversal_persistence_event(
            event_id=cmd_id,
            portfolio_id=alloc_event.portfolio_id,
            account_id=alloc_event.account_id,
            recorded_at=recorded_at,
            reverses_attribution_event_id=alloc_event.id,
        )

        # Step 10: Append through Phase 14L with post-error race recovery
        try:
            persisted = self._repo.append_fee_tax_attribution_event(candidate_reversal)
        except Exception as orig_exc:
            try:
                existing_after_error = self._repo.get_fee_tax_attribution_event(p_id, cmd_id)
            except Exception:
                raise orig_exc from None

            if existing_after_error is not None:
                if _reversal_event_matches_command(
                    existing_after_error,
                    command_id=cmd_id,
                    portfolio_id=p_id,
                    allocation_event_id=alloc_id,
                ):
                    return existing_after_error
                raise PortfolioFeeTaxAttributionCommandError(
                    f"Command ID conflict: Fee/tax attribution event {cmd_id} already exists with different semantics."
                ) from orig_exc

            raise orig_exc

        # Step 11: Defense-in-depth verification of returned persisted event
        if isinstance(persisted, bool) or not isinstance(persisted, FeeTaxAttributionPersistenceEvent):
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned invalid event type: {type(persisted).__name__}"
            )
        if persisted.event_type != FeeTaxAttributionEventType.REVERSAL:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned wrong event type: {persisted.event_type}"
            )
        if persisted.id != cmd_id:
            raise PortfolioFeeTaxAttributionCommandError(
                f"Repository returned mismatched event ID: expected {cmd_id}, got {persisted.id}"
            )
        if (
            persisted.portfolio_id != candidate_reversal.portfolio_id
            or persisted.account_id != candidate_reversal.account_id
            or persisted.reverses_attribution_event_id != candidate_reversal.reverses_attribution_event_id
            or persisted.charge_transaction_id is not None
            or persisted.target_transaction_id is not None
            or persisted.allocated_amount is not None
        ):
            raise PortfolioFeeTaxAttributionCommandError(
                "Persisted event contents do not match candidate reversal."
            )
        if persisted.recorded_at.astimezone(timezone.utc) != candidate_reversal.recorded_at.astimezone(timezone.utc):
            raise PortfolioFeeTaxAttributionCommandError(
                "Persisted event physical timestamp does not match command recorded_at."
            )

        return persisted
