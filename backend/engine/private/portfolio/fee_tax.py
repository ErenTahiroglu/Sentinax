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
from typing import Dict, Iterable, List, Optional, Tuple
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
        if not _is_exact_datetime_representation_equal(
            self.as_of_recorded_at,
            self.ledger_view.as_of_recorded_at,
        ):
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
            raise FeeTaxProjectionError(f"Expected Decimal, got {type(dec).__name__}")
        if not dec.is_finite():
            raise FeeTaxProjectionError("Non-finite Decimal rejected")

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


@dataclass(frozen=True)
class ObservedFeeTaxAggregateState:
    """
    Exact observed fee and tax withholding monetary aggregates for a specific account and currency.
    """
    portfolio_id: UUID
    account_id: UUID
    currency: Currency

    fee_amount: Decimal
    tax_withholding_amount: Decimal

    fee_event_count: int
    tax_withholding_event_count: int

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise FeeTaxProjectionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.account_id, bool) or not isinstance(self.account_id, UUID):
            raise FeeTaxProjectionError(
                f"account_id must be a UUID instance, got {type(self.account_id).__name__}"
            )
        if isinstance(self.currency, bool) or not isinstance(self.currency, Currency):
            raise FeeTaxProjectionError(
                f"currency must be a Currency instance, got {type(self.currency).__name__}"
            )
        if isinstance(self.fee_amount, bool) or not isinstance(self.fee_amount, Decimal):
            raise FeeTaxProjectionError(
                f"fee_amount must be a Decimal instance, got {type(self.fee_amount).__name__}"
            )
        if not self.fee_amount.is_finite() or self.fee_amount < Decimal("0"):
            raise FeeTaxProjectionError(
                f"fee_amount must be a finite non-negative Decimal, got {self.fee_amount}"
            )
        if isinstance(self.tax_withholding_amount, bool) or not isinstance(self.tax_withholding_amount, Decimal):
            raise FeeTaxProjectionError(
                f"tax_withholding_amount must be a Decimal instance, got {type(self.tax_withholding_amount).__name__}"
            )
        if not self.tax_withholding_amount.is_finite() or self.tax_withholding_amount < Decimal("0"):
            raise FeeTaxProjectionError(
                f"tax_withholding_amount must be a finite non-negative Decimal, got {self.tax_withholding_amount}"
            )
        if isinstance(self.fee_event_count, bool) or not isinstance(self.fee_event_count, int) or self.fee_event_count < 0:
            raise FeeTaxProjectionError(
                f"fee_event_count must be a non-negative int, got {self.fee_event_count}"
            )
        if isinstance(self.tax_withholding_event_count, bool) or not isinstance(self.tax_withholding_event_count, int) or self.tax_withholding_event_count < 0:
            raise FeeTaxProjectionError(
                f"tax_withholding_event_count must be a non-negative int, got {self.tax_withholding_event_count}"
            )

        if self.fee_event_count + self.tax_withholding_event_count < 1:
            raise FeeTaxProjectionError(
                "ObservedFeeTaxAggregateState requires at least one fee or tax withholding event (sum of counts >= 1)"
            )

        if self.fee_event_count == 0 and self.fee_amount != Decimal("0"):
            raise FeeTaxProjectionError(
                f"fee_event_count is 0 but fee_amount is non-zero: {self.fee_amount}"
            )
        if self.fee_event_count > 0 and self.fee_amount <= Decimal("0"):
            raise FeeTaxProjectionError(
                f"fee_event_count is {self.fee_event_count} but fee_amount is not strictly positive: {self.fee_amount}"
            )

        if self.tax_withholding_event_count == 0 and self.tax_withholding_amount != Decimal("0"):
            raise FeeTaxProjectionError(
                f"tax_withholding_event_count is 0 but tax_withholding_amount is non-zero: {self.tax_withholding_amount}"
            )
        if self.tax_withholding_event_count > 0 and self.tax_withholding_amount <= Decimal("0"):
            raise FeeTaxProjectionError(
                f"tax_withholding_event_count is {self.tax_withholding_event_count} but tax_withholding_amount is not strictly positive: {self.tax_withholding_amount}"
            )

    @property
    def total_observed_charge(self) -> Decimal:
        """Exact context-independent sum of observed fees and tax withholdings for this account/currency."""
        return _exact_decimal_sum((self.fee_amount, self.tax_withholding_amount))


@dataclass(frozen=True)
class ObservedFeeTaxAggregation:
    """
    Immutable projection of per-account / per-currency observed fee and tax withholding aggregates.
    """
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    observed_projection: ObservedFeeTaxProjection
    states: Tuple[ObservedFeeTaxAggregateState, ...]

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

        if isinstance(self.observed_projection, bool) or not isinstance(self.observed_projection, ObservedFeeTaxProjection):
            raise FeeTaxProjectionError(
                f"observed_projection must be an ObservedFeeTaxProjection instance, got {type(self.observed_projection).__name__}"
            )

        if not isinstance(self.states, tuple):
            raise FeeTaxProjectionError(
                f"states must be a tuple, got {type(self.states).__name__}"
            )

        # Metadata matching
        if self.portfolio_id != self.observed_projection.portfolio_id:
            raise FeeTaxProjectionError(
                f"portfolio_id {self.portfolio_id} does not match observed_projection.portfolio_id {self.observed_projection.portfolio_id}"
            )
        if self.mode != self.observed_projection.mode:
            raise FeeTaxProjectionError(
                f"mode {self.mode} does not match observed_projection.mode {self.observed_projection.mode}"
            )
        if not _is_exact_datetime_representation_equal(
            self.as_of_recorded_at,
            self.observed_projection.as_of_recorded_at,
        ):
            raise FeeTaxProjectionError(
                f"as_of_recorded_at {self.as_of_recorded_at} does not match observed_projection.as_of_recorded_at {self.observed_projection.as_of_recorded_at}"
            )

        # Canonical states derivation from observed_projection.events
        grouped_events: Dict[Tuple[UUID, Currency], List[PortfolioTransaction]] = {}
        for tx in self.observed_projection.events:
            assert tx.cash_currency is not None
            key = (tx.account_id, tx.cash_currency)
            grouped_events.setdefault(key, []).append(tx)

        canonical_states: List[ObservedFeeTaxAggregateState] = []
        for (account_id, currency), txs in grouped_events.items():
            fee_txs = [t for t in txs if t.transaction_type == TransactionType.FEE]
            tax_txs = [t for t in txs if t.transaction_type == TransactionType.TAX_WITHHOLDING]
            fee_count = len(fee_txs)
            tax_count = len(tax_txs)
            fee_amount = _exact_decimal_sum(t.cash_amount for t in fee_txs if t.cash_amount is not None)
            tax_amount = _exact_decimal_sum(t.cash_amount for t in tax_txs if t.cash_amount is not None)
            canonical_states.append(
                ObservedFeeTaxAggregateState(
                    portfolio_id=self.portfolio_id,
                    account_id=account_id,
                    currency=currency,
                    fee_amount=fee_amount,
                    tax_withholding_amount=tax_amount,
                    fee_event_count=fee_count,
                    tax_withholding_event_count=tax_count,
                )
            )

        if len(self.states) != len(canonical_states):
            raise FeeTaxProjectionError(
                f"states count {len(self.states)} does not match canonical aggregate state count {len(canonical_states)}"
            )

        for idx, (actual, expected) in enumerate(zip(self.states, canonical_states)):
            if not isinstance(actual, ObservedFeeTaxAggregateState):
                raise FeeTaxProjectionError(
                    f"State at index {idx} must be an ObservedFeeTaxAggregateState, got {type(actual).__name__}"
                )
            if actual.portfolio_id != expected.portfolio_id:
                raise FeeTaxProjectionError(
                    f"State at index {idx} portfolio_id {actual.portfolio_id} does not match expected {expected.portfolio_id}"
                )
            if actual.account_id != expected.account_id:
                raise FeeTaxProjectionError(
                    f"State at index {idx} account_id {actual.account_id} does not match expected {expected.account_id}"
                )
            if actual.currency != expected.currency:
                raise FeeTaxProjectionError(
                    f"State at index {idx} currency {actual.currency} does not match expected {expected.currency}"
                )
            if actual.fee_event_count != expected.fee_event_count:
                raise FeeTaxProjectionError(
                    f"State at index {idx} fee_event_count {actual.fee_event_count} does not match expected {expected.fee_event_count}"
                )
            if actual.tax_withholding_event_count != expected.tax_withholding_event_count:
                raise FeeTaxProjectionError(
                    f"State at index {idx} tax_withholding_event_count {actual.tax_withholding_event_count} does not match expected {expected.tax_withholding_event_count}"
                )
            if actual.fee_amount.as_tuple() != expected.fee_amount.as_tuple():
                raise FeeTaxProjectionError(
                    f"State at index {idx} fee_amount representation {actual.fee_amount!r} does not match expected {expected.fee_amount!r}"
                )
            if actual.tax_withholding_amount.as_tuple() != expected.tax_withholding_amount.as_tuple():
                raise FeeTaxProjectionError(
                    f"State at index {idx} tax_withholding_amount representation {actual.tax_withholding_amount!r} does not match expected {expected.tax_withholding_amount!r}"
                )

    @property
    def state_count(self) -> int:
        """Total number of aggregate states."""
        return len(self.states)

    @property
    def account_ids(self) -> Tuple[UUID, ...]:
        """Unique account IDs in first-seen state order."""
        return tuple(dict.fromkeys(s.account_id for s in self.states))

    @property
    def fee_bearing_states(self) -> Tuple[ObservedFeeTaxAggregateState, ...]:
        """States with at least one fee event (fee_event_count > 0)."""
        return tuple(s for s in self.states if s.fee_event_count > 0)

    @property
    def tax_withholding_bearing_states(self) -> Tuple[ObservedFeeTaxAggregateState, ...]:
        """States with at least one tax withholding event (tax_withholding_event_count > 0)."""
        return tuple(s for s in self.states if s.tax_withholding_event_count > 0)


def build_observed_fee_tax_aggregation(
    observed: ObservedFeeTaxProjection,
) -> ObservedFeeTaxAggregation:
    """
    Derives exact per-account / per-currency observed fee and tax withholding aggregates.

    Args:
        observed: Authoritative ObservedFeeTaxProjection from Phase 14A.

    Returns:
        ObservedFeeTaxAggregation containing grouped states and delegated views.

    Raises:
        TypeError: If observed is not an instance of ObservedFeeTaxProjection.
        FeeTaxProjectionError: If aggregate construction fails structural invariants.
    """
    if isinstance(observed, bool) or not isinstance(observed, ObservedFeeTaxProjection):
        raise TypeError(
            f"observed must be an instance of ObservedFeeTaxProjection, got {type(observed).__name__}"
        )

    grouped_events: Dict[Tuple[UUID, Currency], List[PortfolioTransaction]] = {}
    for tx in observed.events:
        assert tx.cash_currency is not None
        key = (tx.account_id, tx.cash_currency)
        grouped_events.setdefault(key, []).append(tx)

    states: List[ObservedFeeTaxAggregateState] = []
    for (account_id, currency), txs in grouped_events.items():
        fee_txs = [t for t in txs if t.transaction_type == TransactionType.FEE]
        tax_txs = [t for t in txs if t.transaction_type == TransactionType.TAX_WITHHOLDING]
        fee_count = len(fee_txs)
        tax_count = len(tax_txs)
        fee_amount = _exact_decimal_sum(t.cash_amount for t in fee_txs if t.cash_amount is not None)
        tax_amount = _exact_decimal_sum(t.cash_amount for t in tax_txs if t.cash_amount is not None)
        states.append(
            ObservedFeeTaxAggregateState(
                portfolio_id=observed.portfolio_id,
                account_id=account_id,
                currency=currency,
                fee_amount=fee_amount,
                tax_withholding_amount=tax_amount,
                fee_event_count=fee_count,
                tax_withholding_event_count=tax_count,
            )
        )

    return ObservedFeeTaxAggregation(
        portfolio_id=observed.portfolio_id,
        mode=observed.mode,
        as_of_recorded_at=observed.as_of_recorded_at,
        observed_projection=observed,
        states=tuple(states),
    )
