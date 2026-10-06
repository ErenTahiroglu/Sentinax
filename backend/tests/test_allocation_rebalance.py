"""
backend/tests/test_allocation_rebalance.py
==========================================
Phase 21A: exact, frictionless, cash-first rebalance reference plan.

Explicit target weights + explicit marked current values + explicit investable cash (one currency, same instrument universe)
-> CASH_FUNDED_BUY, then the necessary SELLs, then SALE_FUNDED_BUY, reaching the exact target. Notional only: no tax, fee,
lot, cost basis, price, quantity, order or ledger event. All money identities use context-free exact arithmetic.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_rebalance as module_under_test
from backend.engine.private.allocation_rebalance import (
    CashFirstRebalancePlan,
    RebalanceCurrentState,
    RebalanceTargetAllocation,
    RebalanceTradeInstruction,
    RebalanceTradeStage,
    build_cash_first_rebalance_plan,
)
from backend.engine.private.domain import Currency
from backend.tests.invariants import static_guards as sg

D = Decimal
F = Fraction
STAGE = RebalanceTradeStage
A, B, C, E = (UUID(int=i) for i in (1, 2, 3, 4))  # canonical (ascending str) order
ERR_UNIVERSE = "rebalance target and current state must use the same canonical instrument universe"
ERR_WEALTH = "rebalance total wealth must be strictly positive"
ERR_FUNDING = "rebalance funding identity is inconsistent"
ERR_MATCH = "rebalance plan must match the canonical cash-first plan exactly"
ERR_RANGE = "rebalance analytics exceeds supported Decimal range"


def _target(ids, weights) -> RebalanceTargetAllocation:
    return RebalanceTargetAllocation(instrument_ids=tuple(ids), weights=tuple(D(w) for w in weights))


def _state(ids, values, cash="0", currency=Currency.TRY) -> RebalanceCurrentState:
    return RebalanceCurrentState(instrument_ids=tuple(ids), current_values=tuple(D(v) for v in values),
                                 investable_cash=D(cash), currency=currency)


def _plan(ids, weights, values, cash="0") -> CashFirstRebalancePlan:
    return build_cash_first_rebalance_plan(target=_target(ids, weights), state=_state(ids, values, cash))


def _summary(plan):
    return [(t.instrument_id, t.stage, t.notional) for t in plan.trades]


def _assert_reaches_target(plan: CashFirstRebalancePlan) -> None:
    assert plan.post_trade_values == plan.target_values and plan.post_trade_cash == 0
    def exact(values) -> Fraction:  # Fractions: exact, independent of any Decimal context
        return sum((F(v) for v in values), F(0))

    assert F(plan.gross_buy_notional) == F(plan.state.investable_cash) + F(plan.gross_sell_notional)
    assert F(plan.cash_funded_buy_notional) + F(plan.sale_funded_buy_notional) == F(plan.gross_buy_notional)
    assert F(plan.cash_funded_buy_notional) <= F(plan.state.investable_cash)
    assert exact(plan.target_values) == F(plan.total_wealth)


# --- target contract ---------------------------------------------------------------------------------------------

def test_target_has_exactly_two_stored_fields_and_is_frozen() -> None:
    target = _target([A, B], ["0.5", "0.5"])
    assert [f.name for f in dataclasses.fields(RebalanceTargetAllocation)] == ["instrument_ids", "weights"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.weights = ()  # type: ignore[misc]


def test_target_instrument_ids_contract() -> None:
    class _UuidSub(UUID):
        pass

    for bad in ([A, B], (), "ab", None, (A, str(B)), (A, 2), (A, _UuidSub(int=2))):
        with pytest.raises((TypeError, ValueError)):
            RebalanceTargetAllocation(instrument_ids=bad, weights=(D("0.5"), D("0.5")))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _target([A, A], ["0.5", "0.5"])           # duplicates
    with pytest.raises(ValueError):
        _target([B, A], ["0.5", "0.5"])           # not canonical order: the constructor validates, never sorts
    assert _target([A, B], ["0.5", "0.5"]).instrument_ids == (A, B)
    assert _target([A], ["1"]).weights == (D(1),)


def test_target_weights_contract() -> None:
    class _DecSub(Decimal):
        pass

    for bad in ([D("0.5"), D("0.5")], (0.5, 0.5), (1, 0), (True, False), ("0.5", "0.5"), (_DecSub("0.5"), D("0.5")), None):
        with pytest.raises(TypeError):
            RebalanceTargetAllocation(instrument_ids=(A, B), weights=bad)  # type: ignore[arg-type]
    for bad in (("NaN", "1"), ("Infinity", "-Infinity"), ("-0.1", "1.1"), ("1.1", "-0.1"), ("-0", "1")):
        with pytest.raises(ValueError):
            _target([A, B], bad)
    for bad in ((D("0.5"),), (D("0.5"), D("0.25"), D("0.25"))):
        with pytest.raises(ValueError):
            RebalanceTargetAllocation(instrument_ids=(A, B), weights=bad)
    assert _target([A, B], ["0", "1"]).weights == (D(0), D(1))  # a zero target weight is allowed


def test_target_weights_must_sum_to_exactly_one_without_tolerance_or_normalization() -> None:
    for bad in (("0.5", "0.4999999999999999999999999999999999999999999999999"), ("0.5", "0.5000000000000000000000000000000000000000000000001"),
                ("1", "1"), ("0.4", "0.4")):
        with pytest.raises(ValueError):
            _target([A, B], bad)
    third = "0.3333333333333333333333333333333333333333333333333"
    with decimal.localcontext(decimal.Context(prec=2)):
        assert _target([A, B, C], [third, third, "0.3333333333333333333333333333333333333333333333334"]) is not None  # exact, not ambient


# --- current state contract --------------------------------------------------------------------------------------

def test_state_has_exactly_four_stored_fields_and_is_frozen() -> None:
    state = _state([A, B], ["40", "40"], "20")
    assert [f.name for f in dataclasses.fields(RebalanceCurrentState)] == ["instrument_ids", "current_values", "investable_cash", "currency"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.investable_cash = D(0)  # type: ignore[misc]


def test_state_value_cash_and_currency_contract() -> None:
    class _DecSub(Decimal):
        pass

    ids = (A, B)
    for bad in ([D(1), D(1)], (1, 1), (0.5, 0.5), ("1", "1"), (_DecSub(1), D(1)), None):
        with pytest.raises(TypeError):
            RebalanceCurrentState(instrument_ids=ids, current_values=bad, investable_cash=D(0), currency=Currency.TRY)  # type: ignore[arg-type]
    for bad in ((D("-1"), D(1)), (D("NaN"), D(1)), (D("Infinity"), D(1)), (D("-0"), D(1))):
        with pytest.raises(ValueError):
            RebalanceCurrentState(instrument_ids=ids, current_values=bad, investable_cash=D(0), currency=Currency.TRY)
    with pytest.raises(ValueError):
        RebalanceCurrentState(instrument_ids=ids, current_values=(D(1),), investable_cash=D(0), currency=Currency.TRY)
    for bad in (1, 0.5, "1", True, None, _DecSub(1)):
        with pytest.raises(TypeError):
            RebalanceCurrentState(instrument_ids=ids, current_values=(D(1), D(1)), investable_cash=bad, currency=Currency.TRY)  # type: ignore[arg-type]
    for bad in ("-1", "NaN", "Infinity", "-0"):
        with pytest.raises(ValueError):
            RebalanceCurrentState(instrument_ids=ids, current_values=(D(1), D(1)), investable_cash=D(bad), currency=Currency.TRY)
    for bad in ("TRY", None, 1, object()):
        with pytest.raises(TypeError):
            RebalanceCurrentState(instrument_ids=ids, current_values=(D(1), D(1)), investable_cash=D(0), currency=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _state([B, A], ["1", "1"])  # canonical order required
    with pytest.raises(ValueError):
        _state([A, A], ["1", "1"])
    assert _state([A, B], ["0", "0"], "5", Currency.USD).currency is Currency.USD


def test_investable_cash_is_an_explicit_input_not_inferred() -> None:
    assert "investable_cash" in {f.name for f in dataclasses.fields(RebalanceCurrentState)}
    plan = _plan([A, B], ["0.5", "0.5"], ["40", "40"], "20")
    assert plan.total_wealth == 100  # only the supplied investable cash is part of the rebalance wealth


# --- Phase 26D4A: an EMPTY current universe represents cash-only; the target stays non-empty ------------------------

@pytest.mark.parametrize("cash", ["0", "100", "0.000000000000000000000000001"])
def test_empty_current_state_with_zero_or_positive_cash_is_accepted(cash) -> None:
    state = RebalanceCurrentState(instrument_ids=(), current_values=(), investable_cash=D(cash), currency=Currency.TRY)
    assert state.instrument_ids == () and state.current_values == () and state.investable_cash == D(cash) and state.currency is Currency.TRY
    assert _state([], [], cash, Currency.USD).currency is Currency.USD


def test_empty_ids_with_values_and_nonempty_ids_with_mismatched_values_are_rejected_as_before() -> None:
    with pytest.raises(ValueError):
        RebalanceCurrentState(instrument_ids=(), current_values=(D(1),), investable_cash=D(0), currency=Currency.TRY)
    with pytest.raises(ValueError):
        RebalanceCurrentState(instrument_ids=(A, B), current_values=(), investable_cash=D(0), currency=Currency.TRY)
    with pytest.raises(ValueError):
        RebalanceCurrentState(instrument_ids=(A,), current_values=(D(1), D(1)), investable_cash=D(0), currency=Currency.TRY)
    for bad in (None, [], [A], "ab", (A, str(B)), (A, 2), (True,)):
        with pytest.raises(TypeError):
            RebalanceCurrentState(instrument_ids=bad, current_values=(), investable_cash=D(0), currency=Currency.TRY)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RebalanceCurrentState(instrument_ids=(), current_values=[], investable_cash=D(0), currency=Currency.TRY)  # type: ignore[arg-type]
    for bad_cash in (D("-1"), D("NaN"), D("-0")):
        with pytest.raises(ValueError):
            RebalanceCurrentState(instrument_ids=(), current_values=(), investable_cash=bad_cash, currency=Currency.TRY)
    with pytest.raises(TypeError):
        RebalanceCurrentState(instrument_ids=(), current_values=(), investable_cash=5, currency=Currency.TRY)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RebalanceCurrentState(instrument_ids=(), current_values=(), investable_cash=D(5), currency="TRY")  # type: ignore[arg-type]


def test_nonempty_current_state_ordering_and_uniqueness_invariants_are_unchanged() -> None:
    with pytest.raises(ValueError):
        _state([B, A], ["1", "1"])
    with pytest.raises(ValueError):
        _state([A, A], ["1", "1"])
    assert _state([A, B], ["1", "2"], "3").instrument_ids == (A, B)


def test_the_target_allocation_universe_must_remain_non_empty() -> None:
    for weights in ((), (D(1),)):
        with pytest.raises((TypeError, ValueError)):
            RebalanceTargetAllocation(instrument_ids=(), weights=weights)
    assert "non-empty" in module_under_test._ERR_IDS_TYPE                                                    # the shared validator keeps its non-empty contract


def test_cash_first_builder_never_repairs_an_empty_current_universe_against_a_target() -> None:
    target = _target([A, B], ["0.5", "0.5"])
    for cash in ("0", "100"):
        with pytest.raises(ValueError, match=ERR_UNIVERSE):
            build_cash_first_rebalance_plan(target=target, state=_state([], [], cash))
    with pytest.raises(ValueError, match=ERR_UNIVERSE):
        build_cash_first_rebalance_plan(target=_target([A], ["1"]), state=_state([], [], "50"))


# --- builder / same-universe / wealth ----------------------------------------------------------------------------

def test_builder_is_keyword_only_with_exact_types_and_no_policy_parameters() -> None:
    target, state = _target([A, B], ["0.5", "0.5"]), _state([A, B], ["40", "40"], "20")
    with pytest.raises(TypeError):
        build_cash_first_rebalance_plan(target, state)  # type: ignore[misc]
    for extra in ("band", "fee", "tax", "threshold"):
        with pytest.raises(TypeError):
            build_cash_first_rebalance_plan(target=target, state=state, **{extra: D(1)})  # type: ignore[arg-type]
    for bad in (None, state, object()):
        with pytest.raises(TypeError):
            build_cash_first_rebalance_plan(target=bad, state=state)  # type: ignore[arg-type]
    for bad in (None, target, object()):
        with pytest.raises(TypeError):
            build_cash_first_rebalance_plan(target=target, state=bad)  # type: ignore[arg-type]


def test_same_canonical_instrument_universe_is_required() -> None:
    target = _target([A, B], ["0.5", "0.5"])
    for state in (_state([A, C], ["1", "1"]), _state([A, B, C], ["1", "1", "1"]), _state([A], ["1"])):
        with pytest.raises(ValueError, match=ERR_UNIVERSE):
            build_cash_first_rebalance_plan(target=target, state=state)  # no union, no sell-to-zero, no inferred zero position


def test_zero_total_wealth_is_rejected() -> None:
    with pytest.raises(ValueError, match=ERR_WEALTH):
        _plan([A, B], ["0.5", "0.5"], ["0", "0"], "0")


def test_plan_has_exactly_three_stored_fields_retains_sources_and_is_frozen() -> None:
    target, state = _target([A, B], ["0.5", "0.5"]), _state([A, B], ["40", "40"], "20")
    plan = build_cash_first_rebalance_plan(target=target, state=state)
    assert [f.name for f in dataclasses.fields(CashFirstRebalancePlan)] == ["target", "state", "trades"]
    assert plan.target is target and plan.state is state
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.trades = ()  # type: ignore[misc]


# --- stage / instruction contracts -------------------------------------------------------------------------------

def test_stage_enum_is_exactly_the_three_cash_first_stages() -> None:
    assert issubclass(RebalanceTradeStage, Enum)
    assert {m.name: m.value for m in RebalanceTradeStage} == {
        "CASH_FUNDED_BUY": "cash_funded_buy", "SELL": "sell", "SALE_FUNDED_BUY": "sale_funded_buy"}


def test_instruction_contract() -> None:
    class _DecSub(Decimal):
        pass

    assert [f.name for f in dataclasses.fields(RebalanceTradeInstruction)] == ["instrument_id", "stage", "notional"]
    good = RebalanceTradeInstruction(instrument_id=A, stage=STAGE.SELL, notional=D("1.5"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        good.notional = D(1)  # type: ignore[misc]
    for bad in ("x", 1, None):
        with pytest.raises(TypeError):
            RebalanceTradeInstruction(instrument_id=bad, stage=STAGE.SELL, notional=D(1))  # type: ignore[arg-type]
    for bad in ("sell", None, 1):
        with pytest.raises(TypeError):
            RebalanceTradeInstruction(instrument_id=A, stage=bad, notional=D(1))  # type: ignore[arg-type]
    for bad in (1, 1.5, "1", True, None, _DecSub(1)):
        with pytest.raises(TypeError):
            RebalanceTradeInstruction(instrument_id=A, stage=STAGE.SELL, notional=bad)  # type: ignore[arg-type]
    for bad in ("0", "-0", "-1", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            RebalanceTradeInstruction(instrument_id=A, stage=STAGE.SELL, notional=D(bad))  # no zero or negative instruction


# --- hand fixtures -----------------------------------------------------------------------------------------------

def test_cash_only_fixture() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["40", "40"], "20")
    assert _summary(plan) == [(A, STAGE.CASH_FUNDED_BUY, D("10")), (B, STAGE.CASH_FUNDED_BUY, D("10"))]
    assert plan.gross_sell_notional == 0 and plan.sale_funded_buy_notional == 0  # no SELL, no SALE_FUNDED_BUY
    _assert_reaches_target(plan)


def test_cash_then_sell_fixture() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["80", "10"], "10")
    assert _summary(plan) == [(B, STAGE.CASH_FUNDED_BUY, D("10")), (A, STAGE.SELL, D("30")), (B, STAGE.SALE_FUNDED_BUY, D("30"))]
    assert plan.trade_deltas == (D("-30"), D("40")) and plan.target_values == (D("50"), D("50"))
    assert plan.gross_buy_notional == 40 and plan.gross_sell_notional == 30
    _assert_reaches_target(plan)


def test_multi_asset_waterfall_with_cash_covering_the_first_buys_only() -> None:
    plan = _plan([A, B, C, E], ["0.10", "0.20", "0.30", "0.40"], ["60", "20", "50", "40"], "30")
    assert plan.target_values == (D("20"), D("40"), D("60"), D("80")) and plan.trade_deltas == (D("-40"), D("20"), D("10"), D("40"))
    assert _summary(plan) == [
        (B, STAGE.CASH_FUNDED_BUY, D("20")), (C, STAGE.CASH_FUNDED_BUY, D("10")),   # canonical order: 30 of cash is fully allocated
        (A, STAGE.SELL, D("40")),
        (E, STAGE.SALE_FUNDED_BUY, D("40")),
    ]
    _assert_reaches_target(plan)


def test_multi_asset_waterfall_with_cash_smaller_than_the_first_buy() -> None:
    plan = _plan([A, B, C, E], ["0.10", "0.20", "0.30", "0.40"], ["60", "20", "50", "55"], "15")
    assert plan.trade_deltas == (D("-40"), D("20"), D("10"), D("25"))
    assert _summary(plan) == [
        (B, STAGE.CASH_FUNDED_BUY, D("15")),                                           # partial: cash exhausted inside the first buy
        (A, STAGE.SELL, D("40")),
        (B, STAGE.SALE_FUNDED_BUY, D("5")), (C, STAGE.SALE_FUNDED_BUY, D("10")), (E, STAGE.SALE_FUNDED_BUY, D("25")),
    ]
    assert plan.cash_funded_buy_notional == 15 and plan.sale_funded_buy_notional == 40 == plan.gross_sell_notional
    _assert_reaches_target(plan)


def test_multiple_overweights_and_underweights() -> None:
    plan = _plan([A, B, C, E], ["0.25", "0.25", "0.25", "0.25"], ["70", "10", "90", "30"], "0")
    assert plan.trade_deltas == (D("-20"), D("40"), D("-40"), D("20"))
    assert _summary(plan) == [(A, STAGE.SELL, D("20")), (C, STAGE.SELL, D("40")),
                              (B, STAGE.SALE_FUNDED_BUY, D("40")), (E, STAGE.SALE_FUNDED_BUY, D("20"))]
    _assert_reaches_target(plan)


def test_zero_target_weight_sells_the_exact_current_value() -> None:
    plan = _plan([A, B], ["0", "1"], ["30", "70"], "0")
    assert _summary(plan) == [(A, STAGE.SELL, D("30")), (B, STAGE.SALE_FUNDED_BUY, D("30"))]
    assert plan.post_trade_values == (D(0), D(100))
    _assert_reaches_target(plan)


def test_zero_current_value_generates_a_buy_need() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["100", "0"], "0")
    assert _summary(plan) == [(A, STAGE.SELL, D("50")), (B, STAGE.SALE_FUNDED_BUY, D("50"))]
    cash_plan = _plan([A, B], ["0.5", "0.5"], ["0", "0"], "100")
    assert _summary(cash_plan) == [(A, STAGE.CASH_FUNDED_BUY, D("50")), (B, STAGE.CASH_FUNDED_BUY, D("50"))]
    _assert_reaches_target(plan)
    _assert_reaches_target(cash_plan)


def test_zero_cash_has_no_cash_funded_stage_and_sells_come_first() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["80", "20"], "0")
    stages = [t.stage for t in plan.trades]
    assert STAGE.CASH_FUNDED_BUY not in stages and stages == [STAGE.SELL, STAGE.SALE_FUNDED_BUY]


def test_already_aligned_state_produces_no_trades_and_no_round_trip() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["50", "50"], "0")
    assert plan.trades == () and plan.gross_buy_notional == 0 and plan.gross_sell_notional == 0
    _assert_reaches_target(plan)


def test_single_asset_all_cash_and_sells_never_touch_underweights() -> None:
    assert _summary(_plan([A], ["1"], ["10"], "5")) == [(A, STAGE.CASH_FUNDED_BUY, D("5"))]
    plan = _plan([A, B, C], ["0.2", "0.3", "0.5"], ["10", "20", "60"], "10")
    sold = {t.instrument_id for t in plan.trades if t.stage is STAGE.SELL}
    assert sold == {C}  # only the overweight asset is reduced, and exactly to target: minimum necessary sale
    assert [t.notional for t in plan.trades if t.stage is STAGE.SELL] == [D("10")]
    _assert_reaches_target(plan)


def test_cash_exceeding_every_buy_need_is_impossible_because_cash_is_part_of_wealth() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["0", "0"], "7")
    assert plan.gross_buy_notional == 7 and plan.gross_sell_notional == 0
    _assert_reaches_target(plan)


# --- exactness ---------------------------------------------------------------------------------------------------

def test_non_terminating_weights_still_reconcile_exactly_in_amount_space() -> None:
    third = "0.3333333333333333333333333333333333333333333333333"
    weights = [third, third, "0.3333333333333333333333333333333333333333333333334"]
    plan = _plan([A, B, C], weights, ["10", "0", "0"], "5")
    assert sum((F(v) for v in plan.target_values), F(0)) == F(plan.total_wealth) == 15
    assert F(plan.target_values[0]) == F(D(third)) * 15 and F(plan.target_values[2]) == F(D(weights[2])) * 15
    _assert_reaches_target(plan)
    assert sum((F(t.notional) for t in plan.trades if t.stage is STAGE.SELL), F(0)) == F(plan.gross_sell_notional)


def test_fractional_amounts_beyond_fifty_digits_are_exact() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["1.23456789012345678901234567890123456789012345678901234567890", "0"], "0.1E-70")
    exact_total = F(D("1.23456789012345678901234567890123456789012345678901234567890")) + F(D("0.1E-70"))
    assert F(plan.total_wealth) == exact_total
    _assert_reaches_target(plan)


def test_no_currency_rounding_is_applied() -> None:
    plan = _plan([A, B, C], ["0.3", "0.3", "0.4"], ["0.01", "0.02", "0.03"], "0")
    assert F(plan.target_values[0]) == F(D("0.3")) * F(D("0.06"))
    _assert_reaches_target(plan)


def test_public_zeros_are_unsigned() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["50", "50"], "0")
    values = [plan.total_wealth, plan.post_trade_cash, *plan.trade_deltas, plan.gross_buy_notional, plan.gross_sell_notional,
              plan.cash_funded_buy_notional, plan.sale_funded_buy_notional, *plan.post_trade_values]
    assert all(not v.is_signed() for v in values if v.is_zero())
    zero_target = _plan([A, B], ["0", "1"], ["30", "70"], "0")
    assert zero_target.target_values[0].as_tuple() == D(0).as_tuple() and not zero_target.post_trade_values[0].is_signed()
    assert zero_target.trade_deltas[1] == 30 and zero_target.post_trade_cash.as_tuple() == D(0).as_tuple()


# --- diagnostics -------------------------------------------------------------------------------------------------

def test_current_weights_and_drifts_are_diagnostic_only_and_never_drive_trades() -> None:
    plan = _plan([A, B], ["0.5", "0.5"], ["80", "10"], "10")
    assert plan.current_weights == (D("0.8"), D("0.1")) and plan.weight_drifts == (D("0.3"), D("-0.4"))
    assert [t.notional for t in plan.trades] == [D("10"), D("30"), D("30")]  # amount space, not weight space
    third_plan = _plan([A, B, C], ["0.3333333333333333333333333333333333333333333333333", "0.3333333333333333333333333333333333333333333333333",
                                   "0.3333333333333333333333333333333333333333333333334"], ["1", "1", "1"], "0")
    _assert_reaches_target(third_plan)
    assert abs(sum(F(w) for w in third_plan.current_weights) - 1) <= F(1, 10**45)


def test_derived_diagnostics_are_not_stored() -> None:
    stored = {f.name for f in dataclasses.fields(CashFirstRebalancePlan)}
    assert not stored & {"total_wealth", "target_values", "trade_deltas", "gross_buy_notional", "gross_sell_notional",
                         "cash_funded_buy_notional", "sale_funded_buy_notional", "post_trade_values", "post_trade_cash",
                         "current_weights", "weight_drifts"}


# --- plan self-validation / forge resistance ---------------------------------------------------------------------

def _good():
    target, state = _target([A, B, C, E], ["0.10", "0.20", "0.30", "0.40"]), _state([A, B, C, E], ["60", "20", "50", "55"], "15")
    return target, state, build_cash_first_rebalance_plan(target=target, state=state)


def _make(target, state, trades) -> CashFirstRebalancePlan:
    return CashFirstRebalancePlan(target=target, state=state, trades=trades)


def test_valid_plan_reconstructs_equal() -> None:
    target, state, good = _good()
    assert _make(target, state, good.trades) == good


def test_forged_plans_are_rejected() -> None:
    target, state, good = _good()
    trades = list(good.trades)

    def swap(index, **changes):
        copy = list(trades)
        copy[index] = dataclasses.replace(copy[index], **changes)
        return tuple(copy)

    with pytest.raises(TypeError):
        _make(target, state, list(trades))                                  # list instead of tuple
    with pytest.raises(TypeError):
        _make(target, state, (trades[0], "x"))                              # wrong trade type
    with pytest.raises(TypeError):
        _make(state, state, tuple(trades))                                  # wrong target type
    with pytest.raises(TypeError):
        _make(target, target, tuple(trades))                                # wrong state type
    forgeries = {
        "wrong stage": swap(0, stage=STAGE.SALE_FUNDED_BUY),
        "wrong instrument": swap(1, instrument_id=C),
        "wrong notional": swap(0, notional=trades[0].notional + D("0.01")),
        "missing buy": tuple(trades[:-1]),
        "missing sale": tuple(t for t in trades if t.stage is not STAGE.SELL),
        "extra sale": tuple(trades) + (RebalanceTradeInstruction(instrument_id=B, stage=STAGE.SELL, notional=D(1)),),
        "extra buy": tuple(trades) + (RebalanceTradeInstruction(instrument_id=B, stage=STAGE.CASH_FUNDED_BUY, notional=D(1)),),
        "wrong ordering": tuple(reversed(trades)),
        "empty": (),
        "sold underweight": (trades[0], RebalanceTradeInstruction(instrument_id=B, stage=STAGE.SELL, notional=D(5))) + tuple(trades[1:]),
        "oversold overweight": swap(1, notional=trades[1].notional + D(1)),
    }
    for name, forged in forgeries.items():
        with pytest.raises(ValueError, match=ERR_MATCH):
            _make(target, state, forged)
    with pytest.raises(ValueError, match=ERR_UNIVERSE):
        _make(target, _state([A, B, C], ["1", "1", "1"]), tuple(trades))
    zero = RebalanceTradeInstruction.__new__(RebalanceTradeInstruction)
    object.__setattr__(zero, "instrument_id", A)
    object.__setattr__(zero, "stage", STAGE.SELL)
    object.__setattr__(zero, "notional", D(0))
    with pytest.raises(ValueError, match=ERR_MATCH):
        _make(target, state, tuple(trades) + (zero,))                       # a zero instruction can never match the canonical plan


def test_aligned_state_rejects_a_forged_round_trip() -> None:
    target, state = _target([A, B], ["0.5", "0.5"]), _state([A, B], ["50", "50"], "0")
    round_trip = (RebalanceTradeInstruction(instrument_id=A, stage=STAGE.SELL, notional=D(1)),
                  RebalanceTradeInstruction(instrument_id=A, stage=STAGE.SALE_FUNDED_BUY, notional=D(1)))
    with pytest.raises(ValueError, match=ERR_MATCH):
        _make(target, state, round_trip)
    assert _make(target, state, ()).trades == ()


def test_funding_identity_failure_is_a_static_error(monkeypatch) -> None:
    target, state = _target([A, B], ["0.5", "0.5"]), _state([A, B], ["80", "10"], "10")
    original = module_under_test._exact_add

    def corrupt(values):
        result = original(values)
        return result + D(1) if len(values) == 2 and result == D(40) else result  # break gross_buy == cash + gross_sell

    monkeypatch.setattr(module_under_test, "_exact_add", corrupt)
    with pytest.raises(ValueError, match=ERR_FUNDING):
        build_cash_first_rebalance_plan(target=target, state=state)


# --- input order -------------------------------------------------------------------------------------------------

def test_canonical_upstream_sorting_gives_identical_plans_and_malformed_order_is_never_repaired() -> None:
    ids = [E, C, A, B]
    weights = {A: "0.10", B: "0.20", C: "0.30", E: "0.40"}
    values = {A: "60", B: "20", C: "50", E: "55"}
    plans = []
    for order in ([A, B, C, E], list(reversed([A, B, C, E]))):
        canonical = sorted(order, key=str)
        plans.append(_plan(canonical, [weights[i] for i in canonical], [values[i] for i in canonical], "15"))
    assert plans[0] == plans[1] and plans[0].trades == plans[1].trades
    with pytest.raises(ValueError):
        _target(ids, [weights[i] for i in ids])  # the constructor does not silently reorder


# --- Decimal isolation / range -----------------------------------------------------------------------------------

def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_changes_nothing_and_is_not_mutated() -> None:
    third = "0.3333333333333333333333333333333333333333333333333"
    weights = [third, third, "0.3333333333333333333333333333333333333333333333334"]
    baseline = _plan([A, B, C], weights, ["10.123456789", "0.000000001", "3"], "5.5")
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            plan = _plan([A, B, C], weights, ["10.123456789", "0.000000001", "3"], "5.5")
            derived = (plan.total_wealth, plan.target_values, plan.trade_deltas, plan.post_trade_values, plan.post_trade_cash,
                       plan.gross_buy_notional, plan.gross_sell_notional, plan.current_weights, plan.weight_drifts)
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert plan == baseline
        assert [(t.stage, t.notional.as_tuple()) for t in plan.trades] == [(t.stage, t.notional.as_tuple()) for t in baseline.trades]
        assert derived == (baseline.total_wealth, baseline.target_values, baseline.trade_deltas, baseline.post_trade_values,
                           baseline.post_trade_cash, baseline.gross_buy_notional, baseline.gross_sell_notional,
                           baseline.current_weights, baseline.weight_drifts)
        _assert_reaches_target(plan)


def test_repeated_builds_are_identical() -> None:
    first = _plan([A, B, C, E], ["0.10", "0.20", "0.30", "0.40"], ["60", "20", "50", "55"], "15")
    for _ in range(3):
        again = _plan([A, B, C, E], ["0.10", "0.20", "0.30", "0.40"], ["60", "20", "50", "55"], "15")
        assert again == first and [t.notional.as_tuple() for t in again.trades] == [t.notional.as_tuple() for t in first.trades]


def test_exact_arithmetic_resource_ceiling_is_a_representation_limit_checked_before_any_big_integer() -> None:
    assert module_under_test._EXACT_MAX_DECIMAL_PLACES == 1000
    at_ceiling = module_under_test._exact_add([D("1E+500"), D("1E-500")])
    assert F(at_ceiling) == F(10) ** 500 + F(1, 10 ** 500)
    for values in ([D("1E+501"), D("1E-500")], [D("1E+999999999"), D("1E-999999999")]):
        with pytest.raises(ValueError, match=ERR_RANGE):
            module_under_test._exact_add(values)
    with pytest.raises(ValueError, match=ERR_RANGE):
        module_under_test._exact_mul(D("1E+999999999999999999"), D("1E+999999999999999999"))
    assert module_under_test._exact_mul(D("1.5"), D("2.5")) == D("3.75") and module_under_test._exact_sub(D("1"), D("1")).as_tuple() == D(0).as_tuple()


def test_extreme_amounts_fail_closed_with_the_static_range_error() -> None:
    with pytest.raises(ValueError, match=ERR_RANGE):
        _plan([A, B], ["0.5", "0.5"], ["1E+999999999", "1E-999999999"], "0")


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_rebalance.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_plus_domain_and_the_exact_helper_only() -> None:
    plain: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            plain.add(node.module or "")
    assert plain <= {"__future__", "dataclasses", "decimal", "enum", "uuid",
                     "backend.engine.private.domain", "backend.engine.private.allocation_benchmarks"}
    for forbidden in ("numpy", "scipy", "pandas", "math", "random", "time", "datetime", "os", "allocation_cvar", "allocation_hrp",
                      "allocation_risk_parity", "allocation_user_view", "portfolio", "repository", "supabase", "fractions"):
        assert not any(forbidden in name for name in plain), forbidden


def test_no_ambient_context_float_or_global_context() -> None:
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"getcontext", "setcontext", "localcontext", "float", "Fraction"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]
    owners = [f.name for f in ast.walk(_TREE) if isinstance(f, ast.FunctionDef)
              for c in ast.walk(f) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "Context"]
    assert owners == ["_analytics_context"]  # only the diagnostic current_weights / drift view uses a Decimal context
    assert "prec=50" in _SOURCE and "ROUND_HALF_EVEN" in _SOURCE


_FORBIDDEN_FRAGMENTS = (
    "tax", "fee", "commission", "spread", "slippage", "lot", "cost_basis", "fifo", "lifo", "hifo", "ledger", "portfolio_transaction",
    "repository", "optimiz", "linprog", "scipy", "numpy", "threshold", "tolerance", "band", "turnover", "quantity", "price", "order_type",
    "limit_order", "market_order", "equal_weight", "inverse_vol", "risk_parity", "hrp", "cvar", "posterior", "persist", "supabase",
)


def test_no_tax_fee_lot_ledger_order_optimizer_or_rebalance_band_surface() -> None:
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
    builder = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_cash_first_rebalance_plan")
    assert [a.arg for a in builder.args.kwonlyargs] == ["target", "state"] and not builder.args.args


def test_documents_the_frictionless_cash_first_contract() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("frictionless", "cash-first", "investable cash", "same canonical instrument universe", "no tax", "context-free",
                   "minimum", "notional"):
        assert needle in doc, needle
