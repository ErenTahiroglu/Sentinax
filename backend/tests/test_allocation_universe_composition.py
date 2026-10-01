"""
backend/tests/test_allocation_universe_composition.py
=====================================================
Phase 22A: explicit cross-asset sleeve composition and cross-universe reconciliation. The module only builds a same-universe
Phase 21 target / current-state pair from explicit authority; it never discovers, ranks, infers or liquidates anything.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import allocation_universe_composition as module_under_test
from backend.engine.private.allocation_rebalance import (
    RebalanceCurrentState,
    RebalanceTargetAllocation,
    RebalanceTradeStage,
    build_cash_first_rebalance_plan,
)
from backend.engine.private.allocation_rebalance_policy import (
    RebalanceBandPolicy,
    RebalanceFrictionProfile,
    build_band_aware_rebalance_plan,
)
from backend.engine.private.allocation_universe_composition import (
    CrossAssetCompositionPlan,
    CrossAssetSleeve,
    CrossUniverseAuthority,
    build_cross_asset_composition_plan,
)
from backend.engine.private.domain import AssetClass, Currency
from backend.tests.invariants import static_guards as sg

D = Decimal
EQ, FI = AssetClass.EQUITY, AssetClass.FIXED_INCOME


def _sleeve(asset_class, weight, ids, weights) -> CrossAssetSleeve:
    return CrossAssetSleeve(asset_class=asset_class, target_weight=D(weight), instrument_ids=tuple(ids),
                            instrument_weights=tuple(D(w) for w in weights))


def _state(ids, values, cash="40") -> RebalanceCurrentState:
    return RebalanceCurrentState(instrument_ids=tuple(ids), current_values=tuple(D(v) for v in values),
                                 investable_cash=D(cash), currency=Currency.TRY)


def _authority(zero=(), exits=()) -> CrossUniverseAuthority:
    return CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids=tuple(zero),
                                  authorized_exit_instrument_ids=tuple(exits))


# Fixture UUIDs: A=1 B=2 C=3 D=4 E=5 (canonical ascending string order)
UA, UB, UC, UD, UE = (UUID(int=i) for i in (1, 2, 3, 4, 5))


def _fixture():
    sleeves = (_sleeve(EQ, "0.60", [UA, UB], ["0.70", "0.30"]), _sleeve(FI, "0.40", [UC, UD], ["0.25", "0.75"]))
    state = _state([UA, UC, UE], ["30", "20", "10"], "40")
    authority = _authority(zero=[UB, UD], exits=[UE])
    return state, sleeves, authority


def _plan(state=None, sleeves=None, authority=None) -> CrossAssetCompositionPlan:
    base = _fixture()
    return build_cross_asset_composition_plan(current_state=state or base[0], sleeves=sleeves or base[1], authority=authority or base[2])


# --- hand fixture ------------------------------------------------------------------------------------------------

def test_true_cross_universe_hand_fixture() -> None:
    state, sleeves, authority = _fixture()
    plan = build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=authority)
    assert plan.rebalance_target.instrument_ids == (UA, UB, UC, UD, UE) == plan.rebalance_state.instrument_ids
    assert plan.rebalance_target.weights == (D("0.42"), D("0.18"), D("0.10"), D("0.30"), D(0))   # 0.6*0.7, 0.6*0.3, 0.4*0.25, 0.4*0.75
    assert plan.rebalance_state.current_values == (D("30"), D("0"), D("20"), D("0"), D("10"))      # zero only for confirmed B / D
    assert plan.rebalance_state.investable_cash == D("40") and plan.rebalance_state.currency is Currency.TRY
    assert plan.rebalance_target.weights[4].is_signed() is False and plan.rebalance_state.current_values[1].is_signed() is False
    assert plan.current_state is state and plan.authority is authority and plan.sleeves is sleeves
    assert plan.target_candidate_instrument_ids == (UA, UB, UC, UD)
    assert plan.current_only_exit_instrument_ids == (UE,)
    assert plan.target_only_zero_confirmed_instrument_ids == (UB, UD)
    assert plan.reconciled_instrument_ids == (UA, UB, UC, UD, UE)
    assert plan.asset_class_target_weights == ((EQ, D("0.60")), (FI, D("0.40")))


def test_stored_fields_are_exactly_the_specified_ones_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(CrossAssetSleeve)] == ["asset_class", "target_weight", "instrument_ids", "instrument_weights"]
    assert [f.name for f in dataclasses.fields(CrossUniverseAuthority)] == ["confirmed_zero_current_value_instrument_ids", "authorized_exit_instrument_ids"]
    assert [f.name for f in dataclasses.fields(CrossAssetCompositionPlan)] == ["current_state", "sleeves", "authority", "rebalance_target", "rebalance_state"]
    plan = _plan()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.rebalance_target = plan.rebalance_target  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.sleeves[0].target_weight = D(1)  # type: ignore[misc]


def test_builder_is_keyword_only_strictly_typed_and_has_no_defaults() -> None:
    state, sleeves, authority = _fixture()
    with pytest.raises(TypeError):
        build_cross_asset_composition_plan(state, sleeves, authority)  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_cross_asset_composition_plan(current_state=state, sleeves=sleeves)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_cross_asset_composition_plan(current_state=object(), sleeves=sleeves, authority=authority)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_cross_asset_composition_plan(current_state=state, sleeves=list(sleeves), authority=authority)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_cross_asset_composition_plan(current_state=state, sleeves=(sleeves[0], object()), authority=authority)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):                        # empty sleeves: same convention as an empty instrument id tuple
        build_cross_asset_composition_plan(current_state=state, sleeves=(), authority=authority)


# --- sleeve validation -------------------------------------------------------------------------------------------

class _DecSub(Decimal):
    pass


_BAD_TYPES = (0.5, 1, True, "0.5", _DecSub("0.5"), None, [D("0.5")])


def test_sleeve_asset_class_and_weight_types() -> None:
    for bad in ("equity", None, 1):
        with pytest.raises(TypeError):
            CrossAssetSleeve(asset_class=bad, target_weight=D(1), instrument_ids=(UA,), instrument_weights=(D(1),))  # type: ignore[arg-type]
    for bad in _BAD_TYPES:
        with pytest.raises(TypeError):
            CrossAssetSleeve(asset_class=EQ, target_weight=bad, instrument_ids=(UA,), instrument_weights=(D(1),))  # type: ignore[arg-type]
    for bad in ("NaN", "Infinity", "-0.5", "-0", "0", "0.0"):
        with pytest.raises(ValueError):
            _sleeve(EQ, bad, [UA], ["1"])


def test_sleeve_instrument_ids_contract() -> None:
    for bad in ([UA], (), None, (UA, "x"), ("x",)):
        with pytest.raises(TypeError):
            CrossAssetSleeve(asset_class=EQ, target_weight=D(1), instrument_ids=bad, instrument_weights=(D(1),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _sleeve(EQ, "1", [UB, UA], ["0.5", "0.5"])       # never silently sorted
    with pytest.raises(ValueError):
        _sleeve(EQ, "1", [UA, UA], ["0.5", "0.5"])


def test_sleeve_instrument_weight_contract() -> None:
    for bad in ((0.5,), (1,), (True,), ("1",), (_DecSub(1),), [D(1)], None):
        with pytest.raises(TypeError):
            CrossAssetSleeve(asset_class=EQ, target_weight=D(1), instrument_ids=(UA,), instrument_weights=bad)  # type: ignore[arg-type]
    for bad in ("NaN", "Infinity", "-0.1", "-0", "0", "1.5"):
        with pytest.raises(ValueError):
            _sleeve(EQ, "1", [UA, UB], [bad, "0.5"])
    with pytest.raises(ValueError):
        _sleeve(EQ, "1", [UA, UB], ["1", "0"])            # zero within-sleeve weight is not allowed
    with pytest.raises(ValueError):
        _sleeve(EQ, "1", [UA, UB], ["0.5", "0.4999999999999999999999999999999999999999999999999999"])
    with pytest.raises(ValueError):
        _sleeve(EQ, "1", [UA, UB], ["0.5", "0.5000000000000000000000000000000000000000000000000001"])
    with pytest.raises(ValueError):
        CrossAssetSleeve(asset_class=EQ, target_weight=D(1), instrument_ids=(UA, UB), instrument_weights=(D("1"),))  # shape
    assert _sleeve(EQ, "1", [UA], ["1"]).instrument_weights == (D(1),)


# --- authority validation ----------------------------------------------------------------------------------------

def test_authority_contract() -> None:
    for bad in ([UA], None, (UA, "x"), "x"):
        with pytest.raises(TypeError):
            CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids=bad, authorized_exit_instrument_ids=())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids=(), authorized_exit_instrument_ids=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _authority(zero=[UB, UA])
    with pytest.raises(ValueError):
        _authority(exits=[UA, UA])
    with pytest.raises(ValueError):
        _authority(zero=[UA], exits=[UA])                # disjoint
    with pytest.raises(TypeError):
        CrossUniverseAuthority(confirmed_zero_current_value_instrument_ids=())  # type: ignore[call-arg]
    assert _authority().confirmed_zero_current_value_instrument_ids == ()


# --- sleeve collection -------------------------------------------------------------------------------------------

def test_sleeve_collection_validation() -> None:
    state = _state([UA, UC], ["30", "20"], "10")
    authority = _authority()
    good = (_sleeve(EQ, "0.5", [UA], ["1"]), _sleeve(FI, "0.5", [UC], ["1"]))
    assert build_cross_asset_composition_plan(current_state=state, sleeves=good, authority=authority).rebalance_target.weights == (D("0.5"), D("0.5"))
    with pytest.raises(ValueError):                       # noncanonical (caller) asset-class order, never reordered
        build_cross_asset_composition_plan(current_state=state, sleeves=(good[1], good[0]), authority=authority)
    with pytest.raises(ValueError):                       # duplicate asset class
        build_cross_asset_composition_plan(current_state=state, sleeves=(_sleeve(EQ, "0.5", [UA], ["1"]), _sleeve(EQ, "0.5", [UC], ["1"])), authority=authority)
    with pytest.raises(ValueError):                       # same UUID in two sleeves
        build_cross_asset_composition_plan(current_state=state, sleeves=(_sleeve(EQ, "0.5", [UA], ["1"]), _sleeve(FI, "0.5", [UA], ["1"])), authority=authority)
    with pytest.raises(ValueError):                       # sleeve targets must sum exactly to 1
        build_cross_asset_composition_plan(current_state=state, sleeves=(_sleeve(EQ, "0.5", [UA], ["1"]), _sleeve(FI, "0.4999999999999999999999999999999999", [UC], ["1"])), authority=authority)
    with pytest.raises(ValueError):
        build_cross_asset_composition_plan(current_state=state, sleeves=(_sleeve(EQ, "0.5", [UA], ["1"]), _sleeve(FI, "0.5000000000000000000000000000000001", [UC], ["1"])), authority=authority)


def test_canonical_asset_class_order_is_the_enum_value_order() -> None:
    values = sorted(c.value for c in AssetClass)
    assert values == ["commodity", "equity", "etf", "fixed_income", "fund", "fx"]
    ids = [UA, UB, UC]
    state = _state(ids, ["10", "10", "10"], "0")
    ok = (_sleeve(AssetClass.COMMODITY, "0.2", [UA], ["1"]), _sleeve(AssetClass.ETF, "0.3", [UB], ["1"]), _sleeve(AssetClass.FX, "0.5", [UC], ["1"]))
    assert build_cross_asset_composition_plan(current_state=state, sleeves=ok, authority=_authority()).rebalance_target.weights == (D("0.2"), D("0.3"), D("0.5"))
    with pytest.raises(ValueError):
        build_cross_asset_composition_plan(current_state=state, sleeves=(ok[0], ok[2], ok[1]), authority=_authority())


# --- target-only confirmation / current-only exit ----------------------------------------------------------------

def test_target_only_requires_exact_zero_current_confirmation() -> None:
    state, sleeves, _ = _fixture()
    for zero in ([UB], [UD], [], [UB, UD, UE], [UA, UB, UD]):
        with pytest.raises(ValueError):
            build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=_authority(zero=zero, exits=[UE]))


def test_current_only_requires_exact_exit_authorization() -> None:
    state, sleeves, _ = _fixture()
    for exits in ([], [UA, UE], [UC, UE]):
        zero = [UB, UD]
        with pytest.raises(ValueError):
            build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=_authority(zero=zero, exits=exits))
    with pytest.raises(ValueError):                       # stale authorization of an unknown instrument
        build_cross_asset_composition_plan(current_state=state, sleeves=sleeves,
                                           authority=_authority(zero=[UB, UD], exits=[UE, UUID(int=99)]))
    with pytest.raises(ValueError):                       # stale confirmation of an unknown instrument
        build_cross_asset_composition_plan(current_state=state, sleeves=sleeves,
                                           authority=_authority(zero=[UB, UD, UUID(int=99)], exits=[UE]))


def test_retained_holding_needs_no_exit_authorization_and_is_never_liquidated_automatically() -> None:
    sleeves = (_sleeve(EQ, "0.60", [UA, UB], ["0.70", "0.30"]), _sleeve(FI, "0.40", [UC, UD, UE], ["0.25", "0.50", "0.25"]))
    state = _state([UA, UC, UE], ["30", "20", "10"], "40")
    plan = build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=_authority(zero=[UB, UD]))
    assert plan.current_only_exit_instrument_ids == ()
    assert plan.rebalance_target.weights == (D("0.42"), D("0.18"), D("0.10"), D("0.20"), D("0.10"))
    assert plan.rebalance_state.current_values[4] == D("10") and plan.rebalance_target.weights[4] > 0
    with pytest.raises(ValueError):                       # an exit authorization for a retained holding is stale
        build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=_authority(zero=[UB, UD], exits=[UE]))
    with pytest.raises(ValueError):                       # dropping a held instrument without authorization is not "sell by default"
        build_cross_asset_composition_plan(current_state=state, sleeves=sleeves[:1] + (_sleeve(FI, "0.40", [UC, UD], ["0.5", "0.5"]),),
                                           authority=_authority(zero=[UB, UD]))


def test_no_cross_universe_gap_gives_empty_authority_and_identical_universe() -> None:
    sleeves = (_sleeve(EQ, "1", [UA, UB], ["0.5", "0.5"]),)
    plan = build_cross_asset_composition_plan(current_state=_state([UA, UB], ["10", "30"], "0"), sleeves=sleeves, authority=_authority())
    assert plan.rebalance_state.current_values == (D("10"), D("30")) and plan.rebalance_target.weights == (D("0.5"), D("0.5"))


# --- Phase 21 integration ----------------------------------------------------------------------------------------

def test_phase_21a_consumes_the_outputs_without_universe_repair() -> None:
    plan = _plan()
    result = build_cash_first_rebalance_plan(target=plan.rebalance_target, state=plan.rebalance_state)
    trades = [(t.instrument_id, t.stage, t.notional) for t in result.trades]
    S = RebalanceTradeStage
    assert trades == [(UA, S.CASH_FUNDED_BUY, D("12")), (UB, S.CASH_FUNDED_BUY, D("18")), (UD, S.CASH_FUNDED_BUY, D("10")),
                      (UC, S.SELL, D("10")), (UE, S.SELL, D("10")), (UD, S.SALE_FUNDED_BUY, D("20"))]
    assert any(t[0] == UB and t[1] is not S.SELL for t in trades)        # confirmed-zero candidate receives a buy
    assert any(t[0] == UE and t[1] is S.SELL for t in trades)            # authorized current-only exit is sold toward zero
    assert result.post_trade_values[4] == 0 and result.post_trade_cash == 0


def test_phase_21b_accepts_the_outputs_with_caller_supplied_same_universe_objects() -> None:
    plan = _plan()
    ids = plan.reconciled_instrument_ids
    policy = RebalanceBandPolicy(instrument_ids=ids, trigger_drifts=(D("0.05"),) * 5, destination_drifts=(D("0.02"),) * 5)
    friction = RebalanceFrictionProfile(instrument_ids=ids, buy_friction_rates=(D("0.001"),) * 5, sell_friction_rates=(D("0.002"),) * 5)
    band = build_band_aware_rebalance_plan(target=plan.rebalance_target, state=plan.rebalance_state, policy=policy, friction=friction)
    assert band.is_triggered and band.post_trade_cash == 0 and band.target is plan.rebalance_target and band.state is plan.rebalance_state


# --- forge resistance --------------------------------------------------------------------------------------------

def _forge(good, **changes) -> CrossAssetCompositionPlan:
    values = dict(current_state=good.current_state, sleeves=good.sleeves, authority=good.authority,
                  rebalance_target=good.rebalance_target, rebalance_state=good.rebalance_state)
    values.update(changes)
    return CrossAssetCompositionPlan(**values)


def test_valid_plan_reconstructs_and_forgeries_are_rejected() -> None:
    good = _plan()
    assert _forge(good) == good
    ids, weights = good.rebalance_target.instrument_ids, good.rebalance_target.weights
    values, st = good.rebalance_state.current_values, good.rebalance_state

    def target(i=ids, w=weights):
        return RebalanceTargetAllocation(instrument_ids=tuple(i), weights=tuple(w))

    def state(i=ids, v=values, cash=st.investable_cash, cur=st.currency):
        return RebalanceCurrentState(instrument_ids=tuple(i), current_values=tuple(v), investable_cash=cash, currency=cur)

    forged_targets = {
        "forged weight": target(w=(D("0.43"), D("0.17"), *weights[2:])),
        "extra candidate": target(i=(*ids, UUID(int=9)), w=(*weights, D(0))),
        "missing holding": target(i=ids[:4], w=(weights[0], weights[1], weights[2], weights[3] + weights[4])),
        "non-canonical representation": target(w=(D("0.42"), D("0.18"), D("0.1"), D("0.3"), D("0"))),
    }
    for name, forged in forged_targets.items():
        with pytest.raises(ValueError):
            _forge(good, rebalance_target=forged)
    forged_states = {
        "forged zero value": state(v=(values[0], D("1"), *values[2:])),
        "changed cash": state(cash=D("41")),
        "changed currency": state(cur=Currency.USD),
        "forged current value": state(v=(D("31"), *values[1:])),
        "missing holding": state(i=ids[:4], v=values[:4]),
    }
    for name, forged in forged_states.items():
        with pytest.raises(ValueError):
            _forge(good, rebalance_state=forged)
    with pytest.raises(ValueError):                       # unauthorized exit / unconfirmed zero target
        _forge(good, authority=_authority(zero=[UB, UD]))
    with pytest.raises(ValueError):
        _forge(good, authority=_authority(zero=[UB], exits=[UE]))
    with pytest.raises(ValueError):                       # source state changed, derived outputs stale
        _forge(good, current_state=_state([UA, UC, UE], ["31", "20", "10"], "40"))


def test_constructor_type_errors() -> None:
    good = _plan()
    for field, wrong in (("current_state", object()), ("sleeves", list(good.sleeves)), ("authority", object()),
                         ("rebalance_target", object()), ("rebalance_state", object())):
        with pytest.raises(TypeError):
            _forge(good, **{field: wrong})
    with pytest.raises(TypeError):
        _forge(good, sleeves=(good.sleeves[0], object()))


def test_wrong_union_order_target_is_rejected_by_phase_21a_or_the_plan() -> None:
    good = _plan()
    with pytest.raises(ValueError):
        RebalanceTargetAllocation(instrument_ids=good.rebalance_target.instrument_ids[::-1], weights=good.rebalance_target.weights[::-1])


# --- exactness / hostile ambient Decimal -------------------------------------------------------------------------

def test_exact_products_beyond_fifty_digits() -> None:
    third = "0.3333333333333333333333333333333333333333333333333"
    last = "0.3333333333333333333333333333333333333333333333334"
    sleeves = (_sleeve(EQ, third, [UA], ["1"]), _sleeve(FI, third, [UC], ["1"]), _sleeve(AssetClass.FX, last, [UD], ["1"]))
    state = _state([UA, UC, UD], ["10", "10", "10"], "0")
    plan = build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=_authority())
    assert plan.rebalance_target.weights == (D(third), D(third), D(last))


def test_exact_product_is_not_rounded() -> None:
    sleeves = (_sleeve(EQ, "0.3333333333333333333333333333333333333333333333333", [UA, UB], ["0.5", "0.5"]),
               _sleeve(FI, "0.6666666666666666666666666666666666666666666666667", [UC], ["1"]))
    state = _state([UA, UB, UC], ["1", "1", "1"], "0")
    plan = build_cross_asset_composition_plan(current_state=state, sleeves=sleeves, authority=_authority())
    assert plan.rebalance_target.weights[0] == D("0.16666666666666666666666666666666666666666666666665")  # 51 significant digits kept exactly


def _hostile_contexts():
    return [
        decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3,
                        traps=[decimal.InvalidOperation, decimal.Overflow, decimal.Inexact, decimal.Rounded]),
        decimal.Context(prec=1, rounding=decimal.ROUND_CEILING, traps=[]),
    ]


def test_hostile_ambient_context_changes_nothing_and_is_not_mutated() -> None:
    baseline = _plan()
    for hostile in _hostile_contexts():
        with decimal.localcontext(hostile) as ambient:
            ambient.clear_flags()
            snapshot = (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps))
            plan = _plan()
            assert (ambient.prec, ambient.rounding, ambient.Emin, ambient.Emax, dict(ambient.flags), dict(ambient.traps)) == snapshot
            assert not any(ambient.flags.values())
        assert plan == baseline
        assert [w.as_tuple() for w in plan.rebalance_target.weights] == [w.as_tuple() for w in baseline.rebalance_target.weights]
        assert [v.as_tuple() for v in plan.rebalance_state.current_values] == [v.as_tuple() for v in baseline.rebalance_state.current_values]
        assert plan.reconciled_instrument_ids == baseline.reconciled_instrument_ids
        assert plan.asset_class_target_weights == baseline.asset_class_target_weights


def test_repeated_builds_are_identical() -> None:
    first = _plan()
    for _ in range(3):
        assert _plan() == first


def test_extreme_exponent_span_fails_closed() -> None:
    with pytest.raises(ValueError):
        _sleeve(EQ, "1", [UA, UB], ["0.5", "0.5E-1500"])
    with pytest.raises(ValueError):
        build_cross_asset_composition_plan(
            current_state=_state([UA, UC], ["1", "1"], "0"),
            sleeves=(_sleeve(EQ, "0.5", [UA], ["1"]), _sleeve(FI, "0.5E-1500", [UC], ["1"])), authority=_authority())


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/allocation_universe_composition.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_domain_asset_class_and_phase_21a_only() -> None:
    names: set[str] = set()
    imported: set[tuple[str, str]] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.update((node.module or "", alias.name) for alias in node.names)
    assert names <= {"__future__", "dataclasses", "decimal", "uuid", "backend.engine.private.domain",
                     "backend.engine.private.allocation_rebalance"}
    assert {n for m, n in imported if m == "backend.engine.private.domain"} == {"AssetClass"}
    for forbidden in ("identity", "fund_category", "allocation_rebalance_policy", "allocation_matrix", "allocation_cvar",
                      "allocation_user_view", "allocation_hrp", "allocation_risk", "allocation_benchmarks", "portfolio", "repository",
                      "supabase", "provider", "fee_tax", "numpy", "scipy", "pandas", "math", "random", "time", "datetime"):
        assert not any(forbidden in name for name in names), forbidden
    assert not {n for _, n in imported} & {"InstrumentRecord", "InstrumentType", "TefasFundCategoryObservation"}


def test_no_ambient_context_float_division_or_second_precision_regime() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"getcontext", "setcontext", "localcontext", "Context", "float", "isclose", "quantize", "Fraction"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv))]


_FORBIDDEN_FRAGMENTS = (
    "tax", "capital_gain", "withholding", "cost_basis", "lot", "fifo", "lifo", "hifo", "broker", "order", "quantity", "execution",
    "settle", "historical_fee", "score", "rank", "recommend", "expected_return", "utility", "confidence", "optimiz", "solver",
    "taxonomy", "category", "provider", "symbol", "isin", "mic", "price", "persist", "supabase",
)


def test_no_tax_lot_execution_ranking_or_taxonomy_surface() -> None:
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
    builder = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_cross_asset_composition_plan")
    assert [a.arg for a in builder.args.kwonlyargs] == ["current_state", "sleeves", "authority"] and not builder.args.args


def test_documents_the_composition_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("explicit", "sleeve", "zero-current", "exit", "representation only", "no tax", "no taxonomy", "context-free",
                   "not discover", "same-universe"):
        assert needle in doc, needle
