"""
backend/tests/test_backtest_portfolio_projection.py
===================================================
Phase 26C2B1: the canonical portfolio projection at the replay knowledge cutoff. The projection is derived internally by the CLOSED `build_ledger_projection_view`
from a supplied Portfolio and a supplied transaction tuple; a caller can never inject a LedgerProjectionView. Claim limit: canonical derivation from the supplied
tuple, not completeness of that tuple relative to persistent storage.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engine.private import backtest_portfolio_projection as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext, build_private_backtest_analysis_context
from backend.engine.private.backtest_portfolio_projection import PrivateBacktestPortfolioProjectionBinding, build_private_backtest_portfolio_projection
from backend.engine.private.backtest_replay_point import build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode, Horizon, PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.projection import LedgerProjectionView, PortfolioProjectionError
from backend.tests.invariants import static_guards as sg
from backend.tests.test_portfolio_projection import _make_portfolio, _make_tx

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
T0, T1, T2 = (datetime(2026, 8, d, 12, 0, tzinfo=UTC) for d in (1, 10, 20))
EVAL = date(2026, 8, 10)


def analysis(cutoff=T1, mode=AsOfMode.SOURCE_AS_OF, evaluation_date=EVAL) -> PrivateBacktestAnalysisContext:
    point = build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)
    return build_private_backtest_analysis_context(replay_point=point, horizon=Horizon.ALLOCATION_12M)


def bind(context, portfolio, transactions) -> PrivateBacktestPortfolioProjectionBinding:
    return build_private_backtest_portfolio_projection(analysis_context=context, portfolio=portfolio, transactions=transactions)


def buy(portfolio, account, recorded_at, effective_date=date(2026, 8, 1), **kw) -> PortfolioTransaction:
    return _make_tx(portfolio.id, account, tx_type=TransactionType.BUY, recorded_at=recorded_at, effective_date=effective_date, **kw)


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_shape_constructor_and_builder_signatures() -> None:
    fields = dataclasses.fields(PrivateBacktestPortfolioProjectionBinding)
    assert [f.name for f in fields] == ["analysis_context", "transactions", "projection"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert PrivateBacktestPortfolioProjectionBinding.__dataclass_params__.frozen is True
    for forbidden in ("portfolio", "owner_id", "portfolio_name", "portfolio_mode", "portfolio_id", "cutoff", "completeness", "coverage"):
        assert forbidden not in {f.name for f in fields}
    for callable_, skip in ((PrivateBacktestPortfolioProjectionBinding.__init__, 1), (build_private_backtest_portfolio_projection, 0)):
        parameters = list(inspect.signature(callable_).parameters.values())[skip:]
        assert [p.name for p in parameters] == ["analysis_context", "portfolio", "transactions"]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)


def test_forged_projection_and_foreign_arguments_cannot_enter() -> None:
    context, portfolio = analysis(), _make_portfolio()
    view = LedgerProjectionView(portfolio_id=portfolio.id, mode=portfolio.mode, as_of_recorded_at=T1, known_transactions=(), transaction_states=(), active_transactions=())
    for extra in ("projection", "cutoff", "portfolio_id", "mode", "owner_id"):
        value = view if extra == "projection" else 1
        with pytest.raises(TypeError):
            build_private_backtest_portfolio_projection(analysis_context=context, portfolio=portfolio, transactions=(), **{extra: value})  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            PrivateBacktestPortfolioProjectionBinding(analysis_context=context, portfolio=portfolio, transactions=(), **{extra: value})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PrivateBacktestPortfolioProjectionBinding(analysis_context=context, transactions=(), projection=view)  # type: ignore[call-arg]


def test_exact_types_everywhere() -> None:
    class SubContext(PrivateBacktestAnalysisContext):
        pass

    class SubPortfolio(Portfolio):
        pass

    class SubTx(PortfolioTransaction):
        pass

    class SubTuple(tuple):
        pass

    context, portfolio = analysis(), _make_portfolio()
    account = uuid4()
    tx = buy(portfolio, account, T0)
    sub_context = SubContext(replay_point=context.replay_point, temporal_context=context.temporal_context)
    sub_portfolio = SubPortfolio(**{f.name: getattr(portfolio, f.name) for f in dataclasses.fields(portfolio)})
    sub_tx = object.__new__(SubTx)
    for f in dataclasses.fields(tx):
        object.__setattr__(sub_tx, f.name, getattr(tx, f.name))
    for bad in (None, object(), sub_context, context.temporal_context, context.replay_point):
        with pytest.raises(TypeError):
            bind(bad, portfolio, ())
    for bad in (None, object(), sub_portfolio, "portfolio"):
        with pytest.raises(TypeError):
            bind(context, bad, ())
    for bad in (None, [], [tx], iter(()), {tx}, SubTuple((tx,)), "x"):
        with pytest.raises(TypeError):
            bind(context, portfolio, bad)
    for member in (None, object(), sub_tx, "tx"):
        with pytest.raises(TypeError):
            bind(context, portfolio, (tx, member))
    assert bind(context, portfolio, ()).transactions == ()


def test_identity_and_cutoff_representation_are_preserved() -> None:
    cutoff = datetime(2026, 8, 10, 15, 0, tzinfo=PLUS3)                             # 12:00 UTC
    context, portfolio = analysis(cutoff=cutoff), _make_portfolio()
    transactions = (buy(portfolio, uuid4(), T0),)
    binding = bind(context, portfolio, transactions)
    assert binding.analysis_context is context and binding.transactions is transactions and type(binding.projection) is LedgerProjectionView
    assert binding.projection.as_of_recorded_at is context.replay_point.portfolio_recorded_cutoff is cutoff
    assert binding.projection.as_of_recorded_at.utcoffset() == timedelta(hours=3)
    assert binding.projection.known_transactions == transactions and binding.projection.active_transactions == transactions
    assert PrivateBacktestPortfolioProjectionBinding(analysis_context=context, portfolio=portfolio, transactions=transactions) == binding


# --- recorded-time authority ---------------------------------------------------------------------------------------

def test_future_transaction_cannot_leak_backward_and_the_full_tuple_is_passed() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    a, b = buy(portfolio, account, T0), buy(portfolio, account, T2)
    binding = bind(analysis(cutoff=T1), portfolio, (a, b))
    assert binding.projection.known_transactions == (a,) and binding.projection.active_transactions == (a,)
    assert binding.transactions == (a, b)                                          # nothing was prefiltered
    assert bind(analysis(cutoff=T2), portfolio, (a, b)).projection.known_transactions == (a, b)


def test_future_reversal_does_not_retroactively_affect_an_earlier_replay() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    base = buy(portfolio, account, T0)
    reversal = _make_tx(portfolio.id, account, tx_type=TransactionType.REVERSAL, recorded_at=T2, reverses_tx_id=base.id)
    at_t1 = bind(analysis(cutoff=T1), portfolio, (base, reversal)).projection
    assert at_t1.active_transactions == (base,) and at_t1.transaction_states[0].is_reversed is False and at_t1.known_transactions == (base,)
    assert reversal not in at_t1.known_transactions
    for cutoff in (T2, T2 + timedelta(days=5)):
        later = bind(analysis(cutoff=cutoff), portfolio, (base, reversal)).projection
        assert later.transaction_states[0].is_reversed is True and later.active_transactions == () and reversal in later.known_transactions


def test_effective_date_does_not_gate_recorded_knowledge() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    context = analysis(cutoff=T1, evaluation_date=date(2026, 8, 10))
    future_effective_known = buy(portfolio, account, T0, effective_date=date(2026, 12, 31))
    old_effective_unknown = buy(portfolio, account, T2, effective_date=date(2020, 1, 1))
    projection = bind(context, portfolio, (future_effective_known, old_effective_unknown)).projection
    assert future_effective_known.effective_date > context.replay_point.evaluation_date
    assert projection.known_transactions == (future_effective_known,)


def test_non_utc_cutoff_includes_a_transaction_recorded_at_the_same_instant() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    cutoff = datetime(2026, 8, 10, 15, 0, tzinfo=PLUS3)
    same = buy(portfolio, account, datetime(2026, 8, 10, 12, 0, tzinfo=UTC))
    after = buy(portfolio, account, datetime(2026, 8, 10, 12, 0, 0, 1, tzinfo=UTC))
    projection = bind(analysis(cutoff=cutoff), portfolio, (same, after)).projection
    assert projection.known_transactions == (same,) and projection.as_of_recorded_at is cutoff


def test_empty_history_and_both_portfolio_modes() -> None:
    projection = bind(analysis(), _make_portfolio(), ()).projection
    assert projection.known_transactions == () and projection.transaction_states == () and projection.active_transactions == ()
    for mode in (PortfolioMode.MY_PORTFOLIO, PortfolioMode.SANDBOX):
        portfolio = _make_portfolio(mode=mode)
        view = bind(analysis(), portfolio, ()).projection
        assert view.mode is mode and view.portfolio_id == portfolio.id


def test_caller_order_is_preserved_and_projection_order_is_canonical() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    first, second, third = buy(portfolio, account, T0), buy(portfolio, account, T0 + timedelta(days=1)), buy(portfolio, account, T0 + timedelta(days=2))
    original = (third, first, second)
    binding = bind(analysis(cutoff=T2), portfolio, original)
    assert binding.transactions is original and original == (third, first, second)
    assert binding.projection.known_transactions == (first, second, third)           # the closed builder's audit ordering, not a B1 sort


# --- corruption / mutability / completeness ------------------------------------------------------------------------

def test_corrupted_history_fails_closed_through_the_existing_builder() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    context = analysis(cutoff=T2)
    base = buy(portfolio, account, T0)
    duplicate = buy(portfolio, account, T0, id=base.id)
    with pytest.raises(PortfolioProjectionError):
        bind(context, portfolio, (base, duplicate))
    other_portfolio = _make_portfolio()
    foreign = buy(other_portfolio, account, T0)
    with pytest.raises(PortfolioProjectionError):
        bind(context, portfolio, (foreign,))
    orphan = _make_tx(portfolio.id, account, tx_type=TransactionType.REVERSAL, recorded_at=T1, reverses_tx_id=uuid4())
    with pytest.raises(PortfolioProjectionError):
        bind(context, portfolio, (orphan,))
    r1 = _make_tx(portfolio.id, account, tx_type=TransactionType.REVERSAL, recorded_at=T1, reverses_tx_id=base.id)
    r2 = _make_tx(portfolio.id, account, tx_type=TransactionType.REVERSAL, recorded_at=T2, reverses_tx_id=base.id)
    with pytest.raises(PortfolioProjectionError):
        bind(context, portfolio, (base, r1, r2))
    cross_account = _make_tx(portfolio.id, uuid4(), tx_type=TransactionType.REVERSAL, recorded_at=T1, reverses_tx_id=base.id)
    with pytest.raises(PortfolioProjectionError):
        bind(context, portfolio, (base, cross_account))


def test_a_mutated_invalid_portfolio_is_rejected_before_a_binding_exists() -> None:
    portfolio = _make_portfolio()
    portfolio.name = "   "
    with pytest.raises(ValueError):
        bind(analysis(), portfolio, ())
    portfolio.name = "Restored"
    portfolio.mode = PortfolioMode.MY_PORTFOLIO
    portfolio.source_portfolio_id = uuid4()
    with pytest.raises(ValueError):
        bind(analysis(), portfolio, ())


def test_post_binding_portfolio_mutation_cannot_alter_the_stored_projection() -> None:
    portfolio = _make_portfolio()
    binding = bind(analysis(), portfolio, ())
    before = (binding.projection.portfolio_id, binding.projection.mode)
    portfolio.id = uuid4()
    portfolio.mode = PortfolioMode.SANDBOX
    portfolio.name = ""
    assert (binding.projection.portfolio_id, binding.projection.mode) == before
    assert not any(value is portfolio for value in vars(binding).values())


def test_a_subset_of_history_is_as_derivable_as_the_full_tuple() -> None:
    portfolio, account = _make_portfolio(), uuid4()
    a, b = buy(portfolio, account, T0), buy(portfolio, account, T0 + timedelta(days=1))
    context = analysis(cutoff=T2)
    full = bind(context, portfolio, (a, b)).projection
    subset = bind(context, portfolio, (a,)).projection
    assert full.known_transactions == (a, b) and subset.known_transactions == (a,)           # canonical derivation != history completeness


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_portfolio_projection.py"


def test_module_is_deliberately_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_imports_are_only_the_allowed_modules() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_analysis_context", "backend.engine.private.portfolio.models",
                       "backend.engine.private.portfolio.projection"}
    assert "backend.engine.private.portfolio.projection" in modules
    for forbidden in ("repository", "accounting", "fee_tax", "market_data", "scheduler", "provider", "macro", "game_changer", "risk", "user_view",
                      "supabase", "postgrest"):
        assert not any(forbidden in m for m in modules), forbidden
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]


def test_projection_is_derived_once_by_the_closed_builder_with_the_replay_cutoff() -> None:
    calls = [n for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "build_ledger_projection_view"]
    assert len(calls) == 1
    keywords = {k.arg: k.value for k in calls[0].keywords}
    assert set(keywords) == {"as_of_recorded_at"} and isinstance(keywords["as_of_recorded_at"], ast.Attribute) and keywords["as_of_recorded_at"].attr == "portfolio_recorded_cutoff"
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)})
    assert not names & {"knowledge_cutoff_utc", "evaluation_date", "now", "utcnow", "today", "time", "uuid4", "random", "secrets", "urandom", "sha256", "hashlib",
                        "open", "client", "rpc", "table", "sorted", "sort", "filter", "recorded_at", "economic_fingerprint", "COMPLETE_AT_CUTOFF",
                        "history_coverage", "rebalance", "optimizer"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]


def test_documentation_states_the_claim_limit() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_PORTFOLIO_PROJECTION.md").read_text(encoding="utf-8")
    for needle in ("recorded_at", "economic time", "knowledge time", "cannot inject", "mutable", "frozen", "future reversals", "does NOT prove", "C2B2"):
        assert needle in doc, needle
