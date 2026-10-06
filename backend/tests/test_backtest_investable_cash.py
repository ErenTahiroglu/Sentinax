"""
backend/tests/test_backtest_investable_cash.py
==============================================
Phase 26D3B: explicit fixed-replay investable-cash allocation over the D3A raw cash projection. Every positive raw (account, currency) balance needs exactly one explicit caller
decision (zero is a decision, never "missing"); an amount may not exceed the raw balance; a non-valuation currency must be zero (no FX). The allocations are conditional /
counterfactual policy parameters: current mutable cash_buckets rows are not historical authority, so no CashBucket, transaction bucket, risk-evidence or database input exists here.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private import backtest_investable_cash as module_under_test
from backend.engine.private.backtest_investable_cash import (
    PrivateBacktestCashAllocation,
    PrivateBacktestInvestableCashSelection,
    build_private_backtest_investable_cash_selection,
)
from backend.engine.private.backtest_marked_holdings import PrivateBacktestMarkedHoldingsState
from backend.engine.private.domain import Currency
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_input_bindings import analysis
from backend.tests.test_backtest_marked_holdings import A1, A2, I1, OWNER, bist_snap, bundle_for, buy, build, coverage_for, deposit
from backend.tests.test_portfolio_projection import _make_portfolio

TRY, USD, EUR = Currency.TRY, Currency.USD, Currency.EUR
D = Decimal
A3 = UUID(int=0xA3)


def cash_state(*balances, valuation=TRY) -> PrivateBacktestMarkedHoldingsState:
    """A real D3A state whose raw cash is exactly the given (account, currency, amount) deposits; no holdings."""
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    txs = [deposit(pf, account, amount, minute, currency) for minute, (account, currency, amount) in enumerate(balances)]
    return build(bundle_for(context, coverage_for(context, pf, txs)), valuation)


def zero_balance_state() -> PrivateBacktestMarkedHoldingsState:
    """A real D3A state with a ZERO raw USD balance (retained for audit) next to a positive TRY balance."""
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    txs = [deposit(pf, A1, "100", 0, TRY), deposit(pf, A2, "100", 1, USD), buy(pf, A2, I1, "100", 2)]              # the buy spends all 100 USD
    return build(bundle_for(context, coverage_for(context, pf, txs), bist_snap(context)), TRY)


def alloc(account, currency, amount) -> PrivateBacktestCashAllocation:
    return PrivateBacktestCashAllocation(account_id=account, currency=currency, investable_amount=D(amount))


def select(state, *allocations):
    return build_private_backtest_investable_cash_selection(marked_holdings_state=state, allocations=tuple(allocations))


def rejects(state, *allocations, exc=ValueError) -> None:
    with pytest.raises(exc):
        select(state, *allocations)


STANDARD = (("A1", TRY, "100"), ("A2", TRY, "50"), ("A1", USD, "30"))


def standard_state():
    return cash_state((A1, TRY, "100"), (A2, TRY, "50"), (A1, USD, "30"))


# --- A-D: contracts -----------------------------------------------------------------------------------------------------

def test_allocation_is_exactly_three_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestCashAllocation)
    assert [f.name for f in fs] == ["account_id", "currency", "investable_amount"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestCashAllocation.__dataclass_params__.frozen is True


def test_selection_is_exactly_two_frozen_fields_and_stores_no_total() -> None:
    fs = dataclasses.fields(PrivateBacktestInvestableCashSelection)
    assert [f.name for f in fs] == ["marked_holdings_state", "allocations"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestInvestableCashSelection.__dataclass_params__.frozen is True
    assert isinstance(PrivateBacktestInvestableCashSelection.investable_cash, property)           # derived, read-only, never a stored (forgeable) field
    assert not {"investable_cash", "valuation_currency", "raw_balance", "portfolio_id", "total", "current_state"} & {f.name for f in fs}


def test_builder_is_keyword_only_without_defaults() -> None:
    params = inspect.signature(build_private_backtest_investable_cash_selection).parameters
    assert list(params) == ["marked_holdings_state", "allocations"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())


def test_exact_type_discipline_and_no_coercion() -> None:
    state = standard_state()
    good = (alloc(A1, TRY, "0"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"))

    class SubState(PrivateBacktestMarkedHoldingsState):
        pass

    class SubAllocation(PrivateBacktestCashAllocation):
        pass

    class SubTuple(tuple):
        pass

    sub_state = SubState(**{f.name: getattr(state, f.name) for f in dataclasses.fields(state)})
    for bad in (sub_state, object(), None):
        with pytest.raises(TypeError):
            build_private_backtest_investable_cash_selection(marked_holdings_state=bad, allocations=good)  # type: ignore[arg-type]
    sub_alloc = SubAllocation(account_id=A1, currency=TRY, investable_amount=D("0"))
    for bad in (list(good), set(good), iter(good), SubTuple(good), (sub_alloc,) + good[1:], good + (object(),), None):
        with pytest.raises(TypeError):
            build_private_backtest_investable_cash_selection(marked_holdings_state=state, allocations=bad)  # type: ignore[arg-type]
    for kw in ({"account_id": str(A1)}, {"account_id": 1}, {"currency": "TRY"}, {"currency": None}, {"investable_amount": 5}, {"investable_amount": 5.0},
               {"investable_amount": "5"}, {"investable_amount": True}):
        base = dict(account_id=A1, currency=TRY, investable_amount=D("5"))
        base.update(kw)
        with pytest.raises(TypeError):
            PrivateBacktestCashAllocation(**base)


# --- E-H: allocation amount ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [D("-1"), D("-0.01"), D("-0"), D("NaN"), D("Infinity"), D("-Infinity"), D("sNaN")])
def test_allocation_amount_must_be_finite_and_unsigned(bad) -> None:
    with pytest.raises(ValueError):
        PrivateBacktestCashAllocation(account_id=A1, currency=TRY, investable_amount=bad)


def test_zero_and_positive_amounts_are_valid_allocations() -> None:
    for amount in ("0", "0.0", "0.000", "1", "123456789.123456789"):
        assert PrivateBacktestCashAllocation(account_id=A1, currency=TRY, investable_amount=D(amount)).investable_amount == D(amount)


# --- I-Q: coverage and ceilings --------------------------------------------------------------------------------------------

def test_every_positive_raw_balance_requires_exactly_one_allocation() -> None:
    state = standard_state()
    assert len(state.cash_projection.positive_balances) == 3
    selection = select(state, alloc(A1, TRY, "0"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"))
    assert len(selection.allocations) == 3
    rejects(state, alloc(A1, TRY, "20"), alloc(A2, TRY, "50"))                                                  # the USD balance has no decision
    rejects(state, alloc(A1, TRY, "20"))
    rejects(state)                                                                                              # no implicit zero for any missing row
    rejects(state, alloc(A1, TRY, "20"), alloc(A1, TRY, "20"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"))         # duplicate key, never first-wins
    rejects(state, alloc(A1, TRY, "20"), alloc(A1, TRY, "30"), alloc(A1, USD, "0"))
    rejects(state, alloc(A1, TRY, "0"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"), alloc(A3, TRY, "0"))          # unknown account: extra
    rejects(state, alloc(A1, TRY, "0"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"), alloc(A2, USD, "0"))          # unknown (account, currency) pair: extra
    rejects(state, alloc(A1, TRY, "0"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"), alloc(A1, EUR, "0"))


def test_zero_raw_balances_are_retained_for_audit_but_take_no_allocation() -> None:
    state = zero_balance_state()
    balances = {(b.account_id, b.currency): b.balance for b in state.cash_projection.balances}
    assert balances[(A2, USD)] == D("0") and (A2, USD) not in {(b.account_id, b.currency) for b in state.cash_projection.positive_balances}
    selection = select(state, alloc(A1, TRY, "40"))
    assert selection.investable_cash == D("40")
    rejects(state, alloc(A1, TRY, "40"), alloc(A2, USD, "0"))                                                    # an allocation for a zero balance is extra evidence
    rejects(state, alloc(A1, TRY, "40"), alloc(A2, USD, "5"))


def test_zero_partial_full_and_excess_amounts_against_the_raw_balance() -> None:
    state = cash_state((A1, TRY, "100"))
    for amount in ("0", "40", "99.999999999999999999", "100", "100.00"):
        assert select(state, alloc(A1, TRY, amount)).investable_cash == D(amount)
    for amount in ("100.01", "100.000000000000000001", "1000"):
        rejects(state, alloc(A1, TRY, amount))


def test_same_currency_in_two_accounts_is_classified_separately_and_not_aggregated_first() -> None:
    state = cash_state((A1, TRY, "100"), (A2, TRY, "50"))
    selection = select(state, alloc(A1, TRY, "20"), alloc(A2, TRY, "50"))
    assert selection.investable_cash == D("70")
    rejects(state, alloc(A1, TRY, "120"))                                                                       # one pooled decision is not allowed
    rejects(state, alloc(A1, TRY, "70"), alloc(A2, TRY, "60"))                                                  # per-account ceiling holds although the total fits


# --- R, S: canonical order and identity -----------------------------------------------------------------------------------

def test_canonical_order_is_independent_of_caller_order_and_identities_are_preserved() -> None:
    state = standard_state()
    a, b, c = alloc(A1, TRY, "20"), alloc(A1, USD, "0"), alloc(A2, TRY, "50")
    import itertools
    reference = None
    for order in itertools.permutations((a, b, c)):
        selection = select(state, *order)
        assert [(x.account_id, x.currency.value) for x in selection.allocations] == sorted(
            [(A1, "TRY"), (A1, "USD"), (A2, "TRY")], key=lambda k: (str(k[0]), k[1]))
        assert {id(x) for x in selection.allocations} == {id(a), id(b), id(c)}
        assert selection.marked_holdings_state is state
        reference = reference or selection
        assert selection == reference
    assert reference.allocations == (a, b, c)


# --- U-Y: currency rule, no FX ---------------------------------------------------------------------------------------------

def test_valuation_currency_amount_may_be_positive_and_foreign_currency_must_be_zero() -> None:
    state = standard_state()
    ok = select(state, alloc(A1, TRY, "100"), alloc(A1, USD, "0"), alloc(A2, TRY, "50"))
    assert ok.investable_cash == D("150")
    rejects(state, alloc(A1, TRY, "20"), alloc(A1, USD, "0.01"), alloc(A2, TRY, "50"))                            # foreign cash cannot be made investable
    rejects(state, alloc(A1, TRY, "20"), alloc(A1, USD, "30"), alloc(A2, TRY, "50"))
    assert select(state, alloc(A1, TRY, "20"), alloc(A1, USD, "0.000"), alloc(A2, TRY, "50")).investable_cash == D("20") + D("50")   # explicit foreign zero accepted


def test_foreign_raw_cash_stays_visible_unconverted_and_foreign_zero_never_adds_to_the_total() -> None:
    state = standard_state()
    selection = select(state, alloc(A1, TRY, "10"), alloc(A1, USD, "0"), alloc(A2, TRY, "5"))
    raw = {(b.account_id, b.currency): b.balance for b in selection.marked_holdings_state.cash_projection.balances}
    assert raw == {(A1, TRY): D("100"), (A2, TRY): D("50"), (A1, USD): D("30")}
    assert selection.investable_cash == D("15")
    usd_state = cash_state((A1, USD, "100"), (A1, TRY, "7"), valuation=USD)                                       # the rule follows the explicit valuation currency
    assert select(usd_state, alloc(A1, USD, "60"), alloc(A1, TRY, "0")).investable_cash == D("60")
    rejects(usd_state, alloc(A1, USD, "60"), alloc(A1, TRY, "1"))


# --- Z, AA, AB: empty and zero ----------------------------------------------------------------------------------------------

def test_no_positive_cash_requires_empty_allocations_and_gives_zero() -> None:
    context = analysis()
    pf = _make_portfolio(owner_id=OWNER)
    empty = build(bundle_for(context, coverage_for(context, pf, [])), TRY)
    assert empty.cash_projection.positive_balances == ()
    selection = select(empty)
    assert selection.allocations == () and selection.investable_cash == D("0") and selection.marked_holdings_state is empty
    rejects(empty, alloc(A1, TRY, "0"))
    rejects(empty, alloc(A1, TRY, "5"))


def test_all_explicit_zero_allocations_give_zero_investable_cash_which_is_not_missing() -> None:
    state = standard_state()
    selection = select(state, alloc(A1, TRY, "0"), alloc(A1, USD, "0"), alloc(A2, TRY, "0"))
    assert selection.investable_cash == D("0") and len(selection.allocations) == 3
    with pytest.raises(ValueError):
        select(state)                                                                                         # while omitting the decisions is rejected


# --- AC, AD: exact sum -----------------------------------------------------------------------------------------------------

def test_exact_derived_sum_and_independence_from_ambient_decimal_precision() -> None:
    big = ("1234567890123456789012345.123456789012345678", "0.000000000000000000000000001", "98765432109876543210.5")
    state = cash_state((A1, TRY, "9999999999999999999999999999.999999999999999999999999999"), (A2, TRY, "99999999999999999999999.9"), (A3, TRY, "1"))
    allocations = (alloc(A1, TRY, big[0]), alloc(A2, TRY, big[2]), alloc(A3, TRY, big[1]) if D(big[1]) <= D("1") else alloc(A3, TRY, "1"))
    with localcontext() as c:
        c.prec = 200
        expected = D(big[0]) + D(big[2]) + D(big[1])
    assert len(expected.as_tuple().digits) > 28
    with localcontext() as c:
        c.prec = 2
        c.rounding = "ROUND_DOWN"
        selection = select(state, *allocations)
        assert selection.investable_cash == expected
        assert dataclasses.replace(selection).investable_cash == expected
    assert select(state, *allocations).investable_cash == expected                                                # no rounding or quantization at any precision


# --- AE, AF: no stored total, identity -----------------------------------------------------------------------------------------

def test_investable_cash_is_a_read_only_derived_property() -> None:
    selection = select(standard_state(), alloc(A1, TRY, "20"), alloc(A1, USD, "0"), alloc(A2, TRY, "5"))
    assert selection.investable_cash == D("25")
    with pytest.raises(dataclasses.FrozenInstanceError):
        selection.investable_cash = D("1000")  # type: ignore[misc]
    assert "investable_cash" not in vars(selection)
    assert "investable_cash" not in selection.__dataclass_fields__
    assert selection.marked_holdings_state is selection.marked_holdings_state


# --- AG-AI: direct construction ---------------------------------------------------------------------------------------------

def test_direct_construction_reruns_the_same_validation_and_never_trusts_order_or_totals() -> None:
    state = standard_state()
    a, b, c = alloc(A1, TRY, "20"), alloc(A1, USD, "0"), alloc(A2, TRY, "50")
    good = PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=(a, b, c))
    assert good == select(state, c, a, b)
    for bad in ((b, a, c), (c, b, a), (a, c, b)):
        with pytest.raises(ValueError):
            PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=bad)                  # noncanonical order
    with pytest.raises(ValueError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=(a, b))                  # missing
    with pytest.raises(ValueError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=(a, a, b, c))           # duplicate key
    with pytest.raises(ValueError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=(a, b, c, alloc(A3, TRY, "0")))        # extra
    with pytest.raises(ValueError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=(alloc(A1, TRY, "101"), b, c))          # above the raw balance
    with pytest.raises(ValueError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=(a, alloc(A1, USD, "1"), c))            # foreign positive
    with pytest.raises(TypeError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=state, allocations=[a, b, c])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PrivateBacktestInvestableCashSelection(marked_holdings_state=object(), allocations=(a, b, c))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dataclasses.replace(good, allocations=(a, b))
    other_state = cash_state((A1, TRY, "100"), (A2, TRY, "50"), (A1, USD, "30"))
    assert dataclasses.replace(good, marked_holdings_state=other_state).marked_holdings_state is other_state        # an equally-shaped state is fine ...
    with pytest.raises(ValueError):
        dataclasses.replace(good, marked_holdings_state=cash_state((A1, TRY, "10"), (A2, TRY, "50"), (A1, USD, "30")))  # ... a different raw ledger is not


# --- AJ-AS: scope guards -----------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_TREE = ast.parse(_PATH.read_text(encoding="utf-8"))
_REL = "backend/engine/private/backtest_investable_cash.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_no_cash_bucket_transaction_risk_evidence_database_or_fx_surface() -> None:
    assert not _names() & {"CashBucket", "CashPurpose", "cash_bucket_id", "included_in_investable_assets", "archived_at", "PortfolioTransaction", "active_transactions",
                           "known_transactions", "transactions", "RiskEvidenceKind", "CashBalanceRiskFact", "DecodedCashBalanceRiskEvidence", "risk_evidence",
                           "client", "table", "rpc", "supabase", "repository", "PostgREST", "fx_rate", "convert", "base_currency", "float", "round", "quantize"}
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_marked_holdings", "backend.engine.private.domain"}
    for forbidden in ("risk_evidence", "supabase", "repository", "fx", "provider", "resolver", "market_data", "allocation", "rebalance", "scheduler"):
        assert not any(forbidden in m for m in modules), forbidden


def test_holdings_prices_and_other_decision_surfaces_are_not_touched() -> None:
    assert not _names() & {"marked_positions", "market_observation", "market_data", "position_projection", "input_bundle", "PointInTimeMarketDataResolver",
                           "RebalanceCurrentState", "RebalanceTargetAllocation", "CrossUniverseAuthority", "build_cross_asset_composition_plan",
                           "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan", "GameChangerDecisionGate", "build_user_return_view_set",
                           "optimize_minimum_cvar_portfolio", "scheduler", "dispatcher"}


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "environ", "getenv", "sleep", "datetime", "json", "loads", "dumps"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_INVESTABLE_CASH.md").read_text(encoding="utf-8")
    for needle in ("conditional", "counterfactual", "fixed replay", "raw cash", "not investable cash", "cash_buckets", "included_in_investable_assets", "archived_at",
                   "no revision", "every positive", "zero amount", "cannot exceed", "non-valuation currency", "no FX", "no CashBucket inference", "no transaction bucket",
                   "no risk-evidence substitution", "exact arithmetic", "does not build RebalanceCurrentState", "D3C", "Red Team"):
        assert needle in doc, needle
    marked = (root / "docs" / "PRIVATE_BACKTEST_MARKED_HOLDINGS.md").read_text(encoding="utf-8")
    assert "D3B uses explicit fixed replay cash allocations because the current mutable CashBucket lifecycle does not provide a PIT-safe classification history" in marked
    assert "D3B remains counterfactual and does not claim the policy was historically used" in marked
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_investable_cash.py" in architecture
