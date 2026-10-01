"""
backend/tests/test_allocation_rebalance_policy.py
=================================================
Phase 21B: band-aware, exact minimum-sale, explicit-friction rebalance policy on top of the Phase 21A exact reference.

    no trigger breach -> no trades (cash retained)
    breach            -> rebalance into the destination band, cash fully deployed;
                         gross sales minimized first (closed form S_min = max(S0, B0 - C, 0)),
                         explicit proportional friction minimized second (greedy, no solver).

Notional-only, single currency, exact amount space. No tax, no cost-basis, no settlement of friction.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import itertools
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_rebalance_policy as module_under_test
from backend.engine.private.allocation_rebalance import (
    RebalanceCurrentState,
    RebalanceTargetAllocation,
    RebalanceTradeInstruction,
    RebalanceTradeStage,
    build_cash_first_rebalance_plan,
)
from backend.engine.private.allocation_rebalance_policy import (
    BandAwareRebalancePlan,
    RebalanceBandPolicy,
    RebalanceFrictionProfile,
    build_band_aware_rebalance_plan,
)
from backend.engine.private.domain import Currency
from backend.tests.invariants import static_guards as sg

D = Decimal
F = Fraction
STAGE = RebalanceTradeStage
A, B, C, E = (UUID(int=i) for i in (1, 2, 3, 4))
ERR_UNIVERSE = "rebalance target and current state must use the same canonical instrument universe"
ERR_MATCH = "rebalance plan must match the canonical band-aware plan exactly"
ERR_INCONSISTENT = "rebalance destination region is internally inconsistent"
ERR_RANGE = "rebalance analytics exceeds supported Decimal range"


def _target(ids, weights) -> RebalanceTargetAllocation:
    return RebalanceTargetAllocation(instrument_ids=tuple(ids), weights=tuple(D(w) for w in weights))


def _state(ids, values, cash="0") -> RebalanceCurrentState:
    return RebalanceCurrentState(instrument_ids=tuple(ids), current_values=tuple(D(v) for v in values),
                                 investable_cash=D(cash), currency=Currency.TRY)


def _policy(ids, trigger, destination) -> RebalanceBandPolicy:
    return RebalanceBandPolicy(instrument_ids=tuple(ids), trigger_drifts=tuple(D(v) for v in trigger),
                               destination_drifts=tuple(D(v) for v in destination))


def _friction(ids, buy=None, sell=None) -> RebalanceFrictionProfile:
    zeros = ["0"] * len(ids)
    return RebalanceFrictionProfile(instrument_ids=tuple(ids), buy_friction_rates=tuple(D(v) for v in (buy or zeros)),
                                    sell_friction_rates=tuple(D(v) for v in (sell or zeros)))


def _plan(ids, weights, values, cash, trigger, destination, buy=None, sell=None) -> BandAwareRebalancePlan:
    return build_band_aware_rebalance_plan(
        target=_target(ids, weights), state=_state(ids, values, cash), policy=_policy(ids, trigger, destination),
        friction=_friction(ids, buy, sell),
    )


def _summary(plan):
    return [(t.instrument_id, t.stage, t.notional) for t in plan.trades]


def _exact(values) -> Fraction:
    return sum((F(v) for v in values), F(0))


def _assert_invariants(plan: BandAwareRebalancePlan) -> None:
    buys, sells = {}, {}
    for trade in plan.trades:
        side = sells if trade.stage is STAGE.SELL else buys
        side[trade.instrument_id] = side.get(trade.instrument_id, F(0)) + F(trade.notional)
    assert not set(buys) & set(sells)  # no round trip
    assert all(not t.notional.is_signed() and t.notional > 0 for t in plan.trades)
    if plan.is_triggered:
        assert plan.post_trade_cash == 0
        assert _exact(plan.post_trade_values) == F(plan.total_wealth)
        assert all(lo <= v <= hi for lo, v, hi in zip(plan.destination_lower_values, plan.post_trade_values, plan.destination_upper_values))
        assert F(plan.gross_sell_notional) == F(plan.minimum_gross_sale_notional)
        assert F(plan.gross_buy_notional) == F(plan.state.investable_cash) + F(plan.gross_sell_notional)
    else:
        assert plan.trades == () and plan.post_trade_values == plan.state.current_values
        assert plan.post_trade_cash == plan.state.investable_cash and plan.minimum_gross_sale_notional == 0


# --- policy contract ---------------------------------------------------------------------------------------------

def test_policy_has_exactly_three_stored_fields_and_is_frozen() -> None:
    policy = _policy([A, B], ["0.05", "0.05"], ["0.02", "0"])
    assert [f.name for f in dataclasses.fields(RebalanceBandPolicy)] == ["instrument_ids", "trigger_drifts", "destination_drifts"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.trigger_drifts = ()  # type: ignore[misc]


def test_policy_ids_follow_the_canonical_uuid_contract() -> None:
    for bad in ([A, B], (), None, (A, "x")):
        with pytest.raises((TypeError, ValueError)):
            RebalanceBandPolicy(instrument_ids=bad, trigger_drifts=(D("0.1"), D("0.1")), destination_drifts=(D(0), D(0)))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _policy([B, A], ["0.1", "0.1"], ["0", "0"])   # validated, never sorted
    with pytest.raises(ValueError):
        _policy([A, A], ["0.1", "0.1"], ["0", "0"])


def test_policy_band_contract() -> None:
    class _DecSub(Decimal):
        pass

    ids = (A, B)
    ok = (D("0.1"), D("0.1"))
    for bad in ([D("0.1"), D("0.1")], (0.1, 0.1), (1, 0), (True, False), ("0.1", "0.1"), (_DecSub("0.1"), D("0.1")), None):
        with pytest.raises(TypeError):
            RebalanceBandPolicy(instrument_ids=ids, trigger_drifts=bad, destination_drifts=(D(0), D(0)))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            RebalanceBandPolicy(instrument_ids=ids, trigger_drifts=ok, destination_drifts=bad)  # type: ignore[arg-type]
    for bad in (("NaN", "0.1"), ("Infinity", "0.1"), ("-0.1", "0.1"), ("-0", "0.1"), ("1.0000001", "0.1")):
        with pytest.raises(ValueError):
            _policy([A, B], bad, ["0", "0"])
    for bad in (("NaN", "0"), ("-0.1", "0"), ("-0", "0")):
        with pytest.raises(ValueError):
            _policy([A, B], ["0.1", "0.1"], bad)
    with pytest.raises(ValueError):
        _policy([A, B], ["0.1", "0.1"], ["0.1000001", "0"])      # destination must not exceed trigger
    with pytest.raises(ValueError):
        _policy([A, B], ["0.1", "0.05"], ["0.05", "0.06"])        # per asset, not in aggregate
    for bad in (((D("0.1"),), (D(0), D(0))), ((D("0.1"), D("0.1")), (D(0),))):
        with pytest.raises(ValueError):
            RebalanceBandPolicy(instrument_ids=ids, trigger_drifts=bad[0], destination_drifts=bad[1])
    assert _policy([A, B], ["1", "0"], ["1", "0"]).trigger_drifts == (D(1), D(0))  # 0 <= destination <= trigger <= 1 inclusive


def test_there_are_no_default_bands() -> None:
    with pytest.raises(TypeError):
        RebalanceBandPolicy(instrument_ids=(A, B))  # type: ignore[call-arg]


# --- friction profile contract -----------------------------------------------------------------------------------

def test_friction_profile_contract() -> None:
    class _DecSub(Decimal):
        pass

    assert [f.name for f in dataclasses.fields(RebalanceFrictionProfile)] == ["instrument_ids", "buy_friction_rates", "sell_friction_rates"]
    ids = (A, B)
    good = (D("0.01"), D("0"))
    for bad in ([D(0), D(0)], (0.1, 0.1), (1, 0), (True, False), ("0", "0"), (_DecSub(0), D(0)), None):
        with pytest.raises(TypeError):
            RebalanceFrictionProfile(instrument_ids=ids, buy_friction_rates=bad, sell_friction_rates=good)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            RebalanceFrictionProfile(instrument_ids=ids, buy_friction_rates=good, sell_friction_rates=bad)  # type: ignore[arg-type]
    for bad in (("NaN", "0"), ("Infinity", "0"), ("-0.01", "0"), ("-0", "0")):
        with pytest.raises(ValueError):
            _friction([A, B], buy=bad)
        with pytest.raises(ValueError):
            _friction([A, B], sell=bad)
    for bad in ((D(0),), (D(0), D(0), D(0))):
        with pytest.raises(ValueError):
            RebalanceFrictionProfile(instrument_ids=ids, buy_friction_rates=bad, sell_friction_rates=good)
    with pytest.raises(TypeError):
        RebalanceFrictionProfile(instrument_ids=ids, buy_friction_rates=good)  # type: ignore[call-arg]  # no implicit zero fill
    assert _friction([A, B], buy=["1000", "0"]).buy_friction_rates[0] == 1000  # no upper bound


# --- builder / plan contract -------------------------------------------------------------------------------------

def test_builder_is_keyword_only_with_exact_types_and_no_defaults() -> None:
    ids = [A, B]
    target, state, policy, friction = _target(ids, ["0.5", "0.5"]), _state(ids, ["47", "48"], "5"), _policy(ids, ["0.05"] * 2, ["0.05"] * 2), _friction(ids)
    with pytest.raises(TypeError):
        build_band_aware_rebalance_plan(target, state, policy, friction)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_band_aware_rebalance_plan(target=target, state=state, policy=policy)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_band_aware_rebalance_plan(target=target, state=state, policy=policy, friction=friction, tax=D(1))  # type: ignore[call-arg]
    for field in ("target", "state", "policy", "friction"):
        values = dict(target=target, state=state, policy=policy, friction=friction)
        values[field] = object()
        with pytest.raises(TypeError):
            build_band_aware_rebalance_plan(**values)  # type: ignore[arg-type]


def test_policy_and_friction_universes_must_equal_the_target_and_state_universe() -> None:
    target, state = _target([A, B], ["0.5", "0.5"]), _state([A, B], ["40", "40"], "20")
    ok_policy, ok_friction = _policy([A, B], ["0.05"] * 2, ["0.05"] * 2), _friction([A, B])
    for bad_ids in ([A, C], [A, B, C], [A]):
        with pytest.raises(ValueError, match=ERR_UNIVERSE):
            build_band_aware_rebalance_plan(target=target, state=state, policy=_policy(bad_ids, ["0.05"] * len(bad_ids), ["0.05"] * len(bad_ids)), friction=ok_friction)
        with pytest.raises(ValueError, match=ERR_UNIVERSE):
            build_band_aware_rebalance_plan(target=target, state=state, policy=ok_policy, friction=_friction(bad_ids))
    with pytest.raises(ValueError, match=ERR_UNIVERSE):
        build_band_aware_rebalance_plan(target=target, state=_state([A, C], ["40", "40"], "20"), policy=ok_policy, friction=ok_friction)


def test_plan_has_exactly_five_stored_fields_retains_sources_and_is_frozen() -> None:
    ids = [A, B]
    target, state, policy, friction = _target(ids, ["0.5", "0.5"]), _state(ids, ["30", "30"], "40"), _policy(ids, ["0.05"] * 2, ["0.05"] * 2), _friction(ids)
    plan = build_band_aware_rebalance_plan(target=target, state=state, policy=policy, friction=friction)
    assert [f.name for f in dataclasses.fields(BandAwareRebalancePlan)] == ["target", "state", "policy", "friction", "trades"]
    assert plan.target is target and plan.state is state and plan.policy is policy and plan.friction is friction
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.trades = ()  # type: ignore[misc]


# --- trigger -----------------------------------------------------------------------------------------------------

def test_inside_the_trigger_band_there_is_no_trade_and_cash_is_retained() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["47", "48"], "5", ["0.05"] * 2, ["0.05"] * 2)
    assert plan.is_triggered is False and plan.trades == ()
    assert plan.post_trade_values == (D("47"), D("48")) and plan.post_trade_cash == 5  # cash is NOT deployed merely because it exists
    assert plan.minimum_gross_sale_notional == 0 and plan.gross_buy_notional == 0 and plan.gross_sell_notional == 0
    assert plan.estimated_total_friction == 0
    _assert_invariants(plan)


def test_exact_boundary_is_inside_and_one_exact_unit_beyond_triggers_with_no_tolerance() -> None:
    kwargs = dict(weights=["0.5", "0.5"], trigger=["0.05"] * 2, destination=["0.05"] * 2)
    lower = _plan([A, B], values=["45", "50"], cash="5", **kwargs)           # |e_A| = 5 = H: inside
    upper = _plan([A, B], values=["55", "45"], cash="0", **kwargs)           # |e| = 5 = H on both: inside
    assert lower.is_triggered is False and upper.is_triggered is False and lower.trades == upper.trades == ()
    beyond_lower = _plan([A, B], values=["44.99999999999999999999999999999999999999999999999999", "50"],
                         cash="5.00000000000000000000000000000000000000000000000001", **kwargs)   # W stays exactly 100
    beyond_upper = _plan([A, B], values=["55.00000000000000000000000000000000000000000000000001",
                                         "44.99999999999999999999999999999999999999999999999999"], cash="0", **kwargs)
    assert beyond_lower.total_wealth == 100 and beyond_upper.total_wealth == 100
    assert beyond_lower.is_triggered is True and beyond_upper.is_triggered is True
    _assert_invariants(beyond_lower)
    _assert_invariants(beyond_upper)


def test_cash_is_part_of_the_trigger_wealth_so_uninvested_cash_can_trigger() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["40", "40"], "20", ["0.05"] * 2, ["0.05"] * 2)   # W = 100, e = -10 each, no cash asset invented
    assert plan.is_triggered is True and plan.total_wealth == 100
    assert plan.trigger_lower_values == (D("45"), D("45")) and plan.trigger_upper_values == (D("55"), D("55"))


def test_trigger_uses_amount_space_not_rounded_weights() -> None:
    third = "0.3333333333333333333333333333333333333333333333333"
    ids = [A, B, C]
    weights = [third, third, "0.3333333333333333333333333333333333333333333333334"]
    plan = _plan(ids, weights, ["33.33333333333333333333333333333333333333333333333", "33.33333333333333333333333333333333333333333333333",
                                "33.33333333333333333333333333333333333333333333334"], "0", ["0.01"] * 3, ["0"] * 3)
    assert plan.is_triggered is False and plan.trades == ()  # exactly on target: |e| = 0 is not > H


# --- destination bounds / hand fixtures --------------------------------------------------------------------------

def test_cash_alone_repairs_the_destination_so_no_sale_is_needed() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["30", "30"], "40", ["0.05"] * 2, ["0.05"] * 2, buy=["0.02", "0.01"])
    assert plan.destination_lower_values == (D("45"), D("45")) and plan.destination_upper_values == (D("55"), D("55"))
    assert (plan.mandatory_sell_notional, plan.mandatory_buy_notional) == (0, 30)     # S0 = 0, B0 = 30, C = 40
    assert plan.minimum_gross_sale_notional == 0                                      # S_min = max(0, 30 - 40, 0) = 0
    assert _summary(plan) == [(A, STAGE.CASH_FUNDED_BUY, D("15")), (B, STAGE.CASH_FUNDED_BUY, D("25"))]   # extra buy 10 -> cheaper B
    assert plan.post_trade_values == (D("45"), D("55"))
    assert all(t.stage is not STAGE.SELL for t in plan.trades)
    assert plan.estimated_buy_friction == D("0.55") and plan.estimated_sell_friction == 0 and plan.estimated_total_friction == D("0.55")
    _assert_invariants(plan)


def test_mandatory_overweight_sale_is_smaller_than_the_full_target_sale() -> None:
    args = (["0.5", "0.5"], ["70", "20"], "10")
    plan = _plan([A, B], *args, ["0.05"] * 2, ["0.05"] * 2)
    assert (plan.mandatory_sell_notional, plan.mandatory_buy_notional) == (15, 25)    # S0 = 15, B0 = 25, C = 10
    assert plan.minimum_gross_sale_notional == 15                                     # max(15, 25 - 10, 0)
    assert _summary(plan) == [(B, STAGE.CASH_FUNDED_BUY, D("10")), (A, STAGE.SELL, D("15")), (B, STAGE.SALE_FUNDED_BUY, D("15"))]
    reference = build_cash_first_rebalance_plan(target=_target([A, B], args[0]), state=_state([A, B], args[1], args[2]))
    assert reference.gross_sell_notional == 20 > plan.gross_sell_notional             # Phase 21A would sell back to the exact target
    _assert_invariants(plan)


def test_extra_sell_is_required_and_the_lower_friction_seller_is_consumed_first_up_to_capacity() -> None:
    ids = [A, B, C]  # A = underweight (exact destination), B and C overweight
    args = dict(weights=["0.5", "0.25", "0.25"], values=["10", "45", "45"], cash="0", trigger=["0.05", "0.1", "0.1"], destination=["0", "0.06", "0.1"])
    plan = _plan(ids, buy=["0.03", "0", "0"], sell=["0", "0.01", "0.02"], **args)
    assert (plan.mandatory_sell_notional, plan.mandatory_buy_notional) == (24, 40)    # S0 = 14 + 10, B0 = 40, C = 0
    assert plan.minimum_gross_sale_notional == 40                                     # S_min = max(24, 40, 0): extra sell 16, extra buy 0
    assert _summary(plan) == [(B, STAGE.SELL, D("26")), (C, STAGE.SELL, D("14")), (A, STAGE.SALE_FUNDED_BUY, D("40"))]
    assert plan.post_trade_values == (D("50"), D("19"), D("31"))  # B (cheaper) filled to its lower bound 19 (capacity 12), C took the rest
    assert plan.estimated_buy_friction == D("1.20") and plan.estimated_sell_friction == D("0.54") and plan.estimated_total_friction == D("1.74")
    _assert_invariants(plan)
    swapped = _plan(ids, buy=["0.03", "0", "0"], sell=["0", "0.02", "0.01"], **args)  # now C is cheaper to sell
    assert _summary(swapped) == [(B, STAGE.SELL, D("14")), (C, STAGE.SELL, D("26")), (A, STAGE.SALE_FUNDED_BUY, D("40"))]
    assert swapped.post_trade_values == (D("50"), D("31"), D("19")) or swapped.post_trade_values[2] == D("19")
    _assert_invariants(swapped)


def test_extra_buy_is_required_and_the_lowest_buy_friction_receives_the_optional_notional_first() -> None:
    ids = [A, B, C, E]
    args = dict(weights=["0.4", "0.2", "0.2", "0.2"], values=["70", "10", "10", "10"], cash="0", trigger=["0.05"] * 4, destination=["0.05", "0.05", "0.03", "0.05"])
    plan = _plan(ids, buy=["0.09", "0.03", "0.01", "0.02"], sell=["0.005", "0", "0", "0"], **args)
    assert (plan.mandatory_sell_notional, plan.mandatory_buy_notional) == (25, 17)    # S0 = 25, B0 = 5 + 7 + 5, C = 0
    assert plan.minimum_gross_sale_notional == 25                                     # extra buy 8, extra sell 0
    assert _summary(plan) == [(A, STAGE.SELL, D("25")), (B, STAGE.SALE_FUNDED_BUY, D("5")),
                              (C, STAGE.SALE_FUNDED_BUY, D("13")), (E, STAGE.SALE_FUNDED_BUY, D("7"))]
    assert plan.post_trade_values == (D("45"), D("15"), D("23"), D("17"))             # C filled to capacity 6 first, then E for 2
    assert plan.estimated_buy_friction == D("0.42") and plan.estimated_sell_friction == D("0.125") and plan.estimated_total_friction == D("0.545")
    _assert_invariants(plan)


def test_equal_friction_tie_is_broken_by_canonical_uuid_only() -> None:
    ids = [A, B, C, E]
    plan = _plan(ids, ["0.4", "0.2", "0.2", "0.2"], ["70", "10", "10", "10"], "0", ["0.05"] * 4, ["0.05", "0.05", "0.03", "0.05"],
                 buy=["0.09", "0.02", "0.01", "0.02"])  # B and E tie at 0.02 once C (0.01) is full
    assert _summary(plan)[1:] == [(B, STAGE.SALE_FUNDED_BUY, D("7")), (C, STAGE.SALE_FUNDED_BUY, D("13")), (E, STAGE.SALE_FUNDED_BUY, D("5"))]
    assert plan.post_trade_values == (D("45"), D("17"), D("23"), D("15"))  # the lower UUID (B) takes the tied optional 2, E none
    _assert_invariants(plan)


def test_trigger_equal_to_destination_moves_only_to_the_admissible_boundary() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["70", "30"], "0", ["0.05"] * 2, ["0.05"] * 2)
    assert _summary(plan) == [(A, STAGE.SELL, D("15")), (B, STAGE.SALE_FUNDED_BUY, D("15"))]
    assert plan.post_trade_values == (D("55"), D("45"))  # on the boundary, not at the 50 / 50 target
    _assert_invariants(plan)


def test_target_zero_asset_with_a_positive_destination_band_keeps_value_up_to_the_band() -> None:
    plan = _plan([A, B], ["0", "1"], ["30", "70"], "0", ["0.1"] * 2, ["0.05"] * 2)
    assert plan.destination_lower_values == (D("0"), D("95")) and plan.destination_upper_values == (D("5"), D("105"))  # upper bounds are not clipped to W
    assert _summary(plan) == [(A, STAGE.SELL, D("25")), (B, STAGE.SALE_FUNDED_BUY, D("25"))]
    assert plan.post_trade_values == (D("5"), D("95"))  # not liquidated to zero
    zero_upper = _plan([A, B], ["0", "1"], ["30", "70"], "0", ["0.1"] * 2, ["0", "0.05"])
    assert zero_upper.post_trade_values == (D("0"), D("100"))  # liquidation only when the destination upper bound is zero
    _assert_invariants(plan)
    _assert_invariants(zero_upper)


def test_destination_zero_equals_the_phase_21a_exact_target_plan() -> None:
    cases = [
        (["0.5", "0.5"], ["70", "20"], "10"),
        (["0.5", "0.5"], ["40", "40"], "20"),
        (["0.5", "0.5"], ["80", "20"], "0"),
        (["0.4", "0.2", "0.2", "0.2"], ["70", "10", "10", "10"], "0"),
        (["0.1", "0.2", "0.3", "0.4"], ["60", "20", "50", "55"], "15"),
        (["0.25", "0.25", "0.25", "0.25"], ["70", "10", "90", "30"], "0"),
    ]
    for weights, values, cash in cases:
        ids = [A, B, C, E][: len(weights)]
        band = _plan(ids, weights, values, cash, ["0"] * len(ids), ["0"] * len(ids), buy=["0.01"] * len(ids), sell=["0.02"] * len(ids))
        reference = build_cash_first_rebalance_plan(target=_target(ids, weights), state=_state(ids, values, cash))
        assert band.trades == reference.trades            # same economic deltas AND the same cash-first staging
        assert band.post_trade_values == reference.target_values
        _assert_invariants(band)


def test_no_trigger_and_triggered_cash_behavior() -> None:
    kept = _plan([A, B], ["0.5", "0.5"], ["47", "48"], "5", ["0.05"] * 2, ["0.05"] * 2)
    deployed = _plan([A, B], ["0.5", "0.5"], ["30", "30"], "40", ["0.05"] * 2, ["0.05"] * 2)
    assert kept.post_trade_cash == 5 and deployed.post_trade_cash == 0


# --- lexicographic priority: minimum sale first, friction second --------------------------------------------------

def test_mandatory_repairs_ignore_even_extreme_friction() -> None:
    cheap = _plan([A, B], ["0.5", "0.5"], ["70", "20"], "10", ["0.05"] * 2, ["0.05"] * 2)
    extreme = _plan([A, B], ["0.5", "0.5"], ["70", "20"], "10", ["0.05"] * 2, ["0.05"] * 2, buy=["1000000", "1000000"], sell=["1000000", "1000000"])
    assert extreme.trades == cheap.trades  # friction only selects discretionary allocation inside the minimum-sale set
    assert extreme.estimated_total_friction > 1000000


def test_canonical_plan_is_lexicographically_optimal_over_every_feasible_destination_state() -> None:
    ids = [A, B, C, E]
    weights, values, cash = ["0.4", "0.2", "0.2", "0.2"], ["70", "10", "10", "10"], "0"
    buy, sell = ["0.09", "0.03", "0.01", "0.02"], ["0.005", "0.004", "0.003", "0.002"]
    plan = _plan(ids, weights, values, cash, ["0.05"] * 4, ["0.05", "0.05", "0.03", "0.05"], buy=buy, sell=sell)
    current = [70, 10, 10, 10]
    lowers, uppers = [35, 15, 17, 15], [45, 25, 23, 25]
    canonical = tuple(int(v) for v in plan.post_trade_values)

    def key(final):
        sale = sum(max(c - x, 0) for c, x in zip(current, final))
        cost = sum(F(D(buy[i])) * max(x - c, 0) + F(D(sell[i])) * max(c - x, 0) for i, (c, x) in enumerate(zip(current, final)))
        return (sale, cost)

    feasible = []
    for a, b, c in itertools.product(range(lowers[0], uppers[0] + 1), range(lowers[1], uppers[1] + 1), range(lowers[2], uppers[2] + 1)):
        e = 100 - a - b - c
        if lowers[3] <= e <= uppers[3]:
            feasible.append((a, b, c, e))
    assert canonical in feasible and len(feasible) > 100
    best = min(key(f) for f in feasible)
    assert key(canonical) == best and [f for f in feasible if key(f) == best] == [canonical]   # unique lexicographic optimum
    larger_sale = [f for f in feasible if key(f)[0] > best[0]]
    assert larger_sale  # alternatives with a larger gross sale exist ...
    assert all(key(f)[1] >= best[1] for f in larger_sale)  # ... and none of them is cheaper (min-sale is also min-friction here)
    assert F(plan.estimated_total_friction) == best[1]


def test_friction_never_overrides_the_minimum_sale_priority_when_friction_ties() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["60", "40"], "0", ["0.05"] * 2, ["0.05"] * 2)   # all frictions explicitly zero
    assert _summary(plan) == [(A, STAGE.SELL, D("5")), (B, STAGE.SALE_FUNDED_BUY, D("5"))]  # S_min = 5, not the 10 of the exact target
    assert plan.estimated_total_friction == 0


# --- exact aggregates / diagnostics ------------------------------------------------------------------------------

def test_derived_diagnostics_are_not_stored_and_weight_views_are_diagnostic_only() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["70", "20"], "10", ["0.05"] * 2, ["0.05"] * 2, buy=["0.1", "0.1"], sell=["0.2", "0.2"])
    stored = {f.name for f in dataclasses.fields(BandAwareRebalancePlan)}
    assert not stored & {"is_triggered", "total_wealth", "trigger_lower_values", "trigger_upper_values", "destination_lower_values",
                         "destination_upper_values", "minimum_gross_sale_notional", "gross_buy_notional", "gross_sell_notional",
                         "estimated_buy_friction", "estimated_sell_friction", "estimated_total_friction", "post_trade_values",
                         "post_trade_cash", "current_weight_drifts", "post_trade_weight_drifts"}
    assert plan.current_weight_drifts == (D("0.2"), D("-0.3")) and plan.post_trade_weight_drifts == (D("0.05"), D("-0.05"))
    assert plan.estimated_buy_friction == D("2.5") and plan.estimated_sell_friction == D("3.0") and plan.estimated_total_friction == D("5.5")


def test_friction_is_a_planning_diagnostic_never_deducted_from_notionals_or_wealth() -> None:
    cheap = _plan([A, B], ["0.5", "0.5"], ["70", "20"], "10", ["0.05"] * 2, ["0.05"] * 2)
    costly = _plan([A, B], ["0.5", "0.5"], ["70", "20"], "10", ["0.05"] * 2, ["0.05"] * 2, buy=["0.5", "0.5"], sell=["0.5", "0.5"])
    assert costly.total_wealth == cheap.total_wealth == 100 and costly.post_trade_values == cheap.post_trade_values == (D("55"), D("45"))
    assert [t.notional for t in costly.trades] == [t.notional for t in cheap.trades] and costly.post_trade_cash == 0


def test_exact_arithmetic_beyond_fifty_digits() -> None:
    third = "0.3333333333333333333333333333333333333333333333333"
    ids = [A, B, C]
    weights = [third, third, "0.3333333333333333333333333333333333333333333333334"]
    plan = _plan(ids, weights, ["60.000000000000000000000000000000000000000000000000001", "20", "20"], "0", ["0.01"] * 3, ["0"] * 3,
                 buy=["0.5", "0.25", "0.125"], sell=["0.0625"] * 3)
    assert plan.is_triggered
    assert _exact(plan.post_trade_values) == F(plan.total_wealth) and plan.post_trade_cash == 0
    for lo, v, hi in zip(plan.destination_lower_values, plan.post_trade_values, plan.destination_upper_values):
        assert lo == v == hi  # destination zero: the exact target, represented exactly
    _assert_invariants(plan)


# --- forge resistance --------------------------------------------------------------------------------------------

def _good():
    ids = [A, B, C, E]
    target = _target(ids, ["0.4", "0.2", "0.2", "0.2"])
    state = _state(ids, ["70", "10", "10", "10"], "0")
    policy = _policy(ids, ["0.05"] * 4, ["0.05", "0.05", "0.03", "0.05"])
    friction = _friction(ids, buy=["0.09", "0.03", "0.01", "0.02"], sell=["0.005", "0", "0", "0"])
    return target, state, policy, friction, build_band_aware_rebalance_plan(target=target, state=state, policy=policy, friction=friction)


def _make(target, state, policy, friction, trades) -> BandAwareRebalancePlan:
    return BandAwareRebalancePlan(target=target, state=state, policy=policy, friction=friction, trades=trades)


def test_valid_plan_reconstructs_equal() -> None:
    target, state, policy, friction, good = _good()
    assert _make(target, state, policy, friction, good.trades) == good


def test_forged_plans_are_rejected() -> None:
    target, state, policy, friction, good = _good()
    trades = list(good.trades)

    def swap(index, **changes):
        copy = list(trades)
        copy[index] = dataclasses.replace(copy[index], **changes)
        return tuple(copy)

    for bad in (list(trades), (trades[0], "x")):
        with pytest.raises(TypeError):
            _make(target, state, policy, friction, bad)
    for index, wrong in enumerate((state, target, friction, policy)):
        values = [target, state, policy, friction]
        values[index] = wrong
        with pytest.raises(TypeError):
            _make(*values, tuple(trades))
    forgeries = {
        "missing mandatory repair": tuple(t for t in trades if t.instrument_id != B),
        "sale above S_min": swap(0, notional=trades[0].notional + D(1)),
        "wrong optional buyer": (trades[0], trades[1], trades[2], RebalanceTradeInstruction(E, STAGE.SALE_FUNDED_BUY, D("5")),
                                 RebalanceTradeInstruction(E, STAGE.SALE_FUNDED_BUY, D("2"))),
        "higher-friction alternative": (trades[0], RebalanceTradeInstruction(B, STAGE.SALE_FUNDED_BUY, D("7")), trades[2], RebalanceTradeInstruction(E, STAGE.SALE_FUNDED_BUY, D("5")))[:0]
                                       or (trades[0], RebalanceTradeInstruction(B, STAGE.SALE_FUNDED_BUY, D("7")), RebalanceTradeInstruction(C, STAGE.SALE_FUNDED_BUY, D("11")),
                                           RebalanceTradeInstruction(E, STAGE.SALE_FUNDED_BUY, D("7"))),
        "wrong stage": swap(1, stage=STAGE.CASH_FUNDED_BUY),
        "wrong ordering": tuple(reversed(trades)),
        "round trip": tuple(trades) + (RebalanceTradeInstruction(A, STAGE.SALE_FUNDED_BUY, D(1)),),
        "wrong notional": swap(2, notional=trades[2].notional + D("0.01")),
        "empty": (),
    }
    for name, forged in forgeries.items():
        with pytest.raises(ValueError, match=ERR_MATCH):
            _make(target, state, policy, friction, forged)


def test_a_trade_when_not_triggered_is_rejected() -> None:
    ids = [A, B]
    target, state, policy, friction = _target(ids, ["0.5", "0.5"]), _state(ids, ["47", "48"], "5"), _policy(ids, ["0.05"] * 2, ["0.05"] * 2), _friction(ids)
    with pytest.raises(ValueError, match=ERR_MATCH):
        _make(target, state, policy, friction, (RebalanceTradeInstruction(A, STAGE.CASH_FUNDED_BUY, D("3")),))
    assert _make(target, state, policy, friction, ()).trades == ()


def test_a_forged_plan_with_another_friction_profile_choice_is_rejected() -> None:
    target, state, policy, friction, good = _good()
    other = _friction([A, B, C, E], buy=["0.09", "0.01", "0.03", "0.02"], sell=["0.005", "0", "0", "0"])
    alternative = build_band_aware_rebalance_plan(target=target, state=state, policy=policy, friction=other)
    assert alternative.trades != good.trades          # a different friction profile gives a different canonical plan
    with pytest.raises(ValueError, match=ERR_MATCH):
        _make(target, state, policy, friction, alternative.trades)


def test_universe_mismatch_is_rejected_by_the_plan_constructor() -> None:
    target, state, policy, friction, good = _good()
    with pytest.raises(ValueError, match=ERR_UNIVERSE):
        _make(target, state, _policy([A, B], ["0.05"] * 2, ["0.05"] * 2), friction, good.trades)


# --- input order / Decimal isolation / range ---------------------------------------------------------------------

def test_canonical_upstream_sorting_gives_identical_plans() -> None:
    inputs = {A: ("0.4", "70", "0.05", "0.05", "0.09", "0.005"), B: ("0.2", "10", "0.05", "0.05", "0.03", "0"),
              C: ("0.2", "10", "0.05", "0.03", "0.01", "0"), E: ("0.2", "10", "0.05", "0.05", "0.02", "0")}
    plans = []
    for order in ([A, B, C, E], list(reversed([A, B, C, E]))):
        ids = sorted(order, key=str)
        plans.append(_plan(ids, [inputs[i][0] for i in ids], [inputs[i][1] for i in ids], "0", [inputs[i][2] for i in ids],
                           [inputs[i][3] for i in ids], buy=[inputs[i][4] for i in ids], sell=[inputs[i][5] for i in ids]))
    assert plans[0] == plans[1]


def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_changes_nothing_and_is_not_mutated() -> None:
    def build():
        return _plan([A, B, C, E], ["0.4", "0.2", "0.2", "0.2"], ["70.123456789", "10", "10", "9.876543211"], "0",
                     ["0.05"] * 4, ["0.05", "0.05", "0.03", "0.05"], buy=["0.09", "0.03", "0.01", "0.02"], sell=["0.005", "0", "0", "0"])

    baseline = build()
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            plan = build()
            derived = (plan.is_triggered, plan.minimum_gross_sale_notional, plan.post_trade_values, plan.post_trade_cash,
                       plan.estimated_buy_friction, plan.estimated_sell_friction, plan.estimated_total_friction,
                       plan.current_weight_drifts, plan.post_trade_weight_drifts, plan.trigger_lower_values, plan.destination_upper_values)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert plan == baseline
        assert [(t.stage, t.instrument_id, t.notional.as_tuple()) for t in plan.trades] == [(t.stage, t.instrument_id, t.notional.as_tuple()) for t in baseline.trades]
        assert derived == (baseline.is_triggered, baseline.minimum_gross_sale_notional, baseline.post_trade_values, baseline.post_trade_cash,
                           baseline.estimated_buy_friction, baseline.estimated_sell_friction, baseline.estimated_total_friction,
                           baseline.current_weight_drifts, baseline.post_trade_weight_drifts, baseline.trigger_lower_values,
                           baseline.destination_upper_values)
        _assert_invariants(plan)


def test_repeated_builds_are_identical() -> None:
    _, _, _, _, first = _good()
    for _ in range(3):
        assert _good()[4] == first


def test_extreme_amounts_fail_closed_with_the_static_range_error() -> None:
    with pytest.raises(ValueError, match=ERR_RANGE):
        _plan([A, B], ["0.5", "0.5"], ["1E+999999999", "1E-999999999"], "0", ["0.05"] * 2, ["0.05"] * 2)


def test_internal_inconsistency_is_a_static_error(monkeypatch) -> None:
    ids = [A, B]
    target, state, policy, friction = _target(ids, ["0.5", "0.5"]), _state(ids, ["30", "30"], "40"), _policy(ids, ["0.05"] * 2, ["0.05"] * 2), _friction(ids)
    original = module_under_test._allocate

    def short(total, capacities, rates):
        result = original(total, capacities, rates)
        result[0] = result[0] + D(1)
        return result

    monkeypatch.setattr(module_under_test, "_allocate", short)
    with pytest.raises(ValueError, match=ERR_INCONSISTENT):
        build_band_aware_rebalance_plan(target=target, state=state, policy=policy, friction=friction)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_rebalance_policy.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST and "backend/engine/private/allocation_rebalance.py" in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_plus_phase_21a_only() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {"__future__", "dataclasses", "decimal", "uuid", "backend.engine.private.allocation_rebalance"}
    for forbidden in ("numpy", "scipy", "pandas", "math", "random", "time", "datetime", "os", "fractions", "allocation_cvar", "allocation_hrp",
                      "allocation_risk_parity", "allocation_user_view", "portfolio", "repository", "supabase", "fee_tax"):
        assert not any(forbidden in name for name in plain), forbidden


def test_no_ambient_context_float_solver_or_second_precision_regime() -> None:
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "float", "Fraction", "linprog"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]
    owners = [f.name for f in ast.walk(_TREE) if isinstance(f, ast.FunctionDef)
              for c in ast.walk(f) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "Context"]
    assert owners == []  # the Phase 21A analytics context is reused for the diagnostic drift views; no second context here


_FORBIDDEN_FRAGMENTS = (
    "tax", "capital_gain", "withholding", "cost_basis", "lot", "fifo", "lifo", "hifo", "ledger", "portfolio_transaction", "repository",
    "optimiz", "linprog", "scipy", "numpy", "highs", "solver", "quantity", "price", "order_type", "equal_weight", "inverse_vol",
    "risk_parity", "hrp", "cvar", "posterior", "persist", "supabase", "settle", "deduct",
)


def test_no_tax_lot_solver_allocation_or_settlement_surface() -> None:
    identifiers: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for identifier in identifiers:
        lowered = identifier.lower()
        for fragment in _FORBIDDEN_FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    builder = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_band_aware_rebalance_plan")
    assert [a.arg for a in builder.args.kwonlyargs] == ["target", "state", "policy", "friction"] and not builder.args.args


def test_documents_the_policy_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("trigger band", "destination band", "no-trade", "S_min", "minimum", "friction", "greedy", "no tax", "context-free",
                   "planning diagnostic", "not globally optimal"):
        assert needle in doc, needle
