"""
backend/tests/test_backtest_marked_holdings.py
==============================================
Phase 26D3A: historical marked holdings and raw cash projection. Closed ledger quantities (reversal-aware, aggregated across accounts per instrument) are marked with
PIT-safe typed C2C2 prices under an EXPLICIT valuation currency and an explicit field choice (BIST close, Global close, TEFAS fund unit price). The raw closed cash
projection is retained but is NOT investable cash: no RebalanceCurrentState, no CashBucket classification, no FX, no cost basis, no rebalance.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private import backtest_marked_holdings as module_under_test
from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind as K,
    PrivateBacktestInputRequirement as R,
)
from backend.engine.private.backtest_market_data_bridge import build_private_backtest_market_data_context
from backend.engine.private.backtest_market_data_resolution_snapshot import (
    bind_private_backtest_bist_eod_resolution,
    bind_private_backtest_global_eod_resolution,
    bind_private_backtest_precious_metal_resolution,
    bind_private_backtest_tefas_current_metrics_resolution,
    bind_private_backtest_tefas_fund_price_resolution,
)
from backend.engine.private.backtest_market_data_selected_observation import (
    PrivateBacktestMarketDataSelectedObservation,
    reconstruct_private_backtest_selected_observation,
)
from backend.engine.private.backtest_marked_holdings import (
    PrivateBacktestMarkedHoldingsState,
    PrivateBacktestMarkedPosition,
    build_private_backtest_marked_holdings_state,
)
from backend.engine.private.backtest_portfolio_history_coverage import PrivateBacktestPortfolioHistoryCoverageRepository
from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.market_data.models import (
    BISTInstrumentQueryKey,
    GlobalEODQueryKey,
    PreciousMetalSemanticKey,
    TefasFundCurrentMetricsQueryKey,
    TefasFundPriceQueryKey,
)
from backend.engine.private.portfolio.cash import build_cash_balance_projection
from backend.engine.private.portfolio.models import PortfolioTransaction
from backend.engine.private.portfolio.positions import build_position_quantity_projection
from backend.engine.private.precious_metals.constants import PreciousMetalPriceType, PreciousMetalType, PreciousMetalUnit
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_input_bindings import analysis
from backend.tests.test_backtest_input_completeness import cand, make, mkey, pf_key
from backend.tests.test_backtest_portfolio_history_coverage import FakeClient, row_for
from backend.tests.test_backtest_portfolio_projection import bind as bind_portfolio
from backend.tests.test_global_eod_resolver import make_obs as make_global_obs, make_snapshot as make_global_snapshot
from backend.tests.test_market_data_resolver import create_mock_bist_obs, create_mock_bist_snapshot, create_mock_pm_obs, create_mock_pm_snapshot
from backend.tests.test_portfolio_projection import _make_portfolio
from backend.tests.test_tefas_metrics_resolver import create_metrics_snapshot
from backend.tests.test_tefas_price_resolver import make_observation as make_tefas_obs, make_snapshot as make_tefas_snapshot

UTC = timezone.utc
TRY, USD = Currency.TRY, Currency.USD
EVAL = date(2026, 6, 29)
RETRIEVED = datetime(2026, 6, 29, 18, 0, tzinfo=UTC)               # before the replay knowledge cutoff (2026-06-30 12:00 UTC)
OWNER = UUID(int=77)
I1, I2, I3, I4 = (UUID(int=n) for n in (0x1001, 0x1002, 0x1003, 0x1004))
A1, A2 = UUID(int=0xA1), UUID(int=0xA2)
BIST_CLOSE, GLOBAL_CLOSE, GLOBAL_ADJ, TEFAS_PRICE = Decimal("123.456789"), Decimal("190.10"), Decimal("150.00"), Decimal("1.2345")


# --- ledger fixtures -------------------------------------------------------------------------------------------------

def _tx(pf, account, kind, minute, **kw) -> PortfolioTransaction:
    return PortfolioTransaction(portfolio_id=pf.id, account_id=account, transaction_type=kind, effective_date=date(2026, 6, 1),
                                recorded_at=datetime(2026, 6, 10, 9, minute, tzinfo=UTC), id=uuid4(), **kw)


def buy(pf, account, inst, qty, minute, unit_price="1.00"):
    return _tx(pf, account, TransactionType.BUY, minute, instrument_id=inst, quantity=Decimal(qty), unit_price=Decimal(unit_price), trade_currency=USD)


def sell(pf, account, inst, qty, minute):
    return _tx(pf, account, TransactionType.SELL, minute, instrument_id=inst, quantity=Decimal(qty), unit_price=Decimal("1.00"), trade_currency=USD)


def deposit(pf, account, amount, minute, currency=USD):
    return _tx(pf, account, TransactionType.CASH_DEPOSIT, minute, cash_amount=Decimal(amount), cash_currency=currency)


def coverage_for(context, pf, txs):
    binding = bind_portfolio(context, pf, tuple(txs))
    return PrivateBacktestPortfolioHistoryCoverageRepository(client=FakeClient([row_for(binding, OWNER)]), owner_id=OWNER).verify_projection_history(projection_binding=binding)


def standard_ledger(pf, buy_price="1.00"):
    return [
        deposit(pf, A1, "1000000000", 0), deposit(pf, A2, "1000000000", 1), deposit(pf, A1, "500", 2, TRY),
        buy(pf, A1, I1, "10", 3, buy_price), buy(pf, A2, I1, "5.5", 4, buy_price),                  # I1 across two accounts
        buy(pf, A1, I2, "3", 5, buy_price), buy(pf, A1, I3, "100.25", 6, buy_price),
        buy(pf, A1, I4, "7", 7), sell(pf, A1, I4, "7", 8),                                             # I4 is closed: zero quantity
    ]


# --- market fixtures (every snapshot is bound to the analysis context's very replay point) ---------------------------

def mctx(context):
    return build_private_backtest_market_data_context(replay_point=context.replay_point)


def bist_snap(context, inst=I1, close=BIST_CLOSE, trade_date=EVAL, currency=TRY, symbol="THYAO"):
    obs = create_mock_bist_obs(symbol, inst, trade_date, close)
    obs.currency = currency
    snap = create_mock_bist_snapshot(trade_date, RETRIEVED, "b" * 8, [obs])
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=trade_date, symbol=symbol)
    return bind_private_backtest_bist_eod_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,))


def global_snap(context, inst=I2, close=GLOBAL_CLOSE, adj_close=GLOBAL_ADJ, trade_date=EVAL, currency=TRY):
    obs = make_global_obs(inst, "TIINGO", "AAPL", trade_date, close, adj_close=adj_close, currency=currency)
    snap = make_global_snapshot(inst, "TIINGO", "AAPL", RETRIEVED, "g" * 8, observations=[obs])
    key = GlobalEODQueryKey(instrument_id=inst, trade_date=trade_date, provider="TIINGO", provider_symbol="AAPL")
    return bind_private_backtest_global_eod_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,))


def tefas_snap(context, inst=I3, price=TEFAS_PRICE, trade_date=EVAL, currency=TRY):
    obs = make_tefas_obs(inst, trade_date, price, currency=currency)
    snap = make_tefas_snapshot(inst, RETRIEVED, [obs])
    key = TefasFundPriceQueryKey(instrument_id=inst, trade_date=trade_date, provider_symbol="MAC")
    return bind_private_backtest_tefas_fund_price_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,))


def metrics_snap(context, inst=I3):
    snap = create_metrics_snapshot(inst, "MAC", RETRIEVED)
    return bind_private_backtest_tefas_current_metrics_resolution(market_context=mctx(context), query_key=TefasFundCurrentMetricsQueryKey(instrument_id=inst, provider_symbol="MAC"), snapshots=(snap,))


def pm_snap(context):
    from decimal import Decimal as D
    obs = create_mock_pm_obs(PreciousMetalType.GOLD, EVAL, D("7000000.00"), TRY, PreciousMetalUnit.KG, PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=D("995.0"))
    snap = create_mock_pm_snapshot(EVAL, RETRIEVED, "p" * 8, [obs])
    key = PreciousMetalSemanticKey(metal=PreciousMetalType.GOLD, effective_date=EVAL, price_currency=TRY, quantity_unit=PreciousMetalUnit.KG,
                                   price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE, fineness_per_mille=D("995.0"))
    return bind_private_backtest_precious_metal_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,))


def bundle_for(context, coverage, *snapshots):
    reqs = (R(K.PORTFOLIO_HISTORY, pf_key(coverage)),) + tuple(R(K.MARKET_DATA, mkey(s, s.query_key)) for s in snapshots)
    return make(context, reqs, portfolio_history=coverage, market_data=tuple(snapshots))


def world(buy_price="1.00", prices=None):
    """(context, bundle, snapshots-by-instrument): the standard ledger with one exact-date price for each open instrument."""
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    coverage = coverage_for(context, pf, standard_ledger(pf, buy_price))
    snaps = {I1: bist_snap(context), I2: global_snap(context), I3: tefas_snap(context)}
    snaps.update(prices or {})
    return context, bundle_for(context, coverage, *snaps.values()), snaps, pf


def build(bundle, currency=TRY):
    return build_private_backtest_marked_holdings_state(input_bundle=bundle, valuation_currency=currency)


def exact(a, b):
    with localcontext() as c:
        c.prec = 200
        return a * b


def rejects(bundle, currency=TRY, exc=ValueError):
    with pytest.raises(exc):
        build(bundle, currency)


# --- A-D: contracts and exact types -----------------------------------------------------------------------------------

def test_marked_position_is_exactly_four_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestMarkedPosition)
    assert [f.name for f in fs] == ["instrument_id", "quantity", "market_observation", "market_value"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestMarkedPosition.__dataclass_params__.frozen is True


def test_marked_state_is_exactly_five_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestMarkedHoldingsState)
    assert [f.name for f in fs] == ["input_bundle", "valuation_currency", "position_projection", "cash_projection", "marked_positions"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestMarkedHoldingsState.__dataclass_params__.frozen is True


def test_builder_is_keyword_only_without_defaults_and_requires_exact_types() -> None:
    params = inspect.signature(build_private_backtest_marked_holdings_state).parameters
    assert list(params) == ["input_bundle", "valuation_currency"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())
    _, bundle, _, _ = world()
    with pytest.raises(TypeError):
        build_private_backtest_marked_holdings_state(bundle, TRY)  # type: ignore[misc]

    class SubBundle(type(bundle)):
        pass

    sub = SubBundle(**{f.name: getattr(bundle, f.name) for f in dataclasses.fields(bundle)})
    for bad in (sub, object(), None):
        with pytest.raises(TypeError):
            build(bad)
    for bad in ("TRY", None, 1, TRY.value):
        with pytest.raises(TypeError):
            build(bundle, bad)


# --- E-H: admission and surface ---------------------------------------------------------------------------------------

def test_incomplete_bundle_and_wrong_requirement_surface_are_rejected() -> None:
    context, bundle, snaps, pf = world()
    coverage = bundle.portfolio_history
    incomplete = make(context, (R(K.PORTFOLIO_HISTORY, pf_key(coverage)), R(K.MARKET_DATA, "market_data|missing")), portfolio_history=coverage)
    assert incomplete.status is PrivateBacktestInputCompletenessStatus.INCOMPLETE
    rejects(incomplete)
    candidate = cand(context)
    extra = make(context, tuple(bundle.requirements) + (R(K.CANDIDATE_UNIVERSE, f"{candidate.resolution.query.source_key}|{candidate.resolution.query.universe_key}|equity"),),
                 portfolio_history=coverage, market_data=bundle.market_data, candidate_universes=(candidate,))
    assert extra.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    rejects(extra)
    forged = copy.copy(incomplete)
    object.__setattr__(forged, "status", PrivateBacktestInputCompletenessStatus.COMPLETE)
    rejects(forged)


def test_cash_only_portfolio_is_accepted_and_open_holdings_without_prices_are_rejected() -> None:
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    cash_only = bundle_for(context, coverage_for(context, pf, [deposit(pf, A1, "100", 0), deposit(pf, A1, "7", 1, TRY)]))
    state = build(cash_only)
    assert state.marked_positions == () and cash_only.market_data == ()
    assert len(state.cash_projection.balances) == 2 and state.position_projection.open_positions == ()
    holding = coverage_for(context, pf, [deposit(pf, A1, "100", 0), buy(pf, A1, I1, "1", 1)])
    rejects(bundle_for(context, holding))
    with_price = bundle_for(context, coverage_for(context, pf, [deposit(pf, A1, "100", 0)]), bist_snap(context))
    rejects(with_price)                                                           # a price although nothing is held


# --- I-O: closed projections, raw cash, aggregation, open vs closed ----------------------------------------------------

def test_the_closed_projections_are_used_exactly_and_raw_cash_is_preserved() -> None:
    _, bundle, _, _ = world()
    state = build(bundle)
    view = bundle.portfolio_history.projection_binding.projection
    assert state.position_projection == build_position_quantity_projection(view)
    assert state.cash_projection == build_cash_balance_projection(view)
    balances = {(b.account_id, b.currency): b.balance for b in state.cash_projection.balances}
    assert balances[(A1, TRY)] == Decimal("500") and (A2, USD) in balances and (A1, USD) in balances        # multi-currency raw cash untouched, no conversion
    assert state.input_bundle is bundle
    assert not hasattr(state, "investable_cash")
    assert {f.name for f in dataclasses.fields(state)} == {"input_bundle", "valuation_currency", "position_projection", "cash_projection", "marked_positions"}


def test_same_instrument_across_accounts_aggregates_exactly_and_instruments_stay_separate() -> None:
    _, bundle, snaps, _ = world()
    state = build(bundle)
    by_instrument = {p.instrument_id: p for p in state.marked_positions}
    assert by_instrument[I1].quantity == Decimal("15.5")
    assert by_instrument[I2].quantity == Decimal("3") and by_instrument[I3].quantity == Decimal("100.25")
    assert set(by_instrument) == {I1, I2, I3}


def test_closed_zero_quantity_position_needs_no_price_and_a_price_for_it_is_rejected() -> None:
    context, bundle, snaps, pf = world()
    assert I4 in {p.instrument_id for p in build(bundle).position_projection.positions} and I4 not in {p.instrument_id for p in build(bundle).marked_positions}
    coverage = bundle.portfolio_history
    rejects(bundle_for(context, coverage, *snaps.values(), bist_snap(context, inst=I4, symbol="CLOSED")))


# --- P-T: market evidence matching --------------------------------------------------------------------------------------

def test_missing_duplicate_and_extra_prices_are_rejected() -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    rejects(bundle_for(context, coverage, snaps[I1], snaps[I2]))                                              # I3 has no price
    rejects(bundle_for(context, coverage, *snaps.values(), bist_snap(context, inst=I1, symbol="OTHER")))    # two BIST for I1
    rejects(bundle_for(context, coverage, *snaps.values(), global_snap(context, inst=I1)))                   # BIST + Global for I1
    rejects(bundle_for(context, coverage, *snaps.values(), tefas_snap(context, inst=I1)))                    # TEFAS + BIST for I1
    rejects(bundle_for(context, coverage, *snaps.values(), bist_snap(context, inst=UUID(int=0x9999), symbol="ZZZ")))   # unrelated instrument


# --- U-Z: valuation field choice and unsupported kinds ------------------------------------------------------------------

def test_valuation_fields_are_bist_close_global_close_and_tefas_unit_price() -> None:
    _, bundle, snaps, _ = world()
    values = {p.instrument_id: p.market_value for p in build(bundle).marked_positions}
    assert values[I1] == exact(Decimal("15.5"), BIST_CLOSE)
    assert values[I2] == exact(Decimal("3"), GLOBAL_CLOSE) and values[I2] != exact(Decimal("3"), GLOBAL_ADJ)      # adj_close is not chosen
    assert values[I3] == exact(Decimal("100.25"), TEFAS_PRICE)


def test_tefas_current_metrics_and_precious_metal_are_unsupported_for_marking() -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    metrics = metrics_snap(context, inst=I3)
    assert metrics.status.value == "SELECTED"
    rejects(bundle_for(context, coverage, snaps[I1], snaps[I2], metrics))
    pm = pm_snap(context)
    assert pm.status.value == "SELECTED"
    rejects(bundle_for(context, coverage, *snaps.values(), pm))


def test_transaction_unit_price_and_cost_basis_cannot_affect_valuation() -> None:
    _, bundle_a, _, _ = world(buy_price="1.00")
    _, bundle_b, _, _ = world(buy_price="987.65")
    assert [p.market_value for p in build(bundle_a).marked_positions] == [p.market_value for p in build(bundle_b).marked_positions]


# --- AB-AE: positive price policy -------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1.5")])
def test_zero_and_negative_prices_fail_d3a_for_every_kind(bad) -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    rejects(bundle_for(context, coverage, bist_snap(context, close=bad), snaps[I2], snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], global_snap(context, close=bad), snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], snaps[I2], tefas_snap(context, price=bad)))


# --- AF-AI: currency ------------------------------------------------------------------------------------------------------

def test_price_currency_must_equal_the_valuation_currency_without_fx() -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    rejects(bundle, USD)                                                          # all prices are TRY
    rejects(bundle_for(context, coverage, bist_snap(context, currency=USD), snaps[I2], snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], global_snap(context, currency=USD), snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], snaps[I2], tefas_snap(context, currency=USD)))
    all_usd = bundle_for(context, coverage, bist_snap(context, currency=USD), global_snap(context, currency=USD), tefas_snap(context, currency=USD))
    assert [p.market_value for p in build(all_usd, USD).marked_positions][0] == exact(Decimal("15.5"), BIST_CLOSE)     # same value: no conversion factor exists
    assert build(all_usd, USD).valuation_currency is USD


def test_missing_global_or_tefas_currency_fails() -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    rejects(bundle_for(context, coverage, snaps[I1], global_snap(context, currency=None), snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], snaps[I2], tefas_snap(context, currency=None)))


# --- AJ-AM: exact market date -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("offset", [-1, 1, -3, 7])
def test_prices_must_be_for_exactly_the_replay_evaluation_date(offset) -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    other = EVAL + timedelta(days=offset)
    rejects(bundle_for(context, coverage, bist_snap(context, trade_date=other), snaps[I2], snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], global_snap(context, trade_date=other), snaps[I3]))
    rejects(bundle_for(context, coverage, snaps[I1], snaps[I2], tefas_snap(context, trade_date=other)))


# --- AN, AO, AY: evidence identity -------------------------------------------------------------------------------------

def test_market_snapshot_identity_is_preserved_and_cloned_snapshots_are_rejected() -> None:
    _, bundle, snaps, _ = world()
    state = build(bundle)
    by_instrument = {p.instrument_id: p for p in state.marked_positions}
    for inst, snap in snaps.items():
        assert by_instrument[inst].market_observation.resolution_snapshot is snap
    assert state.input_bundle is bundle
    clone = copy.deepcopy(snaps[I1])
    assert clone == snaps[I1] and clone is not snaps[I1]
    forged_position = PrivateBacktestMarkedPosition(instrument_id=I1, quantity=Decimal("15.5"),
                                                    market_observation=reconstruct_private_backtest_selected_observation(resolution_snapshot=clone),
                                                    market_value=exact(Decimal("15.5"), BIST_CLOSE))
    others = tuple(p for p in state.marked_positions if p.instrument_id != I1)
    with pytest.raises(ValueError):
        dataclasses.replace(state, marked_positions=tuple(sorted((forged_position,) + others, key=lambda p: str(p.instrument_id))))
    rewrapped = PrivateBacktestMarkedPosition(instrument_id=I1, quantity=Decimal("15.5"),
                                              market_observation=reconstruct_private_backtest_selected_observation(resolution_snapshot=snaps[I1]),
                                              market_value=exact(Decimal("15.5"), BIST_CLOSE))
    assert dataclasses.replace(state, marked_positions=tuple(sorted((rewrapped,) + others, key=lambda p: str(p.instrument_id)))) == state      # a new wrapper over the SAME snapshot is fine


# --- AP-AS: ordering, exact multiplication, no rounding ------------------------------------------------------------------

def test_marked_positions_are_in_canonical_uuid_order_for_any_input_order() -> None:
    context, bundle, snaps, _ = world()
    coverage = bundle.portfolio_history
    for order in ((I1, I2, I3), (I3, I2, I1), (I2, I3, I1)):
        state = build(bundle_for(context, coverage, *(snaps[i] for i in order)))
        assert [str(p.instrument_id) for p in state.marked_positions] == sorted(str(i) for i in (I1, I2, I3))


def test_market_value_is_exact_and_independent_of_ambient_decimal_precision() -> None:
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    txs = [deposit(pf, A1, "1000000000", 0), buy(pf, A1, I1, "1234.56789012345678", 1)]
    price = Decimal("98.7654321098765432")
    bundle = bundle_for(context, coverage_for(context, pf, txs), bist_snap(context, close=price))
    expected = exact(Decimal("1234.56789012345678"), price)
    assert len(expected.as_tuple().digits) > 28                                       # more digits than the default 28-digit context could hold
    with localcontext() as c:
        c.prec = 2
        c.rounding = "ROUND_DOWN"
        state = build(bundle)
        assert state.marked_positions[0].market_value == expected
        assert state.marked_positions[0].market_value.as_tuple() == expected.as_tuple() or state.marked_positions[0].market_value == expected
    with localcontext() as c:
        c.prec = 2
        position = state.marked_positions[0]
        assert position.market_value == expected                                       # direct construction re-validation is context independent too
        dataclasses.replace(position)
    assert build(bundle).marked_positions[0].market_value == expected                  # no rounding or quantization at any precision


# --- AT-AX: direct construction ------------------------------------------------------------------------------------------

def test_marked_position_direct_construction_validates_types_amounts_and_the_exact_product() -> None:
    _, bundle, snaps, _ = world()
    good = {p.instrument_id: p for p in build(bundle).marked_positions}[I1]
    wrapper = good.market_observation

    class SubWrapper(PrivateBacktestMarketDataSelectedObservation):
        pass

    def make_position(**over):
        base = dict(instrument_id=good.instrument_id, quantity=good.quantity, market_observation=wrapper, market_value=good.market_value)
        base.update(over)
        return PrivateBacktestMarkedPosition(**base)

    assert make_position() == good
    for over, exc in (({"instrument_id": str(I1)}, TypeError), ({"quantity": 15.5}, TypeError), ({"quantity": True}, TypeError), ({"market_value": 1.5}, TypeError),
                      ({"market_observation": object()}, TypeError), ({"market_observation": SubWrapper(resolution_snapshot=snaps[I1])}, TypeError),
                      ({"quantity": Decimal("0")}, ValueError), ({"quantity": Decimal("-1")}, ValueError), ({"quantity": Decimal("NaN")}, ValueError),
                      ({"market_value": Decimal("NaN")}, ValueError), ({"market_value": good.market_value + Decimal("0.000001")}, ValueError),
                      ({"instrument_id": I2}, ValueError)):
        with pytest.raises(exc):
            make_position(**over)


def test_state_direct_construction_rejects_wrong_quantity_value_and_missing_extra_or_reordered_positions() -> None:
    _, bundle, snaps, _ = world()
    state = build(bundle)
    assert dataclasses.replace(state) == state
    positions = state.marked_positions
    with pytest.raises(ValueError):
        dataclasses.replace(state, marked_positions=positions[:-1])
    with pytest.raises(ValueError):
        dataclasses.replace(state, marked_positions=positions + (positions[0],))
    with pytest.raises(ValueError):
        dataclasses.replace(state, marked_positions=tuple(reversed(positions)))
    wrong_quantity = dataclasses.replace(positions[0], quantity=positions[0].quantity + Decimal("1"), market_value=exact(positions[0].quantity + Decimal("1"),
                                                                                                                      build_unit_price(positions[0])))
    with pytest.raises(ValueError):
        dataclasses.replace(state, marked_positions=(wrong_quantity,) + positions[1:])
    with pytest.raises(TypeError):
        dataclasses.replace(state, marked_positions=list(positions))
    with pytest.raises(TypeError):
        dataclasses.replace(state, valuation_currency="TRY")
    with pytest.raises(ValueError):
        dataclasses.replace(state, valuation_currency=USD)                          # prices are TRY: the whole derivation is re-run


def build_unit_price(position):
    representative = position.market_observation.reconstruct()
    return getattr(representative, "close", None) if hasattr(representative, "close") else representative.unit_price


def test_state_direct_construction_rejects_forged_projections_and_a_foreign_bundle() -> None:
    context, bundle, snaps, pf = world()
    state = build(bundle)
    other_ledger = [deposit(pf, A1, "1000000000", 0), buy(pf, A1, I1, "99", 1)]
    other_view = bind_portfolio(context, pf, tuple(other_ledger)).projection
    with pytest.raises(ValueError):
        dataclasses.replace(state, position_projection=build_position_quantity_projection(other_view))
    with pytest.raises(ValueError):
        dataclasses.replace(state, cash_projection=build_cash_balance_projection(other_view))

    class SubPositions(type(state.position_projection)):
        pass

    sub = SubPositions(**{f.name: getattr(state.position_projection, f.name) for f in dataclasses.fields(state.position_projection)})
    with pytest.raises(TypeError):
        dataclasses.replace(state, position_projection=sub)
    with pytest.raises(TypeError):
        dataclasses.replace(state, cash_projection=object())
    with pytest.raises(TypeError):
        dataclasses.replace(state, input_bundle=object())
    equal_projection = build_position_quantity_projection(bundle.portfolio_history.projection_binding.projection)
    assert dataclasses.replace(state, position_projection=equal_projection) == state


# --- AZ-BE: scope guards -------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_marked_holdings.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_no_investable_cash_rebalance_composition_or_other_decision_surface() -> None:
    assert not _names() & {"RebalanceCurrentState", "RebalanceTargetAllocation", "CashBucket", "CashPurpose", "CrossUniverseAuthority", "CrossAssetCompositionPlan",
                           "build_cross_asset_composition_plan", "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan", "CashFirstRebalancePlan",
                           "BandAwareRebalancePlan", "RebalanceBandPolicy", "RebalanceFrictionProfile", "investable_cash", "GameChangerDecisionGate",
                           "PrivateBacktestGameChangerDecisionReplay", "PrivateBacktestCandidateEligibilityReplay", "build_user_return_view_set",
                           "build_bayesian_expected_return_posterior", "optimize_minimum_cvar_portfolio", "scheduler", "dispatcher"}


def test_no_resolver_fx_cost_basis_ledger_transaction_or_base_currency_access() -> None:
    assert not _names() & {"PointInTimeMarketDataResolver", "resolve_bist_eod", "resolve_global_eod", "resolve_tefas_fund_price", "PortfolioTransaction",
                           "active_transactions", "known_transactions", "transactions", "base_currency", "executed_at", "trade_currency", "cash_currency", "fx_rate",
                           "convert", "previous_close", "weighted_average", "adj_close", "reported_current_unit_price", "average_cost", "cost_basis", "realized",
                           "unrealized", "float", "quantize", "round", "Portfolio", "PortfolioAccount", "snapshot_retrieved_at", "knowledge_cutoff",
                           "portfolio_recorded_cutoff"}
    for function in (n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)):
        reads = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)} & {"close", "unit_price"}
        assert not reads or function.name == "_valuation_price", (function.name, reads)


def test_imports_are_only_the_closed_authorities() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_input_completeness", "backend.engine.private.backtest_market_data_resolution_snapshot",
                       "backend.engine.private.backtest_market_data_selected_observation", "backend.engine.private.domain",
                       "backend.engine.private.portfolio.cash", "backend.engine.private.portfolio.positions"}
    for forbidden in ("allocation", "rebalance", "scheduler", "resolver", "fx", "provider", "supabase", "repository", "game_changer", "user_view"):
        assert not any(forbidden in m for m in modules), forbidden


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "json", "loads", "dumps"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_MARKED_HOLDINGS.md").read_text(encoding="utf-8")
    for needle in ("valuation_currency", "not claimed", "BIST close", "Global close", "TEFAS unit price", "adj_close", "diagnostic", "unsupported", "evaluation date",
                   "no previous-day fallback", "positive price", "aggregat", "exact arithmetic", "raw cash", "NOT investable cash", "CashBucket", "no RebalanceCurrentState",
                   "no FX", "no cost basis", "no rebalance", "D3B"):
        assert needle in doc, needle
    selected = (root / "docs" / "PRIVATE_BACKTEST_MARKET_DATA_SELECTED_OBSERVATION.md").read_text(encoding="utf-8")
    assert "C2C2-R1 is now consumed by D3A marked-holdings construction" in selected
    assert "C2C2 selects/reconstructs observations; D3A owns the explicit valuation-field choice" in selected
    assert "starts only after an independent Red Team of C2C2" not in selected
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_marked_holdings.py" in architecture
