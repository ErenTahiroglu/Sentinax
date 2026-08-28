"""
backend/engine/private/portfolio/positions.py
=============================================
Exact Reversal-Aware Position Quantity Projection (Phase 12C.2 & 12C.2.1).

This module provides a pure in-memory projection that consumes a closed
`LedgerProjectionView` and computes exact instrument unit quantities per account.

Invariants:
- Pure Python domain logic: no network, no Supabase, no SQL, no mutable state.
- Scoped strictly by (portfolio_id, account_id, instrument_id). No cross-account aggregation.
- Strict runtime type validation on view metadata and all active transactions.
- Exhaustive transaction type matching: BUY/SELL alter quantity; approved cash/corporate
  events are quantity-neutral; REVERSAL and any unhandled/string types fail closed.
- Fully closed positions (quantity == Decimal("0")) are retained in `positions` for audit,
  while `open_positions` includes strictly positive holdings.
- Final net negative quantity fails closed with `PositionProjectionError`.
- Context-independent exact Decimal summation using arbitrary-precision integer alignment
  (immune to ambient Decimal context precision).
- Strict constructor invariants on PositionQuantityState and PositionQuantityProjection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.domain import PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import PortfolioTransaction
from backend.engine.private.portfolio.projection import LedgerProjectionView


class PositionProjectionError(ValueError):
    """Raised when position projection encounters invalid view state, unsupported short, or corrupt history."""
    pass


_ALLOWED_POSITION_CHANGING_TYPES = frozenset({
    TransactionType.BUY,
    TransactionType.SELL,
})

_ALLOWED_QUANTITY_NEUTRAL_TYPES = frozenset({
    TransactionType.CASH_DEPOSIT,
    TransactionType.CASH_WITHDRAWAL,
    TransactionType.DIVIDEND,
    TransactionType.INTEREST,
    TransactionType.FX_CONVERSION,
    TransactionType.FEE,
    TransactionType.TAX_WITHHOLDING,
})


@dataclass(frozen=True)
class PositionQuantityState:
    """Exact unit quantity held for an instrument within a specific account."""
    portfolio_id: UUID
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise PositionProjectionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.account_id, bool) or not isinstance(self.account_id, UUID):
            raise PositionProjectionError(
                f"account_id must be a UUID instance, got {type(self.account_id).__name__}"
            )
        if isinstance(self.instrument_id, bool) or not isinstance(self.instrument_id, UUID):
            raise PositionProjectionError(
                f"instrument_id must be a UUID instance, got {type(self.instrument_id).__name__}"
            )
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, Decimal):
            raise PositionProjectionError(
                f"quantity must be a Decimal instance, got {type(self.quantity).__name__}"
            )
        if not self.quantity.is_finite():
            raise PositionProjectionError(
                f"quantity must be finite, got {self.quantity}"
            )
        if self.quantity < Decimal("0"):
            raise PositionProjectionError(
                f"quantity cannot be negative, got {self.quantity}"
            )

    @property
    def is_open(self) -> bool:
        """True if the position has positive remaining quantity."""
        return self.quantity > Decimal("0")


@dataclass(frozen=True)
class PositionQuantityProjection:
    """Immutable projection of security positions derived from an active ledger view."""
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    positions: Tuple[PositionQuantityState, ...]
    open_positions: Tuple[PositionQuantityState, ...]

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise PositionProjectionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.mode, bool) or not isinstance(self.mode, PortfolioMode):
            raise PositionProjectionError(
                f"mode must be a PortfolioMode instance, got {type(self.mode).__name__}"
            )
        if self.as_of_recorded_at is not None:
            if isinstance(self.as_of_recorded_at, bool) or not isinstance(self.as_of_recorded_at, datetime):
                raise PositionProjectionError(
                    f"as_of_recorded_at must be None or datetime, got {type(self.as_of_recorded_at).__name__}"
                )
            if self.as_of_recorded_at.tzinfo is None:
                raise PositionProjectionError(
                    f"as_of_recorded_at must be timezone-aware, got naive: {self.as_of_recorded_at}"
                )

        if not isinstance(self.positions, tuple):
            raise PositionProjectionError(
                f"positions must be a tuple, got {type(self.positions).__name__}"
            )
        if not isinstance(self.open_positions, tuple):
            raise PositionProjectionError(
                f"open_positions must be a tuple, got {type(self.open_positions).__name__}"
            )

        seen_positions: Dict[Tuple[UUID, UUID], PositionQuantityState] = {}
        for pos in self.positions:
            if not isinstance(pos, PositionQuantityState):
                raise PositionProjectionError(
                    f"Element in positions must be PositionQuantityState, got {type(pos).__name__}"
                )
            if pos.portfolio_id != self.portfolio_id:
                raise PositionProjectionError(
                    f"Position portfolio_id {pos.portfolio_id} does not match projection {self.portfolio_id}"
                )
            key = (pos.account_id, pos.instrument_id)
            if key in seen_positions:
                raise PositionProjectionError(
                    f"Duplicate position identity {key} detected in positions"
                )
            seen_positions[key] = pos

        seen_open: set[Tuple[UUID, UUID]] = set()
        for pos in self.open_positions:
            if not isinstance(pos, PositionQuantityState):
                raise PositionProjectionError(
                    f"Element in open_positions must be PositionQuantityState, got {type(pos).__name__}"
                )
            if pos.portfolio_id != self.portfolio_id:
                raise PositionProjectionError(
                    f"Open position portfolio_id {pos.portfolio_id} does not match projection {self.portfolio_id}"
                )
            if not pos.is_open:
                raise PositionProjectionError(
                    f"Zero-quantity position {pos.instrument_id} in account {pos.account_id} must not appear in open_positions"
                )
            key = (pos.account_id, pos.instrument_id)
            if key in seen_open:
                raise PositionProjectionError(
                    f"Duplicate open position identity {key} detected in open_positions"
                )
            seen_open.add(key)
            if key not in seen_positions:
                raise PositionProjectionError(
                    f"Open position {key} not found in positions"
                )
            if seen_positions[key] != pos:
                raise PositionProjectionError(
                    f"Open position {key} does not match corresponding record in positions"
                )

        for key, pos in seen_positions.items():
            if pos.is_open and key not in seen_open:
                raise PositionProjectionError(
                    f"Positive position {key} is missing from open_positions"
                )


def _exact_decimal_sum(deltas: Iterable[Tuple[Decimal, int]]) -> Decimal:
    """
    Computes an exact arbitrary-precision Decimal sum independent of ambient Decimal context.

    Args:
        deltas: Iterable of (decimal_value, sign_multiplier), where sign_multiplier is +1 (BUY) or -1 (SELL).

    Returns:
        Exact Decimal sum without context precision truncation.
    """
    items = list(deltas)
    if not items:
        return Decimal("0")

    parsed_items: List[Tuple[int, int]] = []
    min_exp: Optional[int] = None

    for dec, sign_mult in items:
        if not isinstance(dec, Decimal) or isinstance(dec, bool):
            raise PositionProjectionError(f"Expected Decimal quantity, got {type(dec).__name__}")
        if not dec.is_finite():
            raise PositionProjectionError("Non-finite Decimal quantity rejected")

        sign, digits, exp = dec.as_tuple()
        if not digits:
            int_coeff = 0
        else:
            int_coeff = 0
            for d in digits:
                int_coeff = int_coeff * 10 + d

        if sign == 1:
            int_coeff = -int_coeff

        eff_int = int_coeff * sign_mult
        parsed_items.append((eff_int, exp))

        if min_exp is None or exp < min_exp:
            min_exp = exp

    assert min_exp is not None

    total_int = 0
    for eff_int, exp in parsed_items:
        shift = exp - min_exp
        total_int += eff_int * (10 ** shift)

    if total_int == 0:
        return Decimal("0")

    res_sign = 0 if total_int >= 0 else 1
    res_abs = abs(total_int)
    res_digits = tuple(int(c) for c in str(res_abs))
    return Decimal((res_sign, res_digits, min_exp))


def build_position_quantity_projection(
    view: LedgerProjectionView,
) -> PositionQuantityProjection:
    """
    Derives exact security position quantities from a LedgerProjectionView.

    Args:
        view: Authoritative LedgerProjectionView from Phase 12C.1.

    Returns:
        PositionQuantityProjection containing all touched positions and open positions.

    Raises:
        TypeError: If view is not an instance of LedgerProjectionView or tx is not PortfolioTransaction.
        PositionProjectionError: If view or active transactions contain invalid metadata/types,
                                 forbidden REVERSAL events, cross-portfolio references,
                                 duplicate physical IDs, unapproved transaction types,
                                 or result in negative net quantity.
    """
    if not isinstance(view, LedgerProjectionView):
        raise TypeError(f"view must be an instance of LedgerProjectionView, got {type(view).__name__}")

    # View metadata runtime validation
    if isinstance(view.portfolio_id, bool) or not isinstance(view.portfolio_id, UUID):
        raise PositionProjectionError(
            f"view.portfolio_id must be a UUID instance, got {type(view.portfolio_id).__name__}"
        )

    if isinstance(view.mode, bool) or not isinstance(view.mode, PortfolioMode):
        raise PositionProjectionError(
            f"view.mode must be a PortfolioMode instance, got {type(view.mode).__name__}"
        )

    if view.as_of_recorded_at is not None:
        if isinstance(view.as_of_recorded_at, bool) or not isinstance(view.as_of_recorded_at, datetime):
            raise PositionProjectionError(
                f"view.as_of_recorded_at must be None or datetime, got {type(view.as_of_recorded_at).__name__}"
            )
        if view.as_of_recorded_at.tzinfo is None:
            raise PositionProjectionError(
                f"view.as_of_recorded_at must be timezone-aware, got naive: {view.as_of_recorded_at}"
            )

    # Boundary validation of supplied active transactions
    seen_ids: set[UUID] = set()
    deltas_by_key: Dict[Tuple[UUID, UUID], List[Tuple[Decimal, int]]] = {}

    for tx in view.active_transactions:
        if not isinstance(tx, PortfolioTransaction):
            raise TypeError(f"Expected PortfolioTransaction in active_transactions, got {type(tx).__name__}")

        if isinstance(tx.id, bool) or not isinstance(tx.id, UUID):
            raise PositionProjectionError(
                f"Active transaction id must be a UUID instance, got {type(tx.id).__name__}"
            )

        if isinstance(tx.portfolio_id, bool) or not isinstance(tx.portfolio_id, UUID):
            raise PositionProjectionError(
                f"Active transaction portfolio_id must be a UUID instance, got {type(tx.portfolio_id).__name__}"
            )

        if tx.portfolio_id != view.portfolio_id:
            raise PositionProjectionError(
                f"Active transaction {tx.id} portfolio_id {tx.portfolio_id} does not match view {view.portfolio_id}"
            )

        if isinstance(tx.account_id, bool) or not isinstance(tx.account_id, UUID):
            raise PositionProjectionError(
                f"Active transaction account_id must be a UUID instance, got {type(tx.account_id).__name__}"
            )

        if isinstance(tx.transaction_type, bool) or not isinstance(tx.transaction_type, TransactionType):
            raise PositionProjectionError(
                f"Active transaction transaction_type must be a TransactionType enum instance, got {type(tx.transaction_type).__name__}: {tx.transaction_type!r}"
            )

        if tx.transaction_type == TransactionType.REVERSAL:
            raise PositionProjectionError(
                f"Active transaction {tx.id} is a REVERSAL. REVERSAL events must never appear in active_transactions."
            )

        if tx.id in seen_ids:
            raise PositionProjectionError(
                f"Duplicate physical transaction ID detected in active_transactions: {tx.id}"
            )
        seen_ids.add(tx.id)

        # Exhaustive transaction type check
        if tx.transaction_type in _ALLOWED_POSITION_CHANGING_TYPES:
            if isinstance(tx.instrument_id, bool) or not isinstance(tx.instrument_id, UUID):
                raise PositionProjectionError(
                    f"Transaction {tx.id} of type {tx.transaction_type} missing valid instrument_id UUID"
                )

            if isinstance(tx.quantity, bool) or not isinstance(tx.quantity, Decimal):
                raise PositionProjectionError(
                    f"Transaction {tx.id} quantity must be a Decimal instance, got {type(tx.quantity).__name__}"
                )

            if not tx.quantity.is_finite() or tx.quantity <= Decimal("0"):
                raise PositionProjectionError(
                    f"Transaction {tx.id} quantity must be a strictly positive finite Decimal, got {tx.quantity}"
                )

            key = (tx.account_id, tx.instrument_id)
            sign_mult = 1 if tx.transaction_type == TransactionType.BUY else -1
            deltas_by_key.setdefault(key, []).append((tx.quantity, sign_mult))

        elif tx.transaction_type in _ALLOWED_QUANTITY_NEUTRAL_TYPES:
            # Approved quantity-neutral events
            pass

        else:
            raise PositionProjectionError(
                f"Unhandled transaction type {tx.transaction_type} in position quantity projection"
            )

    # Compute exact net quantities per (account_id, instrument_id)
    all_positions: List[PositionQuantityState] = []
    open_positions: List[PositionQuantityState] = []

    # Sort keys deterministically by (str(account_id), str(instrument_id))
    sorted_keys = sorted(deltas_by_key.keys(), key=lambda k: (str(k[0]), str(k[1])))

    for account_id, instrument_id in sorted_keys:
        deltas = deltas_by_key[(account_id, instrument_id)]
        net_qty = _exact_decimal_sum(deltas)

        if net_qty < Decimal("0"):
            raise PositionProjectionError(
                f"Negative net position quantity {net_qty} for account {account_id} and instrument {instrument_id}. "
                "Short positions or overselling are unsupported."
            )

        pos_state = PositionQuantityState(
            portfolio_id=view.portfolio_id,
            account_id=account_id,
            instrument_id=instrument_id,
            quantity=net_qty,
        )
        all_positions.append(pos_state)
        if pos_state.is_open:
            open_positions.append(pos_state)

    return PositionQuantityProjection(
        portfolio_id=view.portfolio_id,
        mode=view.mode,
        as_of_recorded_at=view.as_of_recorded_at,
        positions=tuple(all_positions),
        open_positions=tuple(open_positions),
    )
