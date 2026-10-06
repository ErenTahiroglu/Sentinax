"""
backend/tests/test_backtest_rebalance_current_state.py
======================================================
Phase 26D3C: historical Phase 21 current-state composition. It packages D3A's marked values and D3B's already-classified investable cash into the closed `RebalanceCurrentState`,
over the held universe only, with exact Decimal representation preserved and no arithmetic, target, candidate, exit, cross-universe, Game Changer or rebalance logic. The result is a
historical/counterfactual input (D3B's cash policy is a fixed replay policy), never claimed as persisted or as an actual historical decision.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import backtest_rebalance_current_state as module_under_test
from backend.engine.private.allocation_rebalance import RebalanceCurrentState
from backend.engine.private.backtest_investable_cash import (
    PrivateBacktestCashAllocation,
    PrivateBacktestInvestableCashSelection,
    build_private_backtest_investable_cash_selection,
)
from backend.engine.private.backtest_rebalance_current_state import (
    PrivateBacktestRebalanceCurrentState,
    build_private_backtest_rebalance_current_state,
)
from backend.engine.private.domain import Currency
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_input_bindings import analysis
from backend.tests.test_backtest_investable_cash import cash_state
from backend.tests.test_backtest_marked_holdings import (
    A1,
    A2,
    I1,
    I2,
    I3,
    OWNER,
    bist_snap,
    build,
    bundle_for,
    buy,
    coverage_for,
    deposit,
    world,
)
from backend.tests.test_portfolio_projection import _make_portfolio

TRY, USD = Currency.TRY, Currency.USD
D = Decimal


def selection_for(state, amount="0") -> PrivateBacktestInvestableCashSelection:
    """An explicit allocation for EVERY positive raw balance: `amount` for the (single) valuation-currency TRY balance, exact zero elsewhere."""
    allocations = tuple(
        PrivateBacktestCashAllocation(account_id=b.account_id, currency=b.currency, investable_amount=D(amount) if b.currency is state.valuation_currency else D("0"))
        for b in state.cash_projection.positive_balances)
    return build_private_backtest_investable_cash_selection(marked_holdings_state=state, allocations=allocations)


def holdings_selection(amount="0"):
    _, bundle, _, _ = world()
    return selection_for(build(bundle), amount)


def compose(selection):
    return build_private_backtest_rebalance_current_state(investable_cash_selection=selection)


def direct(selection, current_state):
    return PrivateBacktestRebalanceCurrentState(investable_cash_selection=selection, current_state=current_state)


def expected_state(selection) -> RebalanceCurrentState:
    positions = selection.marked_holdings_state.marked_positions
    return RebalanceCurrentState(instrument_ids=tuple(p.instrument_id for p in positions), current_values=tuple(p.market_value for p in positions),
                                 investable_cash=selection.investable_cash, currency=selection.marked_holdings_state.valuation_currency)


def other_representation(value: Decimal) -> Decimal:
    """Numerically equal, differently represented (one extra trailing zero digit)."""
    sign, digits, exponent = value.as_tuple()
    other = Decimal((sign, digits + (0,), exponent - 1))
    assert other == value and other.as_tuple() != value.as_tuple()
    return other


def cash_only_selection(amount="0"):
    state = cash_state((A1, TRY, "100"))
    return selection_for(state, amount)


# --- A-F: contracts and identity -----------------------------------------------------------------------------------------

def test_result_is_exactly_two_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestRebalanceCurrentState)
    assert [f.name for f in fs] == ["investable_cash_selection", "current_state"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestRebalanceCurrentState.__dataclass_params__.frozen is True
    assert not {"instrument_ids", "current_values", "investable_cash", "currency", "portfolio_id", "replay_point", "evaluation_date"} & {f.name for f in fs}


def test_builder_is_keyword_only_with_one_parameter_and_no_default() -> None:
    params = inspect.signature(build_private_backtest_rebalance_current_state).parameters
    assert list(params) == ["investable_cash_selection"]
    assert params["investable_cash_selection"].kind is inspect.Parameter.KEYWORD_ONLY and params["investable_cash_selection"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        build_private_backtest_rebalance_current_state(holdings_selection())  # type: ignore[misc]


def test_exact_selection_type_is_required() -> None:
    selection = holdings_selection()

    class SubSelection(PrivateBacktestInvestableCashSelection):
        pass

    sub = SubSelection(marked_holdings_state=selection.marked_holdings_state, allocations=selection.allocations)
    for bad in (sub, object(), None, selection.marked_holdings_state, selection.allocations):
        with pytest.raises(TypeError):
            compose(bad)


def test_selection_is_retained_by_identity_and_d3a_state_comes_only_through_it() -> None:
    selection = holdings_selection("100")
    result = compose(selection)
    assert result.investable_cash_selection is selection
    assert result.investable_cash_selection.marked_holdings_state is selection.marked_holdings_state
    assert "marked_holdings_state" not in inspect.signature(build_private_backtest_rebalance_current_state).parameters


# --- G-Q: successful composition ---------------------------------------------------------------------------------------------

def test_multiple_holdings_succeed_in_d3a_canonical_order() -> None:
    selection = holdings_selection("100")
    positions = selection.marked_holdings_state.marked_positions
    assert len(positions) == 3
    result = compose(selection)
    assert result.current_state.instrument_ids == tuple(p.instrument_id for p in positions)
    assert [str(i) for i in result.current_state.instrument_ids] == sorted(str(i) for i in (I1, I2, I3))


def _single_holding_ledger():
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    txs = [deposit(pf, A1, "1000", 0, USD), deposit(pf, A1, "1000", 1, TRY), buy(pf, A1, I1, "4", 2)]
    return context, pf, txs, bist_snap(context)


def test_a_single_marked_holding_is_valid_without_diversification() -> None:
    context, pf, txs, snap = _single_holding_ledger()
    state = build(bundle_for(context, coverage_for(context, pf, txs), snap), TRY)
    selection = selection_for(state, "250")
    assert len(state.marked_positions) == 1
    result = compose(selection)
    assert result.current_state.instrument_ids == (I1,) and result.current_state.investable_cash == D("250")


def test_current_values_are_d3a_market_values_by_position_without_recomputation() -> None:
    selection = holdings_selection("100")
    positions = selection.marked_holdings_state.marked_positions
    state = compose(selection).current_state
    assert len(state.current_values) == len(positions)
    for value, position in zip(state.current_values, positions):
        assert value is position.market_value                                        # the very D3A Decimal object: nothing was multiplied, summed or rounded here
        assert value.as_tuple() == position.market_value.as_tuple()


def test_investable_cash_is_exactly_the_d3b_derived_value_and_zero_is_valid_with_holdings() -> None:
    for amount in ("0", "123.456", "500"):
        selection = holdings_selection(amount)
        state = compose(selection).current_state
        assert state.investable_cash is selection.investable_cash or state.investable_cash.as_tuple() == selection.investable_cash.as_tuple()
        assert state.investable_cash == D(amount)
    zero = compose(holdings_selection("0"))
    assert zero.current_state.investable_cash == D("0") and len(zero.current_state.instrument_ids) == 3


def test_currency_is_the_d3a_valuation_currency_and_foreign_raw_cash_does_not_appear() -> None:
    selection = holdings_selection("100")
    state = selection.marked_holdings_state
    foreign = [b for b in state.cash_projection.positive_balances if b.currency is not state.valuation_currency]
    assert foreign                                                                  # large USD raw cash exists in the ledger
    result = compose(selection).current_state
    assert result.currency is state.valuation_currency is TRY
    assert result.investable_cash == D("100")                                       # no USD amount and no conversion leaked into the Phase 21 state
    assert {f.name for f in dataclasses.fields(result)} == {"instrument_ids", "current_values", "investable_cash", "currency"}


# --- R-T: cash-only is a representational limit --------------------------------------------------------------------------------

@pytest.mark.parametrize("amount", ["0", "40"])
def test_cash_only_portfolio_fails_closed_because_the_phase_21_universe_cannot_be_empty(amount) -> None:
    selection = cash_only_selection(amount)
    assert selection.marked_holdings_state.marked_positions == ()
    with pytest.raises(ValueError) as info:
        compose(selection)
    assert "held" in str(info.value) and "empty" in str(info.value)                 # states the representational limitation, not an investment-policy verdict
    with pytest.raises((TypeError, ValueError)):
        RebalanceCurrentState(instrument_ids=(), current_values=(), investable_cash=D(amount), currency=TRY)        # the closed type itself cannot represent it


# --- U-AF: direct construction -----------------------------------------------------------------------------------------------

def test_direct_construction_accepts_the_builder_state_and_an_independent_canonical_state() -> None:
    selection = holdings_selection("100")
    built = compose(selection)
    assert direct(selection, built.current_state) == built
    independent = expected_state(selection)
    assert independent is not built.current_state
    assert direct(selection, independent) == built


def test_current_state_must_be_the_exact_closed_type() -> None:
    selection = holdings_selection("100")
    good = expected_state(selection)

    class SubState(RebalanceCurrentState):
        pass

    sub = SubState(instrument_ids=good.instrument_ids, current_values=good.current_values, investable_cash=good.investable_cash, currency=good.currency)
    for bad in (sub, object(), None, selection):
        with pytest.raises(TypeError):
            direct(selection, bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        direct(object(), good)  # type: ignore[arg-type]


def test_direct_construction_rejects_wrong_missing_extra_and_reordered_instruments() -> None:
    selection = holdings_selection("100")
    good = expected_state(selection)
    ids, values = good.instrument_ids, good.current_values
    wrong_id = UUID(int=0x7FFF)
    cases = {
        "wrong id": (tuple(sorted((wrong_id,) + ids[1:], key=str)), values),
        "missing": (ids[:-1], values[:-1]),
        "extra": (tuple(sorted(ids + (wrong_id,), key=str)), values + (values[0],)),
        "reordered": (tuple(reversed(ids)), tuple(reversed(values))),
    }
    for name, (case_ids, case_values) in cases.items():
        try:
            forged = RebalanceCurrentState(instrument_ids=case_ids, current_values=case_values, investable_cash=good.investable_cash, currency=good.currency)
        except (TypeError, ValueError):
            continue                                                                  # the closed type itself already refused it (e.g. unsorted ids)
        with pytest.raises(ValueError, match=".*"):
            direct(selection, forged)


def test_direct_construction_rejects_wrong_values_cash_and_currency() -> None:
    selection = holdings_selection("100")
    good = expected_state(selection)
    values = good.current_values

    def replace(**over):
        base = dict(instrument_ids=good.instrument_ids, current_values=good.current_values, investable_cash=good.investable_cash, currency=good.currency)
        base.update(over)
        return RebalanceCurrentState(**base)

    with pytest.raises(ValueError):
        direct(selection, replace(current_values=(values[0] + D("0.000001"),) + values[1:]))
    with pytest.raises(ValueError):
        direct(selection, replace(current_values=(values[1], values[0]) + values[2:]))
    with pytest.raises(ValueError):
        direct(selection, replace(investable_cash=good.investable_cash + D("1")))
    with pytest.raises(ValueError):
        direct(selection, replace(currency=USD))


def test_numerically_equal_but_differently_represented_decimals_are_rejected() -> None:
    selection = holdings_selection("100")
    good = expected_state(selection)
    for index in range(len(good.current_values)):
        values = list(good.current_values)
        values[index] = other_representation(values[index])
        forged = RebalanceCurrentState(instrument_ids=good.instrument_ids, current_values=tuple(values), investable_cash=good.investable_cash, currency=good.currency)
        assert forged.current_values[index] == good.current_values[index]
        with pytest.raises(ValueError):
            direct(selection, forged)
    forged_cash = RebalanceCurrentState(instrument_ids=good.instrument_ids, current_values=good.current_values, investable_cash=other_representation(good.investable_cash),
                                        currency=good.currency)
    assert forged_cash.investable_cash == good.investable_cash
    with pytest.raises(ValueError):
        direct(selection, forged_cash)


def test_direct_construction_rejects_a_state_of_a_different_selection_and_a_cash_only_selection() -> None:
    selection = holdings_selection("100")
    other = holdings_selection("200")
    with pytest.raises(ValueError):
        direct(selection, expected_state(other))
    cash_only = cash_only_selection("0")
    with pytest.raises(ValueError):
        direct(cash_only, expected_state(selection))


# --- AG-AU: scope guards -------------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_TREE = ast.parse(_PATH.read_text(encoding="utf-8"))
_REL = "backend/engine/private/backtest_rebalance_current_state.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_no_target_candidate_cross_universe_exit_game_changer_or_rebalance_surface() -> None:
    assert not _names() & {"RebalanceTargetAllocation", "CrossUniverseAuthority", "CrossAssetSleeve", "CrossAssetCompositionPlan", "build_cross_asset_composition_plan",
                           "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan", "RebalanceBandPolicy", "RebalanceFrictionProfile",
                           "PrivateBacktestCandidateEligibilityReplay", "PrivateBacktestGameChangerDecisionReplay", "GameChangerDecisionGate", "CashBucket", "CashPurpose",
                           "confirmed_zero_current_value_instrument_ids", "authorized_exit_instrument_ids", "bind_candidate_universes_to_sleeves", "scheduler", "dispatcher"}


def test_no_raw_cash_allocation_price_or_market_data_access() -> None:
    assert not _names() & {"cash_projection", "positive_balances", "balances", "allocations", "market_observation", "resolution_snapshot", "close", "unit_price", "price",
                           "quantity", "position_projection", "input_bundle", "PointInTimeMarketDataResolver", "fx_rate", "convert", "base_currency"}


def test_no_financial_arithmetic_float_rounding_or_quantization() -> None:
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign))]
    assert not _names() & {"float", "round", "quantize", "sum", "min", "max", "abs"}


def test_imports_are_only_the_selection_and_the_closed_phase_21_state() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules == {"backend.engine.private.allocation_rebalance", "backend.engine.private.backtest_investable_cash"}
    imported = {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend") for a in n.names}
    assert imported == {"RebalanceCurrentState", "PrivateBacktestInvestableCashSelection"}


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "json", "loads", "dumps"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_REBALANCE_CURRENT_STATE.md").read_text(encoding="utf-8")
    for needle in ("input-composition boundary", "historical marked values", "counterfactual", "not claimed", "held-instrument universe", "no target", "no candidate additions",
                   "no exit inference", "no zero-current", "Decimal representation", "zero investable cash", "cash-only", "representational", "cross-universe", "no FX",
                   "no rebalance"):
        assert needle in doc, needle
    cash = (root / "docs" / "PRIVATE_BACKTEST_INVESTABLE_CASH.md").read_text(encoding="utf-8")
    assert "D3C consumes D3B's classified investable cash together with D3A marked holdings to construct the held-universe Phase 21 RebalanceCurrentState" in cash
    assert "cash-only portfolios remain deferred to the later explicit cross-universe composition boundary" in cash
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_rebalance_current_state.py" in architecture
