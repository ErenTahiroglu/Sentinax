"""
backend/engine/private/allocation_rebalance_policy.py
=====================================================
Band-aware, minimum-sale, explicit-friction rebalance policy (Phase 21B), built on the Phase 21A exact cash-first reference.

Policy inputs are explicit and never defaulted: a per-asset trigger band, a per-asset destination band
(0 <= destination <= trigger <= 1) and explicit proportional buy / sell friction rates. Everything is exact amount-space,
context-free arithmetic (`Decimal.as_tuple()` integers); the only ambient-free Decimal context is the Phase 21A 50-digit
diagnostic context used for weight views.

    W      = sum(current_values) + investable_cash               T_i = weight_i * W
    trigger band      [T_i - trigger_i * W, T_i + trigger_i * W]      breach: v_i strictly outside (equality is inside)
    destination band  [L_i, U_i] = [max(T_i - destination_i * W, 0), T_i + destination_i * W]

The no-trade rule: if no asset breaches its trigger band there are no trades and the investable cash is retained (never deployed merely
because it exists). Trigger breaches are tested in amount space; weights are never rounded.

Triggered: every asset is repaired into its destination band and the investable cash is fully deployed. With
MS_i = max(v_i - U_i, 0), MB_i = max(L_i - v_i, 0), S0 = sum MS_i, B0 = sum MB_i and C = investable cash, the minimum gross sale
is the closed form

    S_min = max(S0, B0 - C, 0)

(always feasible because sum L <= W <= sum U). The gross sale is minimized FIRST and is never traded off against friction.
The two discretionary amounts are extra_sell = S_min - S0 and extra_buy = C + S_min - B0; they are mutually exclusive. Among
all plans with gross sale S_min the explicit proportional friction is minimized SECOND by a greedy allocation (lowest friction
rate first, canonical UUID order on ties) over the exact capacities, which is exact for this linear single-constraint box
problem. No solver is involved. No round trips: an asset is either bought or sold, never both. destination = 0 gives the exact
target, i.e. the Phase 21A plan.

Boundaries: no tax or tax-law inference (no capital gain, cost basis or lot selection); friction rates are explicit caller
inputs, not a historical fee or tax model, and friction is a planning diagnostic only: it is never deducted from notionals,
wealth or cash, and no execution cost is settled. The plan is optimal only in the narrow lexicographic sense above (minimum gross
sale, then minimum explicit friction); it is not globally optimal for any other objective, tax outcome or execution cost.
Notionals are never quantized. Same canonical UUID universe as Phase 21A; no persistence, orders or quantities.

Architectural Invariants:
    - Pure domain module: standard library plus the Phase 21A exact helpers. No network, filesystem, database, clock, randomness,
      float arithmetic, numpy/scipy/pandas or persistence; context-free exact arithmetic, no ambient Decimal context.
    - `BandAwareRebalancePlan` retains its target, state, policy and friction profile by identity and recomputes the canonical
      plan through the same private helper as the builder, rejecting forged trades. Economic zeros are unsigned Decimal 0.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.allocation_rebalance import (
    _ERR_FUNDING,
    _ERR_RANGE,
    _ERR_STATE_TYPE,
    _ERR_TARGET_TYPE,
    _ERR_TRADES_TYPE,
    _ERR_UNIVERSE,
    RebalanceCurrentState,
    RebalanceTargetAllocation,
    RebalanceTradeInstruction,
    RebalanceTradeStage,
    _analytics_context,
    _exact_add,
    _exact_mul,
    _exact_sub,
    _reconcile,
    _validate_ids,
)

_ERR_BAND_TYPE = "band drifts must be a tuple of exact Decimal instances"
_ERR_BAND_SHAPE = "band drifts must match instrument_ids exactly"
_ERR_TRIGGER_RANGE = "trigger drifts must be finite unsigned Decimals with 0 <= trigger <= 1"
_ERR_DESTINATION_RANGE = "destination drifts must be finite unsigned Decimals with 0 <= destination <= trigger"
_ERR_RATE_TYPE = "friction rates must be a tuple of exact Decimal instances"
_ERR_RATE_SHAPE = "friction rates must match instrument_ids exactly"
_ERR_RATE_RANGE = "friction rates must be finite unsigned Decimals"
_ERR_POLICY_TYPE = "policy must be an exact RebalanceBandPolicy instance"
_ERR_FRICTION_TYPE = "friction must be an exact RebalanceFrictionProfile instance"
_ERR_MATCH = "rebalance plan must match the canonical band-aware plan exactly"
_ERR_INCONSISTENT = "rebalance destination region is internally inconsistent"

_ZERO = Decimal(0)
_ONE = Decimal(1)


def _check_decimals(values: object, count: int, type_error: str, shape_error: str) -> None:
    if type(values) is not tuple or any(type(v) is not Decimal for v in values):
        raise TypeError(type_error)
    if len(values) != count:
        raise ValueError(shape_error)


@dataclass(frozen=True)
class RebalanceBandPolicy:
    """Explicit per-asset trigger and destination drifts (fractions of total wealth); no defaults."""
    instrument_ids: tuple[UUID, ...]
    trigger_drifts: tuple[Decimal, ...]
    destination_drifts: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        _validate_ids(self.instrument_ids)
        count = len(self.instrument_ids)
        _check_decimals(self.trigger_drifts, count, _ERR_BAND_TYPE, _ERR_BAND_SHAPE)
        _check_decimals(self.destination_drifts, count, _ERR_BAND_TYPE, _ERR_BAND_SHAPE)
        if any(not v.is_finite() or v.is_signed() or v > _ONE for v in self.trigger_drifts):
            raise ValueError(_ERR_TRIGGER_RANGE)
        if any(not v.is_finite() or v.is_signed() for v in self.destination_drifts):
            raise ValueError(_ERR_DESTINATION_RANGE)
        if any(dest > trig for dest, trig in zip(self.destination_drifts, self.trigger_drifts)):
            raise ValueError(_ERR_DESTINATION_RANGE)


@dataclass(frozen=True)
class RebalanceFrictionProfile:
    """Explicit proportional buy / sell friction rates per asset; a planning input only, never settled."""
    instrument_ids: tuple[UUID, ...]
    buy_friction_rates: tuple[Decimal, ...]
    sell_friction_rates: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        _validate_ids(self.instrument_ids)
        count = len(self.instrument_ids)
        _check_decimals(self.buy_friction_rates, count, _ERR_RATE_TYPE, _ERR_RATE_SHAPE)
        _check_decimals(self.sell_friction_rates, count, _ERR_RATE_TYPE, _ERR_RATE_SHAPE)
        if any(not v.is_finite() or v.is_signed() for v in (*self.buy_friction_rates, *self.sell_friction_rates)):
            raise ValueError(_ERR_RATE_RANGE)


def _allocate(total: Decimal, capacities: list[Decimal], rates: tuple[Decimal, ...]) -> list[Decimal]:
    """Greedy exact allocation of `total` over capacities: lowest rate first, canonical index order on ties."""
    result = [_ZERO] * len(capacities)
    remaining = total
    for index in sorted(range(len(capacities)), key=lambda i: (rates[i], i)):
        if remaining == 0:
            break
        piece = capacities[index] if capacities[index] <= remaining else remaining
        result[index] = piece
        remaining = _exact_sub(remaining, piece)
    if remaining != 0:
        raise ValueError(_ERR_INCONSISTENT)
    return result


def _checked_allocation(total: Decimal, capacities: list[Decimal], rates: tuple[Decimal, ...]) -> list[Decimal]:
    allocation = _allocate(total, capacities, rates)
    if _exact_add(allocation) != total or any(a > c for a, c in zip(allocation, capacities)):
        raise ValueError(_ERR_INCONSISTENT)
    return allocation


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return lower if value < lower else upper if value > upper else value


def _check_universe(
    target: RebalanceTargetAllocation,
    state: RebalanceCurrentState,
    policy: RebalanceBandPolicy,
    friction: RebalanceFrictionProfile,
) -> None:
    ids = target.instrument_ids
    if state.instrument_ids != ids or policy.instrument_ids != ids or friction.instrument_ids != ids:
        raise ValueError(_ERR_UNIVERSE)


def _bands(
    target: RebalanceTargetAllocation,
    state: RebalanceCurrentState,
    policy: RebalanceBandPolicy,
) -> tuple[Decimal, list[Decimal], list[Decimal], list[Decimal], list[Decimal], list[Decimal]]:
    """Exact (wealth, target values, trigger lower, trigger upper, destination lower, destination upper)."""
    wealth, targets, _, _, _ = _reconcile(target, state)
    trigger_lower: list[Decimal] = []
    trigger_upper: list[Decimal] = []
    destination_lower: list[Decimal] = []
    destination_upper: list[Decimal] = []
    for goal, trigger, destination in zip(targets, policy.trigger_drifts, policy.destination_drifts):
        half = _exact_mul(trigger, wealth)
        low = _exact_sub(goal, half)
        trigger_lower.append(low if low > 0 else _ZERO)
        trigger_upper.append(_exact_add([goal, half]))
        reach = _exact_mul(destination, wealth)
        low = _exact_sub(goal, reach)
        destination_lower.append(low if low > 0 else _ZERO)
        destination_upper.append(_exact_add([goal, reach]))
    if _exact_add(destination_lower) > wealth or _exact_add(destination_upper) < wealth:
        raise ValueError(_ERR_INCONSISTENT)
    return (wealth, targets, trigger_lower, trigger_upper, destination_lower, destination_upper)


@dataclass(frozen=True)
class _Solution:
    wealth: Decimal
    trigger_lower: tuple[Decimal, ...]
    trigger_upper: tuple[Decimal, ...]
    destination_lower: tuple[Decimal, ...]
    destination_upper: tuple[Decimal, ...]
    triggered: bool
    mandatory_sell: Decimal
    mandatory_buy: Decimal
    minimum_sale: Decimal
    sells: tuple[Decimal, ...]
    buys: tuple[Decimal, ...]
    trades: tuple[RebalanceTradeInstruction, ...]


def _solve(
    target: RebalanceTargetAllocation,
    state: RebalanceCurrentState,
    policy: RebalanceBandPolicy,
    friction: RebalanceFrictionProfile,
) -> _Solution:
    """Single canonical band-aware plan shared by the builder and the constructor verification."""
    _check_universe(target, state, policy, friction)
    wealth, _, trigger_lower, trigger_upper, lower, upper = _bands(target, state, policy)
    ids = target.instrument_ids
    values = state.current_values
    cash = state.investable_cash
    count = len(ids)
    triggered = any(v < lo or v > hi for v, lo, hi in zip(values, trigger_lower, trigger_upper))
    if not triggered:
        zeros = tuple(_ZERO for _ in ids)
        return _Solution(wealth, tuple(trigger_lower), tuple(trigger_upper), tuple(lower), tuple(upper), False,
                         _ZERO, _ZERO, _ZERO, zeros, zeros, ())
    mandatory_sells = [_exact_sub(v, hi) if v > hi else _ZERO for v, hi in zip(values, upper)]
    mandatory_buys = [_exact_sub(lo, v) if v < lo else _ZERO for v, lo in zip(values, lower)]
    sell0 = _exact_add(mandatory_sells)
    buy0 = _exact_add(mandatory_buys)
    shortfall = _exact_sub(buy0, cash)
    minimum_sale = sell0 if sell0 >= shortfall else shortfall
    extra_sell = _exact_sub(minimum_sale, sell0)
    extra_buy = _exact_sub(_exact_add([cash, minimum_sale]), buy0)
    if extra_sell < 0 or extra_buy < 0 or (extra_sell > 0 and extra_buy > 0):
        raise ValueError(_ERR_INCONSISTENT)
    clamped = [_clamp(v, lo, hi) for v, lo, hi in zip(values, lower, upper)]
    sell_extra = _checked_allocation(extra_sell, [_exact_sub(c, lo) for c, lo in zip(clamped, lower)], friction.sell_friction_rates)
    buy_extra = _checked_allocation(extra_buy, [_exact_sub(hi, c) for c, hi in zip(clamped, upper)], friction.buy_friction_rates)
    sells = [_exact_add([mandatory_sells[i], sell_extra[i]]) for i in range(count)]
    buys = [_exact_add([mandatory_buys[i], buy_extra[i]]) for i in range(count)]
    if any(sells[i] > 0 and buys[i] > 0 for i in range(count)):
        raise ValueError(_ERR_INCONSISTENT)
    if _exact_add(buys) != _exact_add([cash, *sells]):
        raise ValueError(_ERR_FUNDING)
    remaining = cash
    funded = [_ZERO] * count
    cash_stage: list[RebalanceTradeInstruction] = []
    for index in range(count):  # initial cash first, canonical instrument order
        if buys[index] > 0:
            piece = buys[index] if buys[index] <= remaining else remaining
            if piece > 0:
                cash_stage.append(RebalanceTradeInstruction(ids[index], RebalanceTradeStage.CASH_FUNDED_BUY, piece))
                funded[index] = piece
                remaining = _exact_sub(remaining, piece)
    if remaining != 0:
        raise ValueError(_ERR_FUNDING)
    sell_stage = [RebalanceTradeInstruction(ids[i], RebalanceTradeStage.SELL, sells[i]) for i in range(count) if sells[i] > 0]
    residual_stage = [
        RebalanceTradeInstruction(ids[i], RebalanceTradeStage.SALE_FUNDED_BUY, _exact_sub(buys[i], funded[i]))
        for i in range(count)
        if buys[i] > funded[i]
    ]
    final = [_exact_sub(_exact_add([values[i], buys[i]]), sells[i]) for i in range(count)]
    if any(f < lo or f > hi for f, lo, hi in zip(final, lower, upper)) or _exact_add(final) != wealth:
        raise ValueError(_ERR_INCONSISTENT)
    return _Solution(wealth, tuple(trigger_lower), tuple(trigger_upper), tuple(lower), tuple(upper), True, sell0, buy0,
                     minimum_sale, tuple(sells), tuple(buys), (*cash_stage, *sell_stage, *residual_stage))


@dataclass(frozen=True)
class BandAwareRebalancePlan:
    """Canonical band-aware rebalance plan retaining its target, state, policy and friction profile by identity."""
    target: RebalanceTargetAllocation
    state: RebalanceCurrentState
    policy: RebalanceBandPolicy
    friction: RebalanceFrictionProfile
    trades: tuple[RebalanceTradeInstruction, ...]

    def __post_init__(self) -> None:
        if type(self.target) is not RebalanceTargetAllocation:
            raise TypeError(_ERR_TARGET_TYPE)
        if type(self.state) is not RebalanceCurrentState:
            raise TypeError(_ERR_STATE_TYPE)
        if type(self.policy) is not RebalanceBandPolicy:
            raise TypeError(_ERR_POLICY_TYPE)
        if type(self.friction) is not RebalanceFrictionProfile:
            raise TypeError(_ERR_FRICTION_TYPE)
        if type(self.trades) is not tuple or any(type(t) is not RebalanceTradeInstruction for t in self.trades):
            raise TypeError(_ERR_TRADES_TYPE)
        if self.trades != self._solution().trades:
            raise ValueError(_ERR_MATCH)

    def _solution(self) -> _Solution:
        return _solve(self.target, self.state, self.policy, self.friction)

    @property
    def is_triggered(self) -> bool:
        return self._solution().triggered

    @property
    def total_wealth(self) -> Decimal:
        return self._solution().wealth

    @property
    def trigger_lower_values(self) -> tuple[Decimal, ...]:
        return self._solution().trigger_lower

    @property
    def trigger_upper_values(self) -> tuple[Decimal, ...]:
        return self._solution().trigger_upper

    @property
    def destination_lower_values(self) -> tuple[Decimal, ...]:
        return self._solution().destination_lower

    @property
    def destination_upper_values(self) -> tuple[Decimal, ...]:
        return self._solution().destination_upper

    @property
    def mandatory_sell_notional(self) -> Decimal:
        return self._solution().mandatory_sell

    @property
    def mandatory_buy_notional(self) -> Decimal:
        return self._solution().mandatory_buy

    @property
    def minimum_gross_sale_notional(self) -> Decimal:
        return self._solution().minimum_sale

    @property
    def gross_sell_notional(self) -> Decimal:
        return _exact_add(self._solution().sells)

    @property
    def gross_buy_notional(self) -> Decimal:
        return _exact_add(self._solution().buys)

    @property
    def estimated_buy_friction(self) -> Decimal:
        """Planning diagnostic: explicit buy rates times buy notionals; never deducted from anything."""
        buys = self._solution().buys
        return _exact_add([_exact_mul(b, r) for b, r in zip(buys, self.friction.buy_friction_rates)])

    @property
    def estimated_sell_friction(self) -> Decimal:
        """Planning diagnostic: explicit sell rates times sell notionals; never deducted from anything."""
        sells = self._solution().sells
        return _exact_add([_exact_mul(s, r) for s, r in zip(sells, self.friction.sell_friction_rates)])

    @property
    def estimated_total_friction(self) -> Decimal:
        return _exact_add([self.estimated_buy_friction, self.estimated_sell_friction])

    @property
    def post_trade_values(self) -> tuple[Decimal, ...]:
        solution = self._solution()
        return tuple(
            _exact_sub(_exact_add([v, b]), s)
            for v, b, s in zip(self.state.current_values, solution.buys, solution.sells)
        )

    @property
    def post_trade_cash(self) -> Decimal:
        solution = self._solution()
        return _exact_sub(_exact_add([self.state.investable_cash, *solution.sells]), _exact_add(solution.buys))

    def _weight_drifts(self, values: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        ctx = _analytics_context()
        wealth = self.total_wealth
        try:
            drifts = []
            for value, wanted in zip(values, self.target.weights):
                weight = ctx.divide(value, wealth) if value != 0 else _ZERO
                drift = ctx.subtract(weight, wanted)
                drifts.append(_ZERO if drift.is_zero() else drift)
            return tuple(drifts)
        except decimal.Overflow:
            raise ValueError(_ERR_RANGE) from None

    @property
    def current_weight_drifts(self) -> tuple[Decimal, ...]:
        """DIAGNOSTIC ONLY (fresh 50-digit context): current weight minus target weight; never drives a trigger."""
        return self._weight_drifts(self.state.current_values)

    @property
    def post_trade_weight_drifts(self) -> tuple[Decimal, ...]:
        """DIAGNOSTIC ONLY (fresh 50-digit context): post-trade weight minus target weight."""
        return self._weight_drifts(self.post_trade_values)


def build_band_aware_rebalance_plan(
    *,
    target: RebalanceTargetAllocation,
    state: RebalanceCurrentState,
    policy: RebalanceBandPolicy,
    friction: RebalanceFrictionProfile,
) -> BandAwareRebalancePlan:
    """The canonical band-aware plan: no trade inside the trigger band, else minimum sale, then minimum explicit friction."""
    if type(target) is not RebalanceTargetAllocation:
        raise TypeError(_ERR_TARGET_TYPE)
    if type(state) is not RebalanceCurrentState:
        raise TypeError(_ERR_STATE_TYPE)
    if type(policy) is not RebalanceBandPolicy:
        raise TypeError(_ERR_POLICY_TYPE)
    if type(friction) is not RebalanceFrictionProfile:
        raise TypeError(_ERR_FRICTION_TYPE)
    return BandAwareRebalancePlan(
        target=target, state=state, policy=policy, friction=friction,
        trades=_solve(target, state, policy, friction).trades,
    )
