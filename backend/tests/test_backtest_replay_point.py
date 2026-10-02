"""
backend/tests/test_backtest_replay_point.py
===========================================
Phase 26A1: the canonical historical replay point. One immutable pairing of the CLOSED AnalysisPITContext (knowledge frontier and external-data perspective) with
an explicit economic evaluation date. It composes with the existing PIT authorities (market-data resolver, ledger projection, candidate universe, Game
Changer event binding) and reimplements none of them. Pure; no execution, no performance, no loop.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engine.private import backtest_replay_point as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint, build_private_backtest_replay_point
from backend.engine.private.allocation_candidate_universe import CandidateUniverseResolutionStatus, CandidateUniverseQuery, resolve_candidate_universe
from backend.engine.private.domain import AsOfMode, AssetClass, TransactionType
from backend.engine.private.game_changer_event import bind_game_changer_event_pit
from backend.engine.private.portfolio.projection import build_ledger_projection_view
from backend.tests.invariants import static_guards as sg
from backend.tests.test_allocation_candidate_universe import SRC, UA, UNI, snap
from backend.tests.test_game_changer_event import event as gc_event
from backend.tests.test_portfolio_projection import _make_portfolio, _make_tx

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
CUTOFF = datetime(2026, 9, 10, 10, 0, tzinfo=PLUS3)             # 07:00 UTC
EVAL = date(2026, 9, 9)


def point(mode=SO, cutoff=CUTOFF, evaluation_date=EVAL) -> PrivateBacktestReplayPoint:
    return build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_fields_are_exactly_two_frozen_without_defaults() -> None:
    fields = dataclasses.fields(PrivateBacktestReplayPoint)
    assert [f.name for f in fields] == ["pit_context", "evaluation_date"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    assert PrivateBacktestReplayPoint.__dataclass_params__.frozen is True
    p = point()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.evaluation_date = date(2026, 1, 1)  # type: ignore[misc]
    for forbidden in ("id", "uuid", "hash", "sha256", "step_number", "created_at", "execution_at", "outcome_at", "price_date", "replay_id", "market_data_mode", "market_data_as_of"):
        assert not hasattr(p, forbidden)


def test_builder_signature_is_exactly_two_keyword_only_arguments() -> None:
    parameters = list(inspect.signature(build_private_backtest_replay_point).parameters.values())
    assert [q.name for q in parameters] == ["pit_context", "evaluation_date"]
    assert all(q.kind is inspect.Parameter.KEYWORD_ONLY and q.default is inspect.Parameter.empty for q in parameters)
    context = AnalysisPITContext(mode=SO, knowledge_cutoff=CUTOFF)
    for extra in ("mode", "knowledge_cutoff", "market_data_mode", "execution_at"):
        with pytest.raises(TypeError):
            build_private_backtest_replay_point(pit_context=context, evaluation_date=EVAL, **{extra: 1})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_private_backtest_replay_point(pit_context=context)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_private_backtest_replay_point(context, EVAL)  # type: ignore[misc]


def test_both_external_modes_are_preserved_exactly() -> None:
    for mode in (SO, SY):
        p = point(mode)
        assert p.as_of_mode is mode and p.pit_context.mode is mode
    assert {m for m in AsOfMode} == {SO, SY}                                       # no third replay mode exists


def test_pit_context_is_stored_by_identity_and_properties_expose_the_original_datetime() -> None:
    context = AnalysisPITContext(mode=SO, knowledge_cutoff=CUTOFF)
    p = build_private_backtest_replay_point(pit_context=context, evaluation_date=EVAL)
    assert p.pit_context is context
    assert p.knowledge_cutoff is context.knowledge_cutoff is CUTOFF
    assert p.portfolio_recorded_cutoff is context.knowledge_cutoff
    assert p.knowledge_cutoff.utcoffset() == timedelta(hours=3)                    # caller representation not normalized
    assert p.as_of_mode is context.mode


def test_knowledge_cutoff_utc_delegates_to_the_closed_context() -> None:
    p = point()
    assert p.knowledge_cutoff_utc == datetime(2026, 9, 10, 7, 0, tzinfo=UTC) == p.pit_context.knowledge_cutoff_utc
    assert p.knowledge_cutoff_utc.tzinfo is UTC


def test_evaluation_date_is_independent_of_the_knowledge_cutoff_date() -> None:
    cutoff_date = CUTOFF.date()
    earlier, same, later = (point(evaluation_date=cutoff_date + timedelta(days=d)) for d in (-5, 0, 30))      # a future-effective date is allowed
    assert earlier.evaluation_date < cutoff_date == same.evaluation_date < later.evaluation_date
    assert point(evaluation_date=EVAL) != point(evaluation_date=EVAL + timedelta(days=1))
    assert point().evaluation_date == EVAL


def test_exact_type_failures_including_direct_construction() -> None:
    context = AnalysisPITContext(mode=SO, knowledge_cutoff=CUTOFF)

    class SubContext(AnalysisPITContext):
        pass

    class SubDate(date):
        pass

    for bad_context in (None, object(), "ctx", {"mode": SO}, SubContext(mode=SO, knowledge_cutoff=CUTOFF)):
        with pytest.raises(TypeError):
            build_private_backtest_replay_point(pit_context=bad_context, evaluation_date=EVAL)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            PrivateBacktestReplayPoint(pit_context=bad_context, evaluation_date=EVAL)  # type: ignore[arg-type]
    for bad_date in (datetime(2026, 9, 9, 0, 0), datetime(2026, 9, 9, tzinfo=UTC), "2026-09-09", None, 20260909, SubDate(2026, 9, 9)):
        with pytest.raises(TypeError):
            build_private_backtest_replay_point(pit_context=context, evaluation_date=bad_date)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            PrivateBacktestReplayPoint(pit_context=context, evaluation_date=bad_date)  # type: ignore[arg-type]


# --- composition with the existing PIT authorities -----------------------------------------------------------------

def test_portfolio_projection_uses_the_recorded_cutoff_and_ignores_future_reversals() -> None:
    portfolio = _make_portfolio()
    account = uuid4()
    t0, t1, t2 = (datetime(2026, 8, d, 12, 0, tzinfo=UTC) for d in (1, 10, 20))
    base = _make_tx(portfolio.id, account, tx_type=TransactionType.BUY, recorded_at=t0)
    reversal = _make_tx(portfolio.id, account, tx_type=TransactionType.REVERSAL, recorded_at=t2, reverses_tx_id=base.id)
    for mode in (SO, SY):                                                          # the ledger has no source perspective: both modes use the same frontier
        p = point(mode, cutoff=t1, evaluation_date=date(2026, 8, 10))
        view = build_ledger_projection_view(portfolio, [base, reversal], as_of_recorded_at=p.portfolio_recorded_cutoff)
        assert view.as_of_recorded_at is p.portfolio_recorded_cutoff
        assert view.known_transactions == (base,) and view.active_transactions == (base,) and view.transaction_states[0].is_reversed is False
    later = point(SY, cutoff=t2, evaluation_date=date(2026, 8, 10))
    after = build_ledger_projection_view(portfolio, [base, reversal], as_of_recorded_at=later.portfolio_recorded_cutoff)
    assert after.active_transactions == () and len(after.known_transactions) == 2
    early = point(SY, cutoff=t0 - timedelta(microseconds=1), evaluation_date=date(2026, 8, 10))
    assert build_ledger_projection_view(portfolio, [base, reversal], as_of_recorded_at=early.portfolio_recorded_cutoff).known_transactions == ()


def test_candidate_universe_cannot_be_contaminated_by_post_cutoff_snapshots() -> None:
    known = snap([UA], effective_from=date(2026, 6, 1), published=datetime(2026, 5, 20, tzinfo=UTC), observed=datetime(2026, 5, 21, tzinfo=UTC))
    after_cutoff = snap([UA, UA.__class__(int=9)], effective_from=date(2026, 6, 1), published=datetime(2026, 9, 20, tzinfo=UTC), observed=datetime(2026, 9, 21, tzinfo=UTC),
                        sha="b" * 64)
    for mode in (SO, SY):
        p = point(mode, cutoff=datetime(2026, 9, 10, tzinfo=UTC), evaluation_date=date(2026, 9, 9))
        query = CandidateUniverseQuery(source_key=SRC, universe_key=UNI, asset_class=AssetClass.EQUITY, evaluation_date=p.evaluation_date, pit_context=p.pit_context)
        resolution = resolve_candidate_universe(query=query, snapshots=(known, after_cutoff))
        assert resolution.status is CandidateUniverseResolutionStatus.SELECTED and resolution.selected_snapshot is known
        assert query.pit_context is p.pit_context
        only_future = resolve_candidate_universe(query=query, snapshots=(after_cutoff,))
        assert only_future.status is CandidateUniverseResolutionStatus.NO_SNAPSHOT_AS_OF and only_future.selected_snapshot is None


def test_game_changer_event_binding_shares_the_replay_pit_context() -> None:
    p = point(SO, cutoff=datetime(2026, 9, 1, 11, tzinfo=UTC), evaluation_date=date(2026, 9, 1))
    known = gc_event(published_at=datetime(2026, 9, 1, 10, tzinfo=UTC), observed_at=datetime(2026, 9, 1, 10, 30, tzinfo=UTC))
    assert bind_game_changer_event_pit(event=known, context=p.pit_context).context is p.pit_context
    future = gc_event(published_at=datetime(2026, 9, 1, 11, 30, tzinfo=UTC), observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
    with pytest.raises(ValueError):
        bind_game_changer_event_pit(event=future, context=p.pit_context)
    system = point(SY, cutoff=datetime(2026, 9, 1, 11, tzinfo=UTC), evaluation_date=date(2026, 9, 1))
    published_known_observed_late = gc_event(published_at=datetime(2026, 9, 1, 10, tzinfo=UTC), observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
    assert bind_game_changer_event_pit(event=published_known_observed_late, context=p.pit_context).context is p.pit_context
    with pytest.raises(ValueError):
        bind_game_changer_event_pit(event=published_known_observed_late, context=system.pit_context)


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_replay_point.py"


def test_module_is_registered_pure_and_clean_on_all_guards() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_only_the_two_pure_closed_authorities_and_no_market_data_dependency() -> None:
    imported = {(n.module, a.name) for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names if (n.module or "").startswith("backend")}
    assert imported == {("backend.engine.private.analysis_pit", "AnalysisPITContext"), ("backend.engine.private.domain", "AsOfMode")}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]
    assert "market_data" not in " ".join(m for m, _ in imported)
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not names & {"MarketDataResolutionMode", "market_data_mode", "market_data_as_of", "CURRENT_REPORTED"}
    assert sg.scan_g3(_SOURCE, _REL) == []


def test_no_current_reported_clock_randomness_hash_loop_or_io() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"now", "utcnow", "today", "time", "monotonic", "uuid4", "random", "secrets", "urandom", "sha256", "hashlib", "dumps", "client", "rpc", "table",
                        "latest", "current", "PrivateSchedulerTrigger"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "scheduler" not in (getattr(node, "module", "") or "")


def test_documentation_states_the_three_times_and_the_limits() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_REPLAY_POINT.md").read_text(encoding="utf-8")
    for needle in ("knowledge cutoff", "PIT perspective", "evaluation date", "execution", "SOURCE_AS_OF", "SYSTEM_AS_OF", "MarketDataResolutionMode", "Phase26A2", "recorded_at", "source-as-of portfolio ledger"):
        assert needle in doc, needle
