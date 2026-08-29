"""
backend/engine/private/portfolio/fee_tax_attribution_binding.py
================================================================
Persisted Attribution to Authoritative Ledger Semantic Rebinding (Phase 14J).

This module provides the pure semantic bridge that binds active persisted
fee/tax attribution evidence (Phase 14I PersistedFeeTaxAttributionHistoryView)
to authoritative active ledger transactions (Phase 12C.1 LedgerProjectionView)
and materializes an authoritative ObservedFeeTaxAttributionSet (Phase 14D).

Invariants:
- Pure Python domain composition: no database, no Supabase, no PostgREST,
  no repository, no network, no clock calls, no UUID generation,
  no hashing, no tax-law calculation, no cost-basis modifications, no FX conversions.

- Consumes ONLY active persisted ALLOCATION events from PersistedFeeTaxAttributionHistoryView.
- Preserves exact canonical active event order.
- Revalidates charge is an active FEE/TAX_WITHHOLDING in ObservedFeeTaxProjection.
- Revalidates target is an active transaction in LedgerProjectionView.active_transactions.
- Enforces strict account_id and portfolio_id matching between persisted event and authoritative transactions.
- Preserves exact Decimal allocated_amount representation (.as_tuple()).
- Preserves exact authoritative PortfolioTransaction object identities (is).
- Reuses canonical Phase 14A (build_observed_fee_tax_projection) and Phase 14D (build_observed_fee_tax_attribution_set) authorities.
- Requires exact point-in-time (as_of_recorded_at) metadata representation match between views.
- Strict direct-constructor tamper rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.domain import PortfolioMode
from backend.engine.private.portfolio.fee_tax import (
    ObservedFeeTaxProjection,
    build_observed_fee_tax_projection,
)
from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionIntent,
    ObservedFeeTaxAttributionSet,
    ResolvedFeeTaxAttribution,
    build_observed_fee_tax_attribution_set,
)
from backend.engine.private.portfolio.fee_tax_attribution_history import (
    PersistedFeeTaxAttributionHistoryView,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceEvent,
)
from backend.engine.private.portfolio.models import PortfolioTransaction
from backend.engine.private.portfolio.projection import LedgerProjectionView


class FeeTaxAttributionBindingError(ValueError):
    """Raised when fee/tax attribution semantic rebinding or metadata validation fails closed."""
    pass


def _is_aware_datetime(dt: Any) -> bool:
    """Returns True if dt is a non-bool datetime instance with tzinfo and a non-None utcoffset."""
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


def _derive_attribution_set(
    observed_projection: ObservedFeeTaxProjection,
    ledger_view: LedgerProjectionView,
    persisted_history: PersistedFeeTaxAttributionHistoryView,
) -> ObservedFeeTaxAttributionSet:
    """
    Validates active persisted allocation events against authoritative ledger and observed charge state,
    creates FeeTaxAttributionIntent objects, and builds the canonical ObservedFeeTaxAttributionSet.
    """
    charges_by_id: Dict[UUID, PortfolioTransaction] = {tx.id: tx for tx in observed_projection.events}
    active_by_id: Dict[UUID, PortfolioTransaction] = {
        tx.id: tx for tx in ledger_view.active_transactions
    }

    intents: List[FeeTaxAttributionIntent] = []

    for idx, e in enumerate(persisted_history.active_allocation_events):
        if not isinstance(e, FeeTaxAttributionPersistenceEvent):
            raise FeeTaxAttributionBindingError(
                f"Active event at index {idx} must be FeeTaxAttributionPersistenceEvent, got {type(e).__name__}"
            )
        if e.event_type != FeeTaxAttributionEventType.ALLOCATION:
            raise FeeTaxAttributionBindingError(
                f"Active event {e.id} has invalid event_type {e.event_type}; must be ALLOCATION"
            )
        if isinstance(e.charge_transaction_id, bool) or not isinstance(e.charge_transaction_id, UUID):
            raise FeeTaxAttributionBindingError(
                f"Active event {e.id} charge_transaction_id must be a UUID, got {type(e.charge_transaction_id).__name__}"
            )
        if isinstance(e.target_transaction_id, bool) or not isinstance(e.target_transaction_id, UUID):
            raise FeeTaxAttributionBindingError(
                f"Active event {e.id} target_transaction_id must be a UUID, got {type(e.target_transaction_id).__name__}"
            )
        if isinstance(e.allocated_amount, bool) or not isinstance(e.allocated_amount, Decimal):
            raise FeeTaxAttributionBindingError(
                f"Active event {e.id} allocated_amount must be a Decimal, got {type(e.allocated_amount).__name__}"
            )
        if not e.allocated_amount.is_finite() or e.allocated_amount <= Decimal("0"):
            raise FeeTaxAttributionBindingError(
                f"Active event {e.id} allocated_amount must be finite and positive, got {e.allocated_amount}"
            )
        if e.reverses_attribution_event_id is not None:
            raise FeeTaxAttributionBindingError(
                f"Active ALLOCATION event {e.id} must have reverses_attribution_event_id=None"
            )
        if e.portfolio_id != ledger_view.portfolio_id:
            raise FeeTaxAttributionBindingError(
                f"Active event {e.id} portfolio_id {e.portfolio_id} does not match ledger portfolio_id {ledger_view.portfolio_id}"
            )

        # Rebind to authoritative charge
        if e.charge_transaction_id not in charges_by_id:
            raise FeeTaxAttributionBindingError(
                f"Charge transaction {e.charge_transaction_id} referenced by active allocation {e.id} is not an active FEE or TAX_WITHHOLDING at PIT"
            )
        charge_tx = charges_by_id[e.charge_transaction_id]

        # Rebind to authoritative target
        if e.target_transaction_id not in active_by_id:
            raise FeeTaxAttributionBindingError(
                f"Target transaction {e.target_transaction_id} referenced by active allocation {e.id} is not an active transaction at PIT"
            )
        target_tx = active_by_id[e.target_transaction_id]

        # Defense-in-depth: Account-ID rebinding validation (Item 20)
        if e.account_id != charge_tx.account_id:
            raise FeeTaxAttributionBindingError(
                f"Active allocation event {e.id} account_id {e.account_id} does not match authoritative charge account_id {charge_tx.account_id}"
            )
        if e.account_id != target_tx.account_id:
            raise FeeTaxAttributionBindingError(
                f"Active allocation event {e.id} account_id {e.account_id} does not match authoritative target account_id {target_tx.account_id}"
            )

        # Defense-in-depth: Portfolio-ID rebinding validation (Item 21)
        if e.portfolio_id != charge_tx.portfolio_id:
            raise FeeTaxAttributionBindingError(
                f"Active allocation event {e.id} portfolio_id {e.portfolio_id} does not match authoritative charge portfolio_id {charge_tx.portfolio_id}"
            )
        if e.portfolio_id != target_tx.portfolio_id:
            raise FeeTaxAttributionBindingError(
                f"Active allocation event {e.id} portfolio_id {e.portfolio_id} does not match authoritative target portfolio_id {target_tx.portfolio_id}"
            )

        intent = FeeTaxAttributionIntent(
            charge_transaction_id=e.charge_transaction_id,
            target_transaction_id=e.target_transaction_id,
            allocated_amount=e.allocated_amount,
        )
        intents.append(intent)

    return build_observed_fee_tax_attribution_set(
        observed_projection,
        tuple(intents),
    )


@dataclass(frozen=True)
class PersistedFeeTaxAttributionSemanticView:
    """
    Immutable semantic view binding active persisted attribution history
    to authoritative active ledger transaction objects.
    """
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]

    ledger_view: LedgerProjectionView
    observed_projection: ObservedFeeTaxProjection
    persisted_history: PersistedFeeTaxAttributionHistoryView
    attribution_set: ObservedFeeTaxAttributionSet

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise FeeTaxAttributionBindingError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.mode, bool) or not isinstance(self.mode, PortfolioMode):
            raise FeeTaxAttributionBindingError(
                f"mode must be a PortfolioMode instance, got {type(self.mode).__name__}"
            )
        if self.as_of_recorded_at is not None:
            if isinstance(self.as_of_recorded_at, bool) or not isinstance(self.as_of_recorded_at, datetime):
                raise FeeTaxAttributionBindingError(
                    f"as_of_recorded_at must be None or datetime, got {type(self.as_of_recorded_at).__name__}"
                )
            if not _is_aware_datetime(self.as_of_recorded_at):
                raise FeeTaxAttributionBindingError(
                    f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got {self.as_of_recorded_at}"
                )

        if isinstance(self.ledger_view, bool) or not isinstance(self.ledger_view, LedgerProjectionView):
            raise FeeTaxAttributionBindingError(
                f"ledger_view must be a LedgerProjectionView instance, got {type(self.ledger_view).__name__}"
            )
        if isinstance(self.observed_projection, bool) or not isinstance(self.observed_projection, ObservedFeeTaxProjection):
            raise FeeTaxAttributionBindingError(
                f"observed_projection must be an ObservedFeeTaxProjection instance, got {type(self.observed_projection).__name__}"
            )
        if isinstance(self.persisted_history, bool) or not isinstance(self.persisted_history, PersistedFeeTaxAttributionHistoryView):
            raise FeeTaxAttributionBindingError(
                f"persisted_history must be a PersistedFeeTaxAttributionHistoryView instance, got {type(self.persisted_history).__name__}"
            )
        if isinstance(self.attribution_set, bool) or not isinstance(self.attribution_set, ObservedFeeTaxAttributionSet):
            raise FeeTaxAttributionBindingError(
                f"attribution_set must be an ObservedFeeTaxAttributionSet instance, got {type(self.attribution_set).__name__}"
            )

        # Portfolio ID match across all attached objects
        if (
            self.portfolio_id != self.ledger_view.portfolio_id
            or self.portfolio_id != self.observed_projection.portfolio_id
            or self.portfolio_id != self.persisted_history.portfolio_id
            or self.portfolio_id != self.attribution_set.portfolio_id
        ):
            raise FeeTaxAttributionBindingError(
                f"portfolio_id mismatch across attached views: self={self.portfolio_id}, "
                f"ledger={self.ledger_view.portfolio_id}, observed={self.observed_projection.portfolio_id}, "
                f"history={self.persisted_history.portfolio_id}, attribution_set={self.attribution_set.portfolio_id}"
            )

        # Mode match across all applicable attached objects
        if (
            self.mode != self.ledger_view.mode
            or self.mode != self.observed_projection.mode
            or self.mode != self.attribution_set.mode
        ):
            raise FeeTaxAttributionBindingError(
                f"mode mismatch across attached views: self={self.mode}, "
                f"ledger={self.ledger_view.mode}, observed={self.observed_projection.mode}, "
                f"attribution_set={self.attribution_set.mode}"
            )

        # Exact PIT metadata representation match across all attached objects
        for name, other_dt in [
            ("ledger_view", self.ledger_view.as_of_recorded_at),
            ("observed_projection", self.observed_projection.as_of_recorded_at),
            ("persisted_history", self.persisted_history.as_of_recorded_at),
            ("attribution_set", self.attribution_set.as_of_recorded_at),
        ]:
            if not _is_exact_datetime_representation_equal(self.as_of_recorded_at, other_dt):
                raise FeeTaxAttributionBindingError(
                    f"as_of_recorded_at exact representation mismatch between self ({self.as_of_recorded_at}) and {name} ({other_dt})"
                )

        # Graph identity validation (Phase 14J.1)
        if self.observed_projection.ledger_view is not self.ledger_view:
            raise FeeTaxAttributionBindingError(
                "observed_projection.ledger_view must be the exact attached ledger_view instance"
            )
        if self.attribution_set.observed_projection is not self.observed_projection:
            raise FeeTaxAttributionBindingError(
                "attribution_set.observed_projection must be the exact attached observed_projection instance"
            )

        # Canonical rederivation to detect tampering
        exp_observed = build_observed_fee_tax_projection(self.ledger_view)
        if len(self.observed_projection.events) != len(exp_observed.events):
            raise FeeTaxAttributionBindingError(
                f"Tampered observed_projection events length: got {len(self.observed_projection.events)}, expected {len(exp_observed.events)}"
            )
        for idx, (act_ev, exp_ev) in enumerate(zip(self.observed_projection.events, exp_observed.events)):
            if act_ev is not exp_ev:
                raise FeeTaxAttributionBindingError(
                    f"Tampered observed_projection event at index {idx}: object identity mismatch"
                )

        exp_attribution_set = _derive_attribution_set(
            self.observed_projection,
            self.ledger_view,
            self.persisted_history,
        )

        # Intent validation
        if len(self.attribution_set.intents) != len(exp_attribution_set.intents):
            raise FeeTaxAttributionBindingError(
                f"Tampered attribution_set intents length: got {len(self.attribution_set.intents)}, expected {len(exp_attribution_set.intents)}"
            )
        for idx, (act_intent, exp_intent) in enumerate(zip(self.attribution_set.intents, exp_attribution_set.intents)):
            if isinstance(act_intent, bool) or not isinstance(act_intent, FeeTaxAttributionIntent):
                raise FeeTaxAttributionBindingError(
                    f"Intent at index {idx} must be a FeeTaxAttributionIntent instance, got {type(act_intent).__name__}"
                )
            if act_intent.charge_transaction_id != exp_intent.charge_transaction_id:
                raise FeeTaxAttributionBindingError(
                    f"Tampered intent at index {idx} charge_transaction_id: {act_intent.charge_transaction_id} != {exp_intent.charge_transaction_id}"
                )
            if act_intent.target_transaction_id != exp_intent.target_transaction_id:
                raise FeeTaxAttributionBindingError(
                    f"Tampered intent at index {idx} target_transaction_id: {act_intent.target_transaction_id} != {exp_intent.target_transaction_id}"
                )
            if act_intent.allocated_amount.as_tuple() != exp_intent.allocated_amount.as_tuple():
                raise FeeTaxAttributionBindingError(
                    f"Tampered intent at index {idx} allocated_amount representation: {act_intent.allocated_amount!r} != {exp_intent.allocated_amount!r}"
                )

        # Attribution validation
        if len(self.attribution_set.attributions) != len(exp_attribution_set.attributions):
            raise FeeTaxAttributionBindingError(
                f"Tampered attribution_set attributions length: got {len(self.attribution_set.attributions)}, expected {len(exp_attribution_set.attributions)}"
            )
        for idx, (act_attr, exp_attr) in enumerate(zip(self.attribution_set.attributions, exp_attribution_set.attributions)):
            if act_attr.charge_transaction is not exp_attr.charge_transaction:
                raise FeeTaxAttributionBindingError(
                    f"Tampered attribution at index {idx} charge_transaction: object identity mismatch"
                )
            if act_attr.target_transaction is not exp_attr.target_transaction:
                raise FeeTaxAttributionBindingError(
                    f"Tampered attribution at index {idx} target_transaction: object identity mismatch"
                )
            if act_attr.allocated_amount.as_tuple() != exp_attr.allocated_amount.as_tuple():
                raise FeeTaxAttributionBindingError(
                    f"Tampered attribution at index {idx} allocated_amount: {act_attr.allocated_amount!r} != {exp_attr.allocated_amount!r}"
                )



def bind_persisted_fee_tax_attribution_history(
    ledger_view: LedgerProjectionView,
    persisted_history: PersistedFeeTaxAttributionHistoryView,
) -> PersistedFeeTaxAttributionSemanticView:
    """
    Binds active persisted fee/tax attribution history to authoritative ledger transactions.

    Args:
        ledger_view: Authoritative LedgerProjectionView snapshot.
        persisted_history: Authoritative PersistedFeeTaxAttributionHistoryView snapshot.

    Returns:
        PersistedFeeTaxAttributionSemanticView combining ledger, observed charges, and resolved attributions.

    Raises:
        FeeTaxAttributionBindingError: If arguments fail structural or semantic rebinding invariants.
    """
    if isinstance(ledger_view, bool) or not isinstance(ledger_view, LedgerProjectionView):
        raise FeeTaxAttributionBindingError(
            f"ledger_view must be an instance of LedgerProjectionView, got {type(ledger_view).__name__}"
        )
    if isinstance(persisted_history, bool) or not isinstance(persisted_history, PersistedFeeTaxAttributionHistoryView):
        raise FeeTaxAttributionBindingError(
            f"persisted_history must be an instance of PersistedFeeTaxAttributionHistoryView, got {type(persisted_history).__name__}"
        )

    # Portfolio ID match
    if ledger_view.portfolio_id != persisted_history.portfolio_id:
        raise FeeTaxAttributionBindingError(
            f"portfolio_id mismatch: ledger_view has {ledger_view.portfolio_id}, "
            f"persisted_history has {persisted_history.portfolio_id}"
        )

    # Exact PIT metadata representation match
    if not _is_exact_datetime_representation_equal(
        ledger_view.as_of_recorded_at,
        persisted_history.as_of_recorded_at,
    ):
        raise FeeTaxAttributionBindingError(
            f"Exact as_of_recorded_at representation mismatch: ledger_view has {ledger_view.as_of_recorded_at}, "
            f"persisted_history has {persisted_history.as_of_recorded_at}"
        )

    # Build canonical observed fee/tax projection (Phase 14A authority)
    observed_projection = build_observed_fee_tax_projection(ledger_view)

    # Derive canonical attribution set (Phase 14D authority)
    attribution_set = _derive_attribution_set(
        observed_projection,
        ledger_view,
        persisted_history,
    )

    return PersistedFeeTaxAttributionSemanticView(
        portfolio_id=ledger_view.portfolio_id,
        mode=ledger_view.mode,
        as_of_recorded_at=ledger_view.as_of_recorded_at,
        ledger_view=ledger_view,
        observed_projection=observed_projection,
        persisted_history=persisted_history,
        attribution_set=attribution_set,
    )
