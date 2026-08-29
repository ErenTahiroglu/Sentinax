"""
backend/engine/private/portfolio/fee_tax_attribution.py
======================================================
Explicit Fee/Tax Charge-to-Transaction Attribution Evidence Foundation (Phase 14D).

This module provides a pure, deterministic, point-in-time and reversal-aware in-memory
evidence layer connecting explicitly recorded FEE and TAX_WITHHOLDING ledger events
to one or more active economic transactions, without heuristics and without modifying
underlying ledger transactions.

Key Architectural Invariants:
- Pure Python domain logic: no network, no Supabase, no SQL, no clock, no UUID generation,
  no hashlib, no tax rates, no legal rules, no FX conversion.
- Sole input authority is ObservedFeeTaxProjection (Phase 14A), with target transactions
  validated against observed_projection.ledger_view.active_transactions (Phase 12C.1).
- Explicit evidence only: zero automatic or heuristic charge attribution (no matching
  by same instrument, same date, nearest timestamp, same amount, or same account).
- Multi-target allocation: a single charge may be allocated across multiple economic targets.
- Partial allocation allowed: allocated amount <= charge amount (unallocated remainder is preserved).
- Over-allocation rejected: exact context-independent Decimal sum of allocations cannot exceed charge amount.
- Same account & portfolio required: cross-account and cross-portfolio attributions are strictly rejected.
- Target must be active economic event: BUY, SELL, DIVIDEND, INTEREST, CASH_DEPOSIT, CASH_WITHDRAWAL, FX_CONVERSION.
  FEE, TAX_WITHHOLDING, and REVERSAL are strictly rejected as attribution targets.
- Exact object preservation: preserves original PortfolioTransaction instances by object identity (is).
- Preserves caller-supplied explicit intent order.
- Strict direct-constructor tamper rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from backend.engine.private.domain import PortfolioMode, TransactionType
from backend.engine.private.portfolio.fee_tax import ObservedFeeTaxProjection
from backend.engine.private.portfolio.models import PortfolioTransaction


class FeeTaxAttributionError(ValueError):
    """Raised when fee/tax attribution encounters invalid state, illegal targets, over-allocation, or tampering."""
    pass


def _is_aware_datetime(dt: Optional[datetime]) -> bool:
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


def _exact_decimal_sum(amounts: Iterable[Decimal]) -> Decimal:
    """
    Computes an exact arbitrary-precision Decimal sum independent of ambient Decimal context precision.
    Preserves exact representation scale based on the minimum exponent of the input values.
    """
    items = list(amounts)
    if not items:
        return Decimal("0")

    parsed_items: List[Tuple[int, int]] = []
    min_exp: Optional[int] = None

    for dec in items:
        if isinstance(dec, bool) or not isinstance(dec, Decimal):
            raise FeeTaxAttributionError(f"Expected Decimal, got {type(dec).__name__}")
        if not dec.is_finite():
            raise FeeTaxAttributionError("Non-finite Decimal rejected")

        sign, digits, exp = dec.as_tuple()
        int_coeff = 0
        for d in digits:
            int_coeff = int_coeff * 10 + d
        if sign == 1:
            int_coeff = -int_coeff

        parsed_items.append((int_coeff, exp))
        if min_exp is None or exp < min_exp:
            min_exp = exp

    assert min_exp is not None
    total_int = 0
    for int_coeff, exp in parsed_items:
        shift = exp - min_exp
        total_int += int_coeff * (10 ** shift)

    if total_int == 0:
        return Decimal("0")

    res_sign = 0 if total_int >= 0 else 1
    res_abs = abs(total_int)
    res_digits = tuple(int(c) for c in str(res_abs))
    return Decimal((res_sign, res_digits, min_exp))


def _exact_decimal_sub(a: Decimal, b: Decimal) -> Decimal:
    """
    Computes an exact arbitrary-precision Decimal subtraction (a - b)
    independent of ambient Decimal context precision.
    """
    if isinstance(a, bool) or not isinstance(a, Decimal) or not a.is_finite():
        raise FeeTaxAttributionError(f"Expected finite Decimal for a, got {a!r}")
    if isinstance(b, bool) or not isinstance(b, Decimal) or not b.is_finite():
        raise FeeTaxAttributionError(f"Expected finite Decimal for b, got {b!r}")

    sign_b, digits_b, exp_b = b.as_tuple()
    neg_b_sign = 0 if sign_b == 1 else 1
    if not digits_b or (len(digits_b) == 1 and digits_b[0] == 0):
        neg_b = Decimal((0, digits_b, exp_b))
    else:
        neg_b = Decimal((neg_b_sign, digits_b, exp_b))
    return _exact_decimal_sum([a, neg_b])


_VALID_TARGET_TYPES = {
    TransactionType.BUY,
    TransactionType.SELL,
    TransactionType.DIVIDEND,
    TransactionType.INTEREST,
    TransactionType.CASH_DEPOSIT,
    TransactionType.CASH_WITHDRAWAL,
    TransactionType.FX_CONVERSION,
}

_PROHIBITED_TARGET_TYPES = {
    TransactionType.FEE,
    TransactionType.TAX_WITHHOLDING,
    TransactionType.REVERSAL,
}


@dataclass(frozen=True)
class FeeTaxAttributionIntent:
    """
    Explicit caller-supplied evidence intending to attribute a portion of a charge event
    to an economic target transaction.
    """
    charge_transaction_id: UUID
    target_transaction_id: UUID
    allocated_amount: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.charge_transaction_id, bool) or not isinstance(self.charge_transaction_id, UUID):
            raise FeeTaxAttributionError(
                f"charge_transaction_id must be a UUID instance, got {type(self.charge_transaction_id).__name__}"
            )
        if isinstance(self.target_transaction_id, bool) or not isinstance(self.target_transaction_id, UUID):
            raise FeeTaxAttributionError(
                f"target_transaction_id must be a UUID instance, got {type(self.target_transaction_id).__name__}"
            )
        if isinstance(self.allocated_amount, bool) or not isinstance(self.allocated_amount, Decimal):
            raise FeeTaxAttributionError(
                f"allocated_amount must be a Decimal instance, got {type(self.allocated_amount).__name__}"
            )
        if not self.allocated_amount.is_finite() or self.allocated_amount <= Decimal("0"):
            raise FeeTaxAttributionError(
                f"allocated_amount must be a finite strictly positive Decimal (> 0), got {self.allocated_amount}"
            )
        if self.charge_transaction_id == self.target_transaction_id:
            raise FeeTaxAttributionError(
                f"Self-attribution rejected: charge_transaction_id {self.charge_transaction_id} "
                f"equals target_transaction_id {self.target_transaction_id}"
            )


@dataclass(frozen=True)
class ResolvedFeeTaxAttribution:
    """
    Immutable resolved attribution linking an authoritative charge transaction to an
    authoritative target economic transaction with an exact allocated amount.
    """
    charge_transaction: PortfolioTransaction
    target_transaction: PortfolioTransaction
    allocated_amount: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.charge_transaction, bool) or not isinstance(self.charge_transaction, PortfolioTransaction):
            raise FeeTaxAttributionError(
                f"charge_transaction must be a PortfolioTransaction instance, got {type(self.charge_transaction).__name__}"
            )
        if isinstance(self.target_transaction, bool) or not isinstance(self.target_transaction, PortfolioTransaction):
            raise FeeTaxAttributionError(
                f"target_transaction must be a PortfolioTransaction instance, got {type(self.target_transaction).__name__}"
            )
        if isinstance(self.allocated_amount, bool) or not isinstance(self.allocated_amount, Decimal):
            raise FeeTaxAttributionError(
                f"allocated_amount must be a Decimal instance, got {type(self.allocated_amount).__name__}"
            )
        if not self.allocated_amount.is_finite() or self.allocated_amount <= Decimal("0"):
            raise FeeTaxAttributionError(
                f"allocated_amount must be a finite strictly positive Decimal (> 0), got {self.allocated_amount}"
            )

        # Charge side validation
        if self.charge_transaction.transaction_type not in (TransactionType.FEE, TransactionType.TAX_WITHHOLDING):
            raise FeeTaxAttributionError(
                f"charge_transaction must be FEE or TAX_WITHHOLDING, got {self.charge_transaction.transaction_type.name}"
            )
        if self.charge_transaction.cash_amount is None or self.charge_transaction.cash_currency is None:
            raise FeeTaxAttributionError(
                f"charge_transaction {self.charge_transaction.id} must have non-null cash_amount and cash_currency"
            )

        # Target side validation
        if self.target_transaction.transaction_type in _PROHIBITED_TARGET_TYPES or self.target_transaction.transaction_type not in _VALID_TARGET_TYPES:
            raise FeeTaxAttributionError(
                f"target_transaction cannot be of type {self.target_transaction.transaction_type.name}"
            )

        # Self-link rejection
        if self.charge_transaction.id == self.target_transaction.id:
            raise FeeTaxAttributionError(
                f"Self-attribution rejected on transaction {self.charge_transaction.id}"
            )

        # Same portfolio validation
        if self.charge_transaction.portfolio_id != self.target_transaction.portfolio_id:
            raise FeeTaxAttributionError(
                f"Cross-portfolio attribution rejected: charge portfolio {self.charge_transaction.portfolio_id} "
                f"!= target portfolio {self.target_transaction.portfolio_id}"
            )

        # Same account validation
        if self.charge_transaction.account_id != self.target_transaction.account_id:
            raise FeeTaxAttributionError(
                f"Cross-account attribution rejected: charge account {self.charge_transaction.account_id} "
                f"!= target account {self.target_transaction.account_id}"
            )

        # Single allocation limit
        if self.allocated_amount > self.charge_transaction.cash_amount:
            raise FeeTaxAttributionError(
                f"allocated_amount {self.allocated_amount} exceeds charge cash_amount {self.charge_transaction.cash_amount}"
            )


@dataclass(frozen=True)
class ObservedFeeTaxAttributionSet:
    """
    Immutable, point-in-time validated set of explicit charge-to-transaction attributions.
    """
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    observed_projection: ObservedFeeTaxProjection
    intents: Tuple[FeeTaxAttributionIntent, ...]
    attributions: Tuple[ResolvedFeeTaxAttribution, ...]

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise FeeTaxAttributionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.mode, bool) or not isinstance(self.mode, PortfolioMode):
            raise FeeTaxAttributionError(
                f"mode must be a PortfolioMode instance, got {type(self.mode).__name__}"
            )
        if self.as_of_recorded_at is not None:
            if isinstance(self.as_of_recorded_at, bool) or not isinstance(self.as_of_recorded_at, datetime):
                raise FeeTaxAttributionError(
                    f"as_of_recorded_at must be None or datetime, got {type(self.as_of_recorded_at).__name__}"
                )
            if not _is_aware_datetime(self.as_of_recorded_at):
                raise FeeTaxAttributionError(
                    f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got {self.as_of_recorded_at}"
                )

        if isinstance(self.observed_projection, bool) or not isinstance(self.observed_projection, ObservedFeeTaxProjection):
            raise FeeTaxAttributionError(
                f"observed_projection must be an ObservedFeeTaxProjection instance, got {type(self.observed_projection).__name__}"
            )

        if not isinstance(self.intents, tuple):
            raise FeeTaxAttributionError(
                f"intents must be a tuple, got {type(self.intents).__name__}"
            )
        if not isinstance(self.attributions, tuple):
            raise FeeTaxAttributionError(
                f"attributions must be a tuple, got {type(self.attributions).__name__}"
            )

        # Metadata matching
        if self.portfolio_id != self.observed_projection.portfolio_id:
            raise FeeTaxAttributionError(
                f"portfolio_id {self.portfolio_id} does not match observed_projection.portfolio_id {self.observed_projection.portfolio_id}"
            )
        if self.mode != self.observed_projection.mode:
            raise FeeTaxAttributionError(
                f"mode {self.mode} does not match observed_projection.mode {self.observed_projection.mode}"
            )
        if not _is_exact_datetime_representation_equal(
            self.as_of_recorded_at,
            self.observed_projection.as_of_recorded_at,
        ):
            raise FeeTaxAttributionError(
                f"as_of_recorded_at {self.as_of_recorded_at} does not match observed_projection.as_of_recorded_at {self.observed_projection.as_of_recorded_at}"
            )

        # Canonical lookup structures
        charges_by_id: Dict[UUID, PortfolioTransaction] = {tx.id: tx for tx in self.observed_projection.events}
        active_by_id: Dict[UUID, PortfolioTransaction] = {
            tx.id: tx for tx in self.observed_projection.ledger_view.active_transactions
        }

        # Intent validation and canonical attribution derivation
        seen_pairs: Set[Tuple[UUID, UUID]] = set()
        allocations_by_charge: Dict[UUID, List[Decimal]] = {}
        canonical_attributions: List[ResolvedFeeTaxAttribution] = []

        for idx, intent in enumerate(self.intents):
            if not isinstance(intent, FeeTaxAttributionIntent):
                raise FeeTaxAttributionError(
                    f"Intent at index {idx} must be a FeeTaxAttributionIntent, got {type(intent).__name__}"
                )

            pair = (intent.charge_transaction_id, intent.target_transaction_id)
            if pair in seen_pairs:
                raise FeeTaxAttributionError(
                    f"Duplicate attribution intent detected for pair (charge {intent.charge_transaction_id}, target {intent.target_transaction_id})"
                )
            seen_pairs.add(pair)

            if intent.charge_transaction_id not in charges_by_id:
                raise FeeTaxAttributionError(
                    f"Charge transaction {intent.charge_transaction_id} not found in observed active charge events"
                )
            if intent.target_transaction_id not in active_by_id:
                raise FeeTaxAttributionError(
                    f"Target transaction {intent.target_transaction_id} not found in active transactions at PIT cutoff"
                )

            charge_tx = charges_by_id[intent.charge_transaction_id]
            target_tx = active_by_id[intent.target_transaction_id]

            allocations_by_charge.setdefault(intent.charge_transaction_id, []).append(intent.allocated_amount)

            resolved = ResolvedFeeTaxAttribution(
                charge_transaction=charge_tx,
                target_transaction=target_tx,
                allocated_amount=intent.allocated_amount,
            )
            canonical_attributions.append(resolved)

        # Check total allocation per charge against charge cash_amount
        for charge_id, amounts in allocations_by_charge.items():
            charge_tx = charges_by_id[charge_id]
            assert charge_tx.cash_amount is not None
            total_allocated = _exact_decimal_sum(amounts)
            if total_allocated > charge_tx.cash_amount:
                raise FeeTaxAttributionError(
                    f"Over-allocation detected for charge {charge_id}: "
                    f"total allocated {total_allocated} exceeds charge amount {charge_tx.cash_amount}"
                )

        # Direct-constructor tamper revalidation against canonical resolved attributions
        if len(self.attributions) != len(canonical_attributions):
            raise FeeTaxAttributionError(
                f"attributions count {len(self.attributions)} does not match canonical attribution count {len(canonical_attributions)}"
            )

        for idx, (actual, expected) in enumerate(zip(self.attributions, canonical_attributions)):
            if not isinstance(actual, ResolvedFeeTaxAttribution):
                raise FeeTaxAttributionError(
                    f"Attribution at index {idx} must be a ResolvedFeeTaxAttribution, got {type(actual).__name__}"
                )
            if actual.charge_transaction is not expected.charge_transaction:
                raise FeeTaxAttributionError(
                    f"Attribution at index {idx} charge_transaction object does not match authoritative canonical charge"
                )
            if actual.target_transaction is not expected.target_transaction:
                raise FeeTaxAttributionError(
                    f"Attribution at index {idx} target_transaction object does not match authoritative canonical target"
                )
            if actual.allocated_amount.as_tuple() != expected.allocated_amount.as_tuple():
                raise FeeTaxAttributionError(
                    f"Attribution at index {idx} allocated_amount representation {actual.allocated_amount!r} "
                    f"does not match expected {expected.allocated_amount!r}"
                )

    @property
    def attribution_count(self) -> int:
        """Total number of resolved attributions."""
        return len(self.attributions)

    @property
    def charge_ids(self) -> Tuple[UUID, ...]:
        """Unique charge transaction IDs in first-seen attribution order."""
        return tuple(dict.fromkeys(a.charge_transaction.id for a in self.attributions))

    @property
    def target_ids(self) -> Tuple[UUID, ...]:
        """Unique target transaction IDs in first-seen attribution order."""
        return tuple(dict.fromkeys(a.target_transaction.id for a in self.attributions))

    def attributions_for_charge(
        self,
        charge_transaction_id: UUID,
    ) -> Tuple[ResolvedFeeTaxAttribution, ...]:
        """
        Returns all resolved attributions for the given charge transaction ID in canonical order.
        If no attributions exist for this charge, returns an empty tuple.
        """
        if isinstance(charge_transaction_id, bool) or not isinstance(charge_transaction_id, UUID):
            raise FeeTaxAttributionError(
                f"charge_transaction_id must be a UUID instance, got {type(charge_transaction_id).__name__}"
            )
        return tuple(a for a in self.attributions if a.charge_transaction.id == charge_transaction_id)

    def attributions_for_target(
        self,
        target_transaction_id: UUID,
    ) -> Tuple[ResolvedFeeTaxAttribution, ...]:
        """
        Returns all resolved attributions for the given target transaction ID in canonical order.
        If no attributions exist for this target, returns an empty tuple.
        """
        if isinstance(target_transaction_id, bool) or not isinstance(target_transaction_id, UUID):
            raise FeeTaxAttributionError(
                f"target_transaction_id must be a UUID instance, got {type(target_transaction_id).__name__}"
            )
        return tuple(a for a in self.attributions if a.target_transaction.id == target_transaction_id)

    def unallocated_amount_for_charge(
        self,
        charge_transaction_id: UUID,
    ) -> Decimal:
        """
        Returns the exact context-independent unallocated remaining amount for an observed charge.
        Raises FeeTaxAttributionError if the charge is not found in observed projection events.
        """
        if isinstance(charge_transaction_id, bool) or not isinstance(charge_transaction_id, UUID):
            raise FeeTaxAttributionError(
                f"charge_transaction_id must be a UUID instance, got {type(charge_transaction_id).__name__}"
            )

        charge_tx: Optional[PortfolioTransaction] = None
        for tx in self.observed_projection.events:
            if tx.id == charge_transaction_id:
                charge_tx = tx
                break

        if charge_tx is None:
            raise FeeTaxAttributionError(
                f"Charge transaction {charge_transaction_id} not found in observed active charge events"
            )

        assert charge_tx.cash_amount is not None
        matching = [a for a in self.attributions if a.charge_transaction.id == charge_transaction_id]
        if not matching:
            return charge_tx.cash_amount

        allocated_sum = _exact_decimal_sum(a.allocated_amount for a in matching)
        return _exact_decimal_sub(charge_tx.cash_amount, allocated_sum)

    def is_fully_allocated(
        self,
        charge_transaction_id: UUID,
    ) -> bool:
        """
        Returns True if the observed charge is exactly 100% allocated by value.
        Raises FeeTaxAttributionError if the charge is not found in observed projection events.
        """
        unallocated = self.unallocated_amount_for_charge(charge_transaction_id)
        return unallocated == Decimal("0")


def build_observed_fee_tax_attribution_set(
    observed: ObservedFeeTaxProjection,
    intents: Tuple[FeeTaxAttributionIntent, ...],
) -> ObservedFeeTaxAttributionSet:
    """
    Validates explicit caller-supplied attribution evidence against an authoritative
    ObservedFeeTaxProjection snapshot and builds an immutable ObservedFeeTaxAttributionSet.

    Args:
        observed: Authoritative ObservedFeeTaxProjection from Phase 14A.
        intents: Explicit tuple of FeeTaxAttributionIntent instances.

    Returns:
        ObservedFeeTaxAttributionSet containing validated resolved attributions.

    Raises:
        TypeError: If arguments are of invalid types.
        FeeTaxAttributionError: If attribution validation fails structural invariants.
    """
    if isinstance(observed, bool) or not isinstance(observed, ObservedFeeTaxProjection):
        raise TypeError(
            f"observed must be an instance of ObservedFeeTaxProjection, got {type(observed).__name__}"
        )
    if not isinstance(intents, tuple):
        raise TypeError(
            f"intents must be a tuple, got {type(intents).__name__}"
        )

    charges_by_id: Dict[UUID, PortfolioTransaction] = {tx.id: tx for tx in observed.events}
    active_by_id: Dict[UUID, PortfolioTransaction] = {
        tx.id: tx for tx in observed.ledger_view.active_transactions
    }

    seen_pairs: Set[Tuple[UUID, UUID]] = set()
    allocations_by_charge: Dict[UUID, List[Decimal]] = {}
    resolved_list: List[ResolvedFeeTaxAttribution] = []

    for idx, intent in enumerate(intents):
        if isinstance(intent, bool) or not isinstance(intent, FeeTaxAttributionIntent):
            raise FeeTaxAttributionError(
                f"Intent at index {idx} must be a FeeTaxAttributionIntent, got {type(intent).__name__}"
            )

        pair = (intent.charge_transaction_id, intent.target_transaction_id)
        if pair in seen_pairs:
            raise FeeTaxAttributionError(
                f"Duplicate attribution intent detected for pair (charge {intent.charge_transaction_id}, target {intent.target_transaction_id})"
            )
        seen_pairs.add(pair)

        if intent.charge_transaction_id not in charges_by_id:
            raise FeeTaxAttributionError(
                f"Charge transaction {intent.charge_transaction_id} not found in observed active charge events"
            )
        if intent.target_transaction_id not in active_by_id:
            raise FeeTaxAttributionError(
                f"Target transaction {intent.target_transaction_id} not found in active transactions at PIT cutoff"
            )

        charge_tx = charges_by_id[intent.charge_transaction_id]
        target_tx = active_by_id[intent.target_transaction_id]

        allocations_by_charge.setdefault(intent.charge_transaction_id, []).append(intent.allocated_amount)

        resolved = ResolvedFeeTaxAttribution(
            charge_transaction=charge_tx,
            target_transaction=target_tx,
            allocated_amount=intent.allocated_amount,
        )
        resolved_list.append(resolved)

    # Validate sum of allocations against charge cash_amount
    for charge_id, amounts in allocations_by_charge.items():
        charge_tx = charges_by_id[charge_id]
        assert charge_tx.cash_amount is not None
        total_allocated = _exact_decimal_sum(amounts)
        if total_allocated > charge_tx.cash_amount:
            raise FeeTaxAttributionError(
                f"Over-allocation detected for charge {charge_id}: "
                f"total allocated {total_allocated} exceeds charge amount {charge_tx.cash_amount}"
            )

    return ObservedFeeTaxAttributionSet(
        portfolio_id=observed.portfolio_id,
        mode=observed.mode,
        as_of_recorded_at=observed.as_of_recorded_at,
        observed_projection=observed,
        intents=intents,
        attributions=tuple(resolved_list),
    )
