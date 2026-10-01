"""
backend/engine/private/allocation_rebalance.py
==============================================
Exact, frictionless, cash-first rebalance reference plan (Phase 21A).

Given an explicit target allocation, explicit marked current values and explicit investable cash (one currency, one
canonical instrument universe), this module produces a notional-only plan that reaches the exact target:

    1. CASH_FUNDED_BUY    the initial investable cash is consumed first, in canonical instrument order
    2. SELL               every overweight asset is reduced by exactly its excess (minimum necessary sale)
    3. SALE_FUNDED_BUY    the residual buy needs, funded by the sale proceeds

Inputs are explicit and are never inferred. The repository has exact quantities and cash balances but no authoritative
market-value projection, cost-basis, lot or future-tax engine, so current values and investable cash are caller inputs.
`investable_cash` is cash that an upstream authority (CashBucket semantics) has ALREADY classified as investable; this module
never assumes that all account cash is investable and never touches reserve, near-term or restricted cash.

Exact amount-space arithmetic (context-free, no ambient Decimal context, no rounding, no currency quantization):

    W        = sum(current_values) + investable_cash          must be > 0
    T_i      = weight_i * W                                    exact; sum(T_i) == W exactly
    delta_i  = T_i - current_value_i                           > 0 buy need, < 0 sell need
    gross_buy_notional == investable_cash + gross_sell_notional    (funding identity, fail closed if broken)

Sales are necessary and minimal under the exact-target / frictionless model: only assets with delta < 0 are sold, each by
exactly -delta; no underweight asset is sold and no overweight asset is oversold. The canonical UUID order only fixes the
bookkeeping order in which the initial cash is staged; it is not a ranking and does not change any final trade delta.

Frictionless and notional-only: no tax or tax-law inference (no capital gain, lot, cost basis, FIFO / LIFO / HIFO), no
commission, spread, slippage or fee model (historical fee/tax observations are not a future transaction-cost model), no
rebalance bands, thresholds or drift triggers, no quantities, prices or orders, no ledger event, no persistence and no call to
any allocation optimizer. Current weights and drifts are DIAGNOSTIC views (fresh 50-digit context) and never drive notionals.
Trade notionals are never quantized to currency or instrument precision: execution translation is a later concern.

Universe: the target and the current state must use the same canonical instrument universe (ascending UUID string). There is
no union, no sell-to-zero of unknown assets and no inferred zero position; broader universe composition is a later phase.

Architectural Invariants:
    - Pure domain module: standard library plus `domain.Currency` and the reviewed Phase 18B exact-sum helper. No network,
      filesystem, database, clock, randomness, float arithmetic, numpy/scipy/pandas or persistence.
    - All money identities use context-free exact coefficient arithmetic (`Decimal.as_tuple()` integers). Exact alignment is
      bounded by `_EXACT_MAX_DECIMAL_PLACES`, a representation / memory resource ceiling (checked before any 10 ** shift) and
      NOT money rounding, financial materiality, a trade minimum or a drift tolerance.
    - `CashFirstRebalancePlan` retains its target and state by identity and recomputes the canonical plan through the same
      private helper as the builder, rejecting forged trades. All public economic zeros are unsigned Decimal 0.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_benchmarks import _sums_to_exactly_one
from backend.engine.private.domain import Currency

_ERR_IDS_TYPE = "instrument_ids must be a non-empty tuple of exact UUID instances"
_ERR_IDS_VALUE = "instrument_ids must be unique and in canonical ascending UUID string order"
_ERR_WEIGHTS_TYPE = "weights must be a tuple of exact Decimal instances"
_ERR_WEIGHTS_SHAPE = "weights must match instrument_ids exactly"
_ERR_WEIGHTS_RANGE = "weights must be finite unsigned Decimals with 0 <= weight <= 1"
_ERR_WEIGHTS_SUM = "weights must sum to exactly 1"
_ERR_VALUES_TYPE = "current_values must be a tuple of exact Decimal instances"
_ERR_VALUES_SHAPE = "current_values must match instrument_ids exactly"
_ERR_AMOUNT = "current values and investable cash must be finite unsigned non-negative Decimals"
_ERR_CASH_TYPE = "investable_cash must be an exact Decimal instance"
_ERR_CURRENCY = "currency must be an exact Currency instance"
_ERR_INSTRUMENT = "instrument_id must be an exact UUID instance"
_ERR_STAGE = "stage must be an exact RebalanceTradeStage instance"
_ERR_NOTIONAL_TYPE = "notional must be an exact Decimal instance"
_ERR_NOTIONAL_VALUE = "notional must be a finite Decimal greater than zero"
_ERR_TARGET_TYPE = "target must be an exact RebalanceTargetAllocation instance"
_ERR_STATE_TYPE = "state must be an exact RebalanceCurrentState instance"
_ERR_TRADES_TYPE = "trades must be a tuple of exact RebalanceTradeInstruction instances"
_ERR_UNIVERSE = "rebalance target and current state must use the same canonical instrument universe"
_ERR_WEALTH = "rebalance total wealth must be strictly positive"
_ERR_FUNDING = "rebalance funding identity is inconsistent"
_ERR_MATCH = "rebalance plan must match the canonical cash-first plan exactly"
_ERR_RANGE = "rebalance analytics exceeds supported Decimal range"

# Representation / memory resource ceiling for exact alignment (base-10 places). Not money rounding or a financial threshold.
_EXACT_MAX_DECIMAL_PLACES = 1000


def _analytics_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _exact_parts(value: Decimal) -> tuple[int, int]:
    """Exact (signed integer coefficient, base-10 exponent) of a finite Decimal; no Decimal arithmetic."""
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return (-coefficient if sign else coefficient, exponent)  # type: ignore[return-value]


def _exact_build(coefficient: int, exponent: int) -> Decimal:
    """Exact Decimal from an integer coefficient and exponent; zero is plain unsigned Decimal 0."""
    if coefficient == 0:
        return Decimal(0)
    digits: list[int] = []
    remaining = -coefficient if coefficient < 0 else coefficient
    while remaining:
        remaining, digit = divmod(remaining, 10)
        digits.append(digit)
    try:
        return Decimal((1 if coefficient < 0 else 0, tuple(reversed(digits)), exponent))
    except (ValueError, decimal.InvalidOperation):
        raise ValueError(_ERR_RANGE) from None


def _exact_add(values: list[Decimal] | tuple[Decimal, ...]) -> Decimal:
    """Exact sum: integer coefficients aligned to the lowest exponent; the span is bounded BEFORE any 10 ** shift."""
    parts = [part for part in (_exact_parts(v) for v in values) if part[0] != 0]
    if not parts:
        return Decimal(0)
    lowest = highest = parts[0][1]
    for _, exponent in parts:
        if exponent < lowest:
            lowest = exponent
        if exponent > highest:
            highest = exponent
    if highest - lowest > _EXACT_MAX_DECIMAL_PLACES:
        raise ValueError(_ERR_RANGE)
    total = 0
    for coefficient, exponent in parts:
        total += coefficient * 10 ** (exponent - lowest)
    return _exact_build(total, lowest)


def _exact_sub(left: Decimal, right: Decimal) -> Decimal:
    return _exact_add([left, right.copy_negate()])


def _exact_mul(left: Decimal, right: Decimal) -> Decimal:
    coefficient_left, exponent_left = _exact_parts(left)
    coefficient_right, exponent_right = _exact_parts(right)
    return _exact_build(coefficient_left * coefficient_right, exponent_left + exponent_right)


def _validate_ids(ids: object) -> None:
    if type(ids) is not tuple or not ids or any(type(i) is not UUID for i in ids):
        raise TypeError(_ERR_IDS_TYPE)
    keys = [str(i) for i in ids]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise ValueError(_ERR_IDS_VALUE)


class RebalanceTradeStage(Enum):
    CASH_FUNDED_BUY = "cash_funded_buy"
    SELL = "sell"
    SALE_FUNDED_BUY = "sale_funded_buy"


@dataclass(frozen=True)
class RebalanceTargetAllocation:
    """Explicit target weights over a canonical instrument universe; a zero weight is allowed; the sum is exactly 1."""
    instrument_ids: tuple[UUID, ...]
    weights: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        _validate_ids(self.instrument_ids)
        if type(self.weights) is not tuple or any(type(w) is not Decimal for w in self.weights):
            raise TypeError(_ERR_WEIGHTS_TYPE)
        if len(self.weights) != len(self.instrument_ids):
            raise ValueError(_ERR_WEIGHTS_SHAPE)
        if any(not w.is_finite() or w.is_signed() or w > Decimal(1) for w in self.weights):
            raise ValueError(_ERR_WEIGHTS_RANGE)
        try:
            exact = _sums_to_exactly_one(self.weights)
        except ValueError:
            raise ValueError(_ERR_RANGE) from None
        if not exact:
            raise ValueError(_ERR_WEIGHTS_SUM)


@dataclass(frozen=True)
class RebalanceCurrentState:
    """Explicit marked current values and already-classified investable cash, all in one currency."""
    instrument_ids: tuple[UUID, ...]
    current_values: tuple[Decimal, ...]
    investable_cash: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        _validate_ids(self.instrument_ids)
        if type(self.current_values) is not tuple or any(type(v) is not Decimal for v in self.current_values):
            raise TypeError(_ERR_VALUES_TYPE)
        if len(self.current_values) != len(self.instrument_ids):
            raise ValueError(_ERR_VALUES_SHAPE)
        if type(self.investable_cash) is not Decimal:
            raise TypeError(_ERR_CASH_TYPE)
        if any(not v.is_finite() or v.is_signed() for v in (*self.current_values, self.investable_cash)):
            raise ValueError(_ERR_AMOUNT)
        if type(self.currency) is not Currency:
            raise TypeError(_ERR_CURRENCY)


@dataclass(frozen=True)
class RebalanceTradeInstruction:
    """One notional instruction (never a quantity or order); notional is strictly positive."""
    instrument_id: UUID
    stage: RebalanceTradeStage
    notional: Decimal

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError(_ERR_INSTRUMENT)
        if type(self.stage) is not RebalanceTradeStage:
            raise TypeError(_ERR_STAGE)
        if type(self.notional) is not Decimal:
            raise TypeError(_ERR_NOTIONAL_TYPE)
        if not self.notional.is_finite() or self.notional.is_signed() or self.notional == 0:
            raise ValueError(_ERR_NOTIONAL_VALUE)


def _reconcile(
    target: RebalanceTargetAllocation,
    state: RebalanceCurrentState,
) -> tuple[Decimal, list[Decimal], list[Decimal], Decimal, Decimal]:
    """Exact (wealth, target values, deltas, gross buy, gross sell); fails closed on any identity break."""
    if state.instrument_ids != target.instrument_ids:
        raise ValueError(_ERR_UNIVERSE)
    wealth = _exact_add([*state.current_values, state.investable_cash])
    if wealth <= 0:
        raise ValueError(_ERR_WEALTH)
    targets = [_exact_mul(weight, wealth) for weight in target.weights]
    if _exact_add(targets) != wealth:
        raise ValueError(_ERR_FUNDING)
    deltas = [_exact_sub(value, current) for value, current in zip(targets, state.current_values)]
    gross_buy = _exact_add([delta for delta in deltas if delta > 0])
    gross_sell = _exact_add([delta.copy_negate() for delta in deltas if delta < 0])
    if _exact_add([state.investable_cash, gross_sell]) != gross_buy:
        raise ValueError(_ERR_FUNDING)
    return (wealth, targets, deltas, gross_buy, gross_sell)


def _canonical_trades(target: RebalanceTargetAllocation, state: RebalanceCurrentState) -> tuple[RebalanceTradeInstruction, ...]:
    """Single canonical cash-first plan shared by the builder and the constructor verification."""
    _, _, deltas, _, gross_sell = _reconcile(target, state)
    ids = target.instrument_ids
    remaining = state.investable_cash
    funded = [Decimal(0)] * len(ids)
    cash_stage: list[RebalanceTradeInstruction] = []
    for index, delta in enumerate(deltas):  # stage 1: initial cash first, canonical instrument order
        if delta > 0:
            piece = delta if delta <= remaining else remaining
            if piece > 0:
                cash_stage.append(RebalanceTradeInstruction(ids[index], RebalanceTradeStage.CASH_FUNDED_BUY, piece))
                funded[index] = piece
                remaining = _exact_sub(remaining, piece)
    if remaining != 0:
        raise ValueError(_ERR_FUNDING)
    sell_stage = [
        RebalanceTradeInstruction(ids[index], RebalanceTradeStage.SELL, delta.copy_negate())
        for index, delta in enumerate(deltas)
        if delta < 0
    ]
    residuals = [(index, _exact_sub(delta, funded[index])) for index, delta in enumerate(deltas) if delta > 0]
    residual_stage = [
        RebalanceTradeInstruction(ids[index], RebalanceTradeStage.SALE_FUNDED_BUY, residual)
        for index, residual in residuals
        if residual > 0
    ]
    if _exact_add([instruction.notional for instruction in residual_stage]) != gross_sell:
        raise ValueError(_ERR_FUNDING)
    return (*cash_stage, *sell_stage, *residual_stage)


@dataclass(frozen=True)
class CashFirstRebalancePlan:
    """Canonical cash-first, frictionless rebalance plan retaining its target and state by identity."""
    target: RebalanceTargetAllocation
    state: RebalanceCurrentState
    trades: tuple[RebalanceTradeInstruction, ...]

    def __post_init__(self) -> None:
        if type(self.target) is not RebalanceTargetAllocation:
            raise TypeError(_ERR_TARGET_TYPE)
        if type(self.state) is not RebalanceCurrentState:
            raise TypeError(_ERR_STATE_TYPE)
        if type(self.trades) is not tuple or any(type(t) is not RebalanceTradeInstruction for t in self.trades):
            raise TypeError(_ERR_TRADES_TYPE)
        if self.state.instrument_ids != self.target.instrument_ids:
            raise ValueError(_ERR_UNIVERSE)
        if self.trades != _canonical_trades(self.target, self.state):
            raise ValueError(_ERR_MATCH)

    @property
    def total_wealth(self) -> Decimal:
        return _reconcile(self.target, self.state)[0]

    @property
    def target_values(self) -> tuple[Decimal, ...]:
        return tuple(_reconcile(self.target, self.state)[1])

    @property
    def trade_deltas(self) -> tuple[Decimal, ...]:
        return tuple(_reconcile(self.target, self.state)[2])

    @property
    def gross_buy_notional(self) -> Decimal:
        return _reconcile(self.target, self.state)[3]

    @property
    def gross_sell_notional(self) -> Decimal:
        return _reconcile(self.target, self.state)[4]

    @property
    def cash_funded_buy_notional(self) -> Decimal:
        return _exact_add([t.notional for t in self.trades if t.stage is RebalanceTradeStage.CASH_FUNDED_BUY])

    @property
    def sale_funded_buy_notional(self) -> Decimal:
        return _exact_add([t.notional for t in self.trades if t.stage is RebalanceTradeStage.SALE_FUNDED_BUY])

    @property
    def post_trade_values(self) -> tuple[Decimal, ...]:
        """Current values after applying the instructions exactly (equals target_values for a valid plan)."""
        values = {i: v for i, v in zip(self.state.instrument_ids, self.state.current_values)}
        for trade in self.trades:
            if trade.stage is RebalanceTradeStage.SELL:
                values[trade.instrument_id] = _exact_sub(values[trade.instrument_id], trade.notional)
            else:
                values[trade.instrument_id] = _exact_add([values[trade.instrument_id], trade.notional])
        return tuple(values[i] for i in self.state.instrument_ids)

    @property
    def post_trade_cash(self) -> Decimal:
        """Initial cash plus sale proceeds minus every buy (exactly 0 for a valid plan)."""
        proceeds = _exact_add([t.notional for t in self.trades if t.stage is RebalanceTradeStage.SELL])
        buys = _exact_add([t.notional for t in self.trades if t.stage is not RebalanceTradeStage.SELL])
        return _exact_sub(_exact_add([self.state.investable_cash, proceeds]), buys)

    @property
    def current_weights(self) -> tuple[Decimal, ...]:
        """DIAGNOSTIC ONLY (fresh 50-digit context): current_value / total wealth; never drives trade notionals."""
        ctx = _analytics_context()
        wealth = self.total_wealth
        try:
            return tuple(ctx.divide(value, wealth) if value != 0 else Decimal(0) for value in self.state.current_values)
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def weight_drifts(self) -> tuple[Decimal, ...]:
        """DIAGNOSTIC ONLY: current weight minus target weight; no trigger band is attached to it."""
        ctx = _analytics_context()
        try:
            return tuple(
                Decimal(0) if (drift := ctx.subtract(current, wanted)).is_zero() else drift
                for current, wanted in zip(self.current_weights, self.target.weights)
            )
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None


def build_cash_first_rebalance_plan(
    *,
    target: RebalanceTargetAllocation,
    state: RebalanceCurrentState,
) -> CashFirstRebalancePlan:
    """The canonical frictionless cash-first rebalance reference plan; no policy parameters."""
    if type(target) is not RebalanceTargetAllocation:
        raise TypeError(_ERR_TARGET_TYPE)
    if type(state) is not RebalanceCurrentState:
        raise TypeError(_ERR_STATE_TYPE)
    return CashFirstRebalancePlan(target=target, state=state, trades=_canonical_trades(target, state))
