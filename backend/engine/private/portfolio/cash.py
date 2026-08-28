"""
backend/engine/private/portfolio/cash.py
========================================
Exact Reversal-Aware Account Cash Balance Projection (Phase 12C.3).

This module provides a pure in-memory projection that consumes a closed
`LedgerProjectionView` and computes exact account-level, per-currency cash balances.

Invariants:
- Pure Python domain logic: no network, no Supabase, no SQL, no mutable state.
- Scoped strictly by (portfolio_id, account_id, currency). No cross-account aggregation.
- No implicit currency conversion. Multi-currency balances are kept distinct.
- Cash effects derived strictly from active ledger economics:
    * BUY: - (quantity * unit_price) in trade_currency
    * SELL: + (quantity * unit_price) in trade_currency
    * CASH_DEPOSIT: + cash_amount in cash_currency
    * CASH_WITHDRAWAL: - cash_amount in cash_currency
    * DIVIDEND: + cash_amount in cash_currency
    * INTEREST: + cash_amount in cash_currency
    * FEE: - cash_amount in cash_currency
    * TAX_WITHHOLDING: - cash_amount in cash_currency
    * FX_CONVERSION: - from_amount in from_currency, + to_amount in to_currency
- Exhaustive TransactionType matching: REVERSAL and unhandled types fail closed.
- Fully closed cash balances (balance == Decimal("0")) are retained in `balances` for audit,
  while `positive_balances` includes strictly positive holdings.
- Final net negative cash fails closed with `CashProjectionError`.
- Context-independent exact Decimal arithmetic (multiplication and summation) using
  arbitrary-precision integer coefficient alignment (immune to ambient Decimal context).
- Strict constructor invariants on CashBalanceState and CashBalanceProjection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import PortfolioTransaction
from backend.engine.private.portfolio.projection import LedgerProjectionView


class CashProjectionError(ValueError):
    """Raised when cash balance projection encounters invalid view state, unsupported overdraft, or corrupt history."""
    pass


def _is_aware_datetime(dt: Optional[datetime]) -> bool:
    """
    Returns True if dt is a non-bool datetime instance with tzinfo and a non-None utcoffset.
    """
    if dt is None or isinstance(dt, bool) or not isinstance(dt, datetime):
        return False
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


@dataclass(frozen=True)
class CashBalanceState:
    """Exact cash balance held for a specific currency within an account."""
    portfolio_id: UUID
    account_id: UUID
    currency: Currency
    balance: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise CashProjectionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.account_id, bool) or not isinstance(self.account_id, UUID):
            raise CashProjectionError(
                f"account_id must be a UUID instance, got {type(self.account_id).__name__}"
            )
        if isinstance(self.currency, bool) or not isinstance(self.currency, Currency):
            raise CashProjectionError(
                f"currency must be a Currency enum instance, got {type(self.currency).__name__}"
            )
        if isinstance(self.balance, bool) or not isinstance(self.balance, Decimal):
            raise CashProjectionError(
                f"balance must be a Decimal instance, got {type(self.balance).__name__}"
            )
        if not self.balance.is_finite():
            raise CashProjectionError(
                f"balance must be finite, got {self.balance}"
            )
        if self.balance < Decimal("0"):
            raise CashProjectionError(
                f"balance cannot be negative, got {self.balance}"
            )

    @property
    def is_positive(self) -> bool:
        """True if the cash balance is strictly positive."""
        return self.balance > Decimal("0")


@dataclass(frozen=True)
class CashBalanceProjection:
    """Immutable projection of account cash balances derived from an active ledger view."""
    portfolio_id: UUID
    mode: PortfolioMode
    as_of_recorded_at: Optional[datetime]
    balances: Tuple[CashBalanceState, ...]
    positive_balances: Tuple[CashBalanceState, ...]

    def __post_init__(self) -> None:
        if isinstance(self.portfolio_id, bool) or not isinstance(self.portfolio_id, UUID):
            raise CashProjectionError(
                f"portfolio_id must be a UUID instance, got {type(self.portfolio_id).__name__}"
            )
        if isinstance(self.mode, bool) or not isinstance(self.mode, PortfolioMode):
            raise CashProjectionError(
                f"mode must be a PortfolioMode instance, got {type(self.mode).__name__}"
            )
        if self.as_of_recorded_at is not None:
            if isinstance(self.as_of_recorded_at, bool) or not isinstance(self.as_of_recorded_at, datetime):
                raise CashProjectionError(
                    f"as_of_recorded_at must be None or datetime, got {type(self.as_of_recorded_at).__name__}"
                )
            if not _is_aware_datetime(self.as_of_recorded_at):
                raise CashProjectionError(
                    f"as_of_recorded_at must be timezone-aware with non-null utcoffset, got naive or null-offset: {self.as_of_recorded_at}"
                )

        if not isinstance(self.balances, tuple):
            raise CashProjectionError(
                f"balances must be a tuple, got {type(self.balances).__name__}"
            )
        if not isinstance(self.positive_balances, tuple):
            raise CashProjectionError(
                f"positive_balances must be a tuple, got {type(self.positive_balances).__name__}"
            )

        seen_balances: Dict[Tuple[UUID, Currency], CashBalanceState] = {}
        for state in self.balances:
            if not isinstance(state, CashBalanceState):
                raise CashProjectionError(
                    f"Element in balances must be CashBalanceState, got {type(state).__name__}"
                )
            if state.portfolio_id != self.portfolio_id:
                raise CashProjectionError(
                    f"State portfolio_id {state.portfolio_id} does not match projection {self.portfolio_id}"
                )
            key = (state.account_id, state.currency)
            if key in seen_balances:
                raise CashProjectionError(
                    f"Duplicate cash balance identity {key} detected in balances"
                )
            seen_balances[key] = state

        seen_positive: set[Tuple[UUID, Currency]] = set()
        for state in self.positive_balances:
            if not isinstance(state, CashBalanceState):
                raise CashProjectionError(
                    f"Element in positive_balances must be CashBalanceState, got {type(state).__name__}"
                )
            if state.portfolio_id != self.portfolio_id:
                raise CashProjectionError(
                    f"Positive balance portfolio_id {state.portfolio_id} does not match projection {self.portfolio_id}"
                )
            if not state.is_positive:
                raise CashProjectionError(
                    f"Zero-balance state for currency {state.currency} in account {state.account_id} must not appear in positive_balances"
                )
            key = (state.account_id, state.currency)
            if key in seen_positive:
                raise CashProjectionError(
                    f"Duplicate positive balance identity {key} detected in positive_balances"
                )
            seen_positive.add(key)
            if key not in seen_balances:
                raise CashProjectionError(
                    f"Positive balance {key} not found in balances"
                )
            if seen_balances[key] != state:
                raise CashProjectionError(
                    f"Positive balance {key} does not match corresponding record in balances"
                )

        for key, state in seen_balances.items():
            if state.is_positive and key not in seen_positive:
                raise CashProjectionError(
                    f"Positive balance {key} is missing from positive_balances"
                )


def _exact_decimal_mul(a: Decimal, b: Decimal) -> Decimal:
    """
    Computes exact Decimal multiplication independent of ambient Decimal context precision.
    """
    if isinstance(a, bool) or not isinstance(a, Decimal):
        raise CashProjectionError(f"Expected Decimal, got {type(a).__name__}")
    if isinstance(b, bool) or not isinstance(b, Decimal):
        raise CashProjectionError(f"Expected Decimal, got {type(b).__name__}")
    if not a.is_finite() or not b.is_finite():
        raise CashProjectionError("Non-finite Decimal in exact multiplication rejected")

    sign_a, digits_a, exp_a = a.as_tuple()
    sign_b, digits_b, exp_b = b.as_tuple()

    int_a = 0
    for d in digits_a:
        int_a = int_a * 10 + d
    if sign_a == 1:
        int_a = -int_a

    int_b = 0
    for d in digits_b:
        int_b = int_b * 10 + d
    if sign_b == 1:
        int_b = -int_b

    prod_int = int_a * int_b
    if prod_int == 0:
        return Decimal("0")

    prod_exp = exp_a + exp_b
    res_sign = 0 if prod_int >= 0 else 1
    res_abs = abs(prod_int)
    res_digits = tuple(int(c) for c in str(res_abs))
    return Decimal((res_sign, res_digits, prod_exp))


def _exact_decimal_sum(deltas: Iterable[Tuple[Decimal, int]]) -> Decimal:
    """
    Computes an exact arbitrary-precision Decimal sum independent of ambient Decimal context.

    Args:
        deltas: Iterable of (decimal_value, sign_multiplier), where sign_multiplier is +1 (credit) or -1 (debit).

    Returns:
        Exact Decimal sum without context precision truncation.
    """
    items = list(deltas)
    if not items:
        return Decimal("0")

    parsed_items: List[Tuple[int, int]] = []
    min_exp: Optional[int] = None

    for dec, sign_mult in items:
        if isinstance(dec, bool) or not isinstance(dec, Decimal):
            raise CashProjectionError(f"Expected Decimal amount, got {type(dec).__name__}")
        if not dec.is_finite():
            raise CashProjectionError("Non-finite Decimal cash amount rejected")

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


def build_cash_balance_projection(
    view: LedgerProjectionView,
) -> CashBalanceProjection:
    """
    Derives exact account cash balances per currency from a LedgerProjectionView.

    Args:
        view: Authoritative LedgerProjectionView from Phase 12C.1.

    Returns:
        CashBalanceProjection containing all touched cash balances and positive balances.

    Raises:
        TypeError: If view is not an instance of LedgerProjectionView or tx is not PortfolioTransaction.
        CashProjectionError: If view or active transactions contain invalid metadata/types,
                             forbidden REVERSAL events, cross-portfolio references,
                             duplicate physical IDs, unapproved transaction types,
                             or result in negative net cash balance.
    """
    if not isinstance(view, LedgerProjectionView):
        raise TypeError(f"view must be an instance of LedgerProjectionView, got {type(view).__name__}")

    # View metadata runtime validation
    if isinstance(view.portfolio_id, bool) or not isinstance(view.portfolio_id, UUID):
        raise CashProjectionError(
            f"view.portfolio_id must be a UUID instance, got {type(view.portfolio_id).__name__}"
        )

    if isinstance(view.mode, bool) or not isinstance(view.mode, PortfolioMode):
        raise CashProjectionError(
            f"view.mode must be a PortfolioMode instance, got {type(view.mode).__name__}"
        )

    if view.as_of_recorded_at is not None:
        if isinstance(view.as_of_recorded_at, bool) or not isinstance(view.as_of_recorded_at, datetime):
            raise CashProjectionError(
                f"view.as_of_recorded_at must be None or datetime, got {type(view.as_of_recorded_at).__name__}"
            )
        if not _is_aware_datetime(view.as_of_recorded_at):
            raise CashProjectionError(
                f"view.as_of_recorded_at must be timezone-aware with non-null utcoffset, got naive or null-offset: {view.as_of_recorded_at}"
            )

    # Boundary validation of supplied active transactions
    seen_ids: set[UUID] = set()
    deltas_by_key: Dict[Tuple[UUID, Currency], List[Tuple[Decimal, int]]] = {}

    for tx in view.active_transactions:
        if not isinstance(tx, PortfolioTransaction):
            raise TypeError(f"Expected PortfolioTransaction in active_transactions, got {type(tx).__name__}")

        if isinstance(tx.id, bool) or not isinstance(tx.id, UUID):
            raise CashProjectionError(
                f"Active transaction id must be a UUID instance, got {type(tx.id).__name__}"
            )

        if isinstance(tx.portfolio_id, bool) or not isinstance(tx.portfolio_id, UUID):
            raise CashProjectionError(
                f"Active transaction portfolio_id must be a UUID instance, got {type(tx.portfolio_id).__name__}"
            )

        if tx.portfolio_id != view.portfolio_id:
            raise CashProjectionError(
                f"Active transaction {tx.id} portfolio_id {tx.portfolio_id} does not match view {view.portfolio_id}"
            )

        if isinstance(tx.account_id, bool) or not isinstance(tx.account_id, UUID):
            raise CashProjectionError(
                f"Active transaction account_id must be a UUID instance, got {type(tx.account_id).__name__}"
            )

        if isinstance(tx.transaction_type, bool) or not isinstance(tx.transaction_type, TransactionType):
            raise CashProjectionError(
                f"Active transaction transaction_type must be a TransactionType enum instance, got {type(tx.transaction_type).__name__}: {tx.transaction_type!r}"
            )

        if tx.transaction_type == TransactionType.REVERSAL:
            raise CashProjectionError(
                f"Active transaction {tx.id} is a REVERSAL. REVERSAL events must never appear in active_transactions."
            )

        if tx.id in seen_ids:
            raise CashProjectionError(
                f"Duplicate physical transaction ID detected in active_transactions: {tx.id}"
            )
        seen_ids.add(tx.id)

        # Exhaustive transaction type matching
        tt = tx.transaction_type
        if tt in (TransactionType.BUY, TransactionType.SELL):
            if isinstance(tx.quantity, bool) or not isinstance(tx.quantity, Decimal):
                raise CashProjectionError(
                    f"Transaction {tx.id} quantity must be a Decimal instance, got {type(tx.quantity).__name__}"
                )
            if not tx.quantity.is_finite() or tx.quantity <= Decimal("0"):
                raise CashProjectionError(
                    f"Transaction {tx.id} quantity must be a strictly positive finite Decimal, got {tx.quantity}"
                )

            if isinstance(tx.unit_price, bool) or not isinstance(tx.unit_price, Decimal):
                raise CashProjectionError(
                    f"Transaction {tx.id} unit_price must be a Decimal instance, got {type(tx.unit_price).__name__}"
                )
            if not tx.unit_price.is_finite() or tx.unit_price <= Decimal("0"):
                raise CashProjectionError(
                    f"Transaction {tx.id} unit_price must be a strictly positive finite Decimal, got {tx.unit_price}"
                )

            if isinstance(tx.trade_currency, bool) or not isinstance(tx.trade_currency, Currency):
                raise CashProjectionError(
                    f"Transaction {tx.id} trade_currency must be a Currency enum instance, got {type(tx.trade_currency).__name__}"
                )

            notional = _exact_decimal_mul(tx.quantity, tx.unit_price)
            key = (tx.account_id, tx.trade_currency)
            sign_mult = -1 if tt == TransactionType.BUY else 1
            deltas_by_key.setdefault(key, []).append((notional, sign_mult))

        elif tt in (
            TransactionType.CASH_DEPOSIT,
            TransactionType.CASH_WITHDRAWAL,
            TransactionType.DIVIDEND,
            TransactionType.INTEREST,
            TransactionType.FEE,
            TransactionType.TAX_WITHHOLDING,
        ):
            if isinstance(tx.cash_amount, bool) or not isinstance(tx.cash_amount, Decimal):
                raise CashProjectionError(
                    f"Transaction {tx.id} cash_amount must be a Decimal instance, got {type(tx.cash_amount).__name__}"
                )
            if not tx.cash_amount.is_finite() or tx.cash_amount <= Decimal("0"):
                raise CashProjectionError(
                    f"Transaction {tx.id} cash_amount must be a strictly positive finite Decimal, got {tx.cash_amount}"
                )

            if isinstance(tx.cash_currency, bool) or not isinstance(tx.cash_currency, Currency):
                raise CashProjectionError(
                    f"Transaction {tx.id} cash_currency must be a Currency enum instance, got {type(tx.cash_currency).__name__}"
                )

            key = (tx.account_id, tx.cash_currency)
            if tt in (TransactionType.CASH_DEPOSIT, TransactionType.DIVIDEND, TransactionType.INTEREST):
                sign_mult = 1
            else:  # CASH_WITHDRAWAL, FEE, TAX_WITHHOLDING
                sign_mult = -1
            deltas_by_key.setdefault(key, []).append((tx.cash_amount, sign_mult))

        elif tt == TransactionType.FX_CONVERSION:
            if isinstance(tx.from_amount, bool) or not isinstance(tx.from_amount, Decimal):
                raise CashProjectionError(
                    f"Transaction {tx.id} from_amount must be a Decimal instance, got {type(tx.from_amount).__name__}"
                )
            if not tx.from_amount.is_finite() or tx.from_amount <= Decimal("0"):
                raise CashProjectionError(
                    f"Transaction {tx.id} from_amount must be a strictly positive finite Decimal, got {tx.from_amount}"
                )

            if isinstance(tx.from_currency, bool) or not isinstance(tx.from_currency, Currency):
                raise CashProjectionError(
                    f"Transaction {tx.id} from_currency must be a Currency enum instance, got {type(tx.from_currency).__name__}"
                )

            if isinstance(tx.to_amount, bool) or not isinstance(tx.to_amount, Decimal):
                raise CashProjectionError(
                    f"Transaction {tx.id} to_amount must be a Decimal instance, got {type(tx.to_amount).__name__}"
                )
            if not tx.to_amount.is_finite() or tx.to_amount <= Decimal("0"):
                raise CashProjectionError(
                    f"Transaction {tx.id} to_amount must be a strictly positive finite Decimal, got {tx.to_amount}"
                )

            if isinstance(tx.to_currency, bool) or not isinstance(tx.to_currency, Currency):
                raise CashProjectionError(
                    f"Transaction {tx.id} to_currency must be a Currency enum instance, got {type(tx.to_currency).__name__}"
                )

            if tx.from_currency == tx.to_currency:
                raise CashProjectionError(
                    f"Transaction {tx.id} FX_CONVERSION requires distinct currencies, got {tx.from_currency} on both legs"
                )

            key_from = (tx.account_id, tx.from_currency)
            key_to = (tx.account_id, tx.to_currency)
            deltas_by_key.setdefault(key_from, []).append((tx.from_amount, -1))
            deltas_by_key.setdefault(key_to, []).append((tx.to_amount, 1))

        else:
            raise CashProjectionError(
                f"Unhandled transaction type {tt} in cash balance projection"
            )

    # Compute exact net cash balances per (account_id, currency)
    all_balances: List[CashBalanceState] = []
    positive_balances: List[CashBalanceState] = []

    # Sort keys deterministically by (str(account_id), currency.value)
    sorted_keys = sorted(deltas_by_key.keys(), key=lambda k: (str(k[0]), k[1].value))

    for account_id, currency in sorted_keys:
        deltas = deltas_by_key[(account_id, currency)]
        net_balance = _exact_decimal_sum(deltas)

        if net_balance < Decimal("0"):
            raise CashProjectionError(
                f"Negative net cash balance {net_balance} {currency.value} for account {account_id}. "
                "Overdraft or margin borrowing are unsupported."
            )

        cash_state = CashBalanceState(
            portfolio_id=view.portfolio_id,
            account_id=account_id,
            currency=currency,
            balance=net_balance,
        )
        all_balances.append(cash_state)
        if cash_state.is_positive:
            positive_balances.append(cash_state)

    return CashBalanceProjection(
        portfolio_id=view.portfolio_id,
        mode=view.mode,
        as_of_recorded_at=view.as_of_recorded_at,
        balances=tuple(all_balances),
        positive_balances=tuple(positive_balances),
    )
