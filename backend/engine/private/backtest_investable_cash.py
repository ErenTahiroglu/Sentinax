"""
backend/engine/private/backtest_investable_cash.py
==================================================
Explicit fixed-replay investable-cash allocation (Phase 26D3B). D3A retains the RAW account/currency cash projection; raw cash is not investable cash. This module answers only: under
an explicitly supplied fixed replay policy, how much of each positive raw historical cash balance is declared investable?

Why a fixed policy: `public.cash_buckets` carries `purpose`, `included_in_investable_assets` and `archived_at`, which the persistence schema lets anyone UPDATE in place (only identity,
ownership, account and currency are immutable). There is no bucket revision table, no recorded-time version history and no availability timeline, so today's row cannot say what a
historical replay point would have classified. Nothing here reads cash_buckets, a transaction's bucket reference, a bucket name or purpose, or CASH_BALANCE risk evidence (which carries no
purpose or investability), and nothing queries a database. The allocations are CONDITIONAL / COUNTERFACTUAL policy parameters: they prove "given the historical raw cash and this policy, these
amounts are designated investable", not that the allocations or any bucket classification existed historically or that the user applied this policy.

Rules. Every positive raw balance, identified by (account_id, currency), needs exactly one explicit allocation, and the classification is not aggregated first (two TRY accounts need two
decisions). Zero is an explicit decision, never "missing"; a zero raw balance (kept in the projection for audit) takes no allocation and an allocation for it is extra evidence. Missing,
duplicate and extra keys fail with no first-wins and no implicit zero. An amount is an exact finite unsigned Decimal that never exceeds its raw balance (no tolerance or rounding). Only
the explicit valuation currency of the D3A state may carry a positive amount; every other currency must be exactly zero, because there is no FX in this replay (foreign raw cash stays
visible, unconverted and not investable in the valuation currency). Allocations are canonicalized by (str(account_id), currency.value), each supplied object is preserved by identity, and
direct construction requires that order. `investable_cash` is a derived read-only property (an exact, ambient-context-independent sum for the valuation currency), never a stored field,
so there is no total to falsify; zero is valid (the policy designates none) and, with no positive raw cash, the allocations are exactly empty.

The D3A state is retained by identity and untouched: no holding, price or valuation currency is read or changed, no RebalanceCurrentState is built and nothing is composed or rebalanced.
Non-pure by dependency composition; no I/O, clock, randomness, hashing or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.backtest_marked_holdings import PrivateBacktestMarkedHoldingsState
from backend.engine.private.domain import Currency

_ERR_STATE = "marked_holdings_state must be an exact PrivateBacktestMarkedHoldingsState instance"
_ERR_ALLOCATIONS = "allocations must be an exact tuple of exact PrivateBacktestCashAllocation instances"
_ERR_ACCOUNT = "account_id must be an exact UUID"
_ERR_CURRENCY = "currency must be an exact Currency member"
_ERR_AMOUNT_TYPE = "investable_amount must be an exact Decimal"
_ERR_AMOUNT = "investable_amount must be finite and unsigned (zero is a valid explicit decision)"
_ERR_ORDER = "allocations must be in canonical (str(account_id), currency.value) order"
_ERR_DUPLICATE = "more than one allocation for the same (account_id, currency)"
_ERR_EXTRA = "allocation does not correspond to a positive raw cash balance"
_ERR_MISSING = "every positive raw cash balance requires exactly one explicit allocation"
_ERR_CEILING = "investable_amount cannot exceed the raw cash balance"
_ERR_FOREIGN = "an allocation in a non-valuation currency must be exactly zero (no FX)"

_ZERO = Decimal("0")


@dataclass(frozen=True)
class PrivateBacktestCashAllocation:
    """One explicit fixed-replay decision: how much of the raw balance of (account, currency) is designated investable."""
    account_id: UUID
    currency: Currency
    investable_amount: Decimal

    def __post_init__(self) -> None:
        if type(self.account_id) is not UUID:
            raise TypeError(_ERR_ACCOUNT)
        if type(self.currency) is not Currency:
            raise TypeError(_ERR_CURRENCY)
        if type(self.investable_amount) is not Decimal:
            raise TypeError(_ERR_AMOUNT_TYPE)
        if not self.investable_amount.is_finite() or self.investable_amount.is_signed():
            raise ValueError(_ERR_AMOUNT)


def _order_key(allocation: PrivateBacktestCashAllocation) -> tuple:
    return (str(allocation.account_id), allocation.currency.value)


def _check_types(marked_holdings_state: object, allocations: object) -> None:
    if type(marked_holdings_state) is not PrivateBacktestMarkedHoldingsState:
        raise TypeError(_ERR_STATE)
    if type(allocations) is not tuple or not all(type(allocation) is PrivateBacktestCashAllocation for allocation in allocations):
        raise TypeError(_ERR_ALLOCATIONS)


def _validate(marked_holdings_state: PrivateBacktestMarkedHoldingsState, allocations: tuple) -> None:
    """The single validation path shared by the builder and `__post_init__`; allocations must already be in canonical order."""
    valuation_currency = marked_holdings_state.valuation_currency
    raw_balances = {(balance.account_id, balance.currency): balance.balance for balance in marked_holdings_state.cash_projection.positive_balances}
    for earlier, later in zip(allocations, allocations[1:]):
        if _order_key(earlier) == _order_key(later):
            raise ValueError(_ERR_DUPLICATE)
        if _order_key(earlier) > _order_key(later):
            raise ValueError(_ERR_ORDER)
    for allocation in allocations:
        key = (allocation.account_id, allocation.currency)
        if key not in raw_balances:
            raise ValueError(_ERR_EXTRA)
        if allocation.investable_amount > raw_balances[key]:
            raise ValueError(_ERR_CEILING)
        if allocation.currency is not valuation_currency and allocation.investable_amount != _ZERO:
            raise ValueError(_ERR_FOREIGN)
    if len(allocations) != len(raw_balances):
        raise ValueError(_ERR_MISSING)


def _exact_sum(values: list) -> Decimal:
    """Exact sum by integer arithmetic, independent of the ambient Decimal context (no rounding, no quantization)."""
    if not values:
        return _ZERO
    parts = []
    for value in values:
        sign, digits, exponent = value.as_tuple()
        magnitude = 0
        for digit in digits:
            magnitude = magnitude * 10 + digit
        parts.append(((-magnitude if sign else magnitude), exponent))
    smallest = min(exponent for _integer, exponent in parts)
    total = sum(integer * 10 ** (exponent - smallest) for integer, exponent in parts)
    return Decimal((1 if total < 0 else 0, tuple(int(character) for character in str(abs(total))), smallest))


@dataclass(frozen=True)
class PrivateBacktestInvestableCashSelection:
    """The D3A state and the explicit fixed-replay allocation of every positive raw cash balance; the investable total is derived, never stored."""
    marked_holdings_state: PrivateBacktestMarkedHoldingsState
    allocations: tuple[PrivateBacktestCashAllocation, ...]

    def __post_init__(self) -> None:
        _check_types(self.marked_holdings_state, self.allocations)
        _validate(self.marked_holdings_state, self.allocations)

    @property
    def investable_cash(self) -> Decimal:
        """Exact sum of the valuation-currency allocations (foreign allocations are zero by construction)."""
        valuation_currency = self.marked_holdings_state.valuation_currency
        return _exact_sum([allocation.investable_amount for allocation in self.allocations if allocation.currency is valuation_currency])


def build_private_backtest_investable_cash_selection(
    *,
    marked_holdings_state: PrivateBacktestMarkedHoldingsState,
    allocations: tuple[PrivateBacktestCashAllocation, ...],
) -> PrivateBacktestInvestableCashSelection:
    """Canonicalize the explicit allocations (caller order has no meaning, objects are kept by identity) and validate them against the raw cash projection."""
    _check_types(marked_holdings_state, allocations)
    ordered = tuple(sorted(allocations, key=_order_key))
    return PrivateBacktestInvestableCashSelection(marked_holdings_state=marked_holdings_state, allocations=ordered)
