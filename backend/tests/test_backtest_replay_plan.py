"""
backend/tests/test_backtest_replay_plan.py
==========================================
Phase 26B: the pure ordered historical replay plan. An immutable, caller-ordered tuple of explicit Phase 26A1 replay points with strictly increasing UTC
knowledge frontiers and one PIT perspective. No date generation, no sorting, no deduplication and no ordering of evaluation dates.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.engine.private import backtest_replay_plan as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_replay_plan import PrivateBacktestReplayPlan, build_private_backtest_replay_plan
from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint, build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
T0 = datetime(2026, 1, 10, 7, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(days=1)


def pt(cutoff=T0, mode=SO, evaluation_date=date(2026, 1, 10)) -> PrivateBacktestReplayPoint:
    return build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)


def plan(*points) -> PrivateBacktestReplayPlan:
    return build_private_backtest_replay_plan(points=tuple(points))


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_shape_and_builder_signature() -> None:
    fields = dataclasses.fields(PrivateBacktestReplayPlan)
    assert [f.name for f in fields] == ["points"]
    assert fields[0].default is dataclasses.MISSING and fields[0].default_factory is dataclasses.MISSING
    assert PrivateBacktestReplayPlan.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan(pt()).points = ()  # type: ignore[misc]
    parameters = list(inspect.signature(build_private_backtest_replay_plan).parameters.values())
    assert [p.name for p in parameters] == ["points"]
    assert parameters[0].kind is inspect.Parameter.KEYWORD_ONLY and parameters[0].default is inspect.Parameter.empty
    for extra in ("mode", "start", "end", "frequency", "calendar"):
        with pytest.raises(TypeError):
            build_private_backtest_replay_plan(points=(pt(),), **{extra: 1})  # type: ignore[arg-type]
    for stored in ("mode", "start", "end", "count", "id", "hash", "calendar", "frequency", "start_date", "end_date", "plan_id"):
        assert not hasattr(plan(pt()), stored)


def test_one_point_plans_in_both_modes() -> None:
    for mode in (SO, SY):
        point = pt(mode=mode)
        p = plan(point)
        assert p.first_point is point and p.last_point is point and p.as_of_mode is point.as_of_mode is mode


def test_tuple_and_member_identity_is_preserved_without_copy_or_sort() -> None:
    points = (pt(T0), pt(T1), pt(T2))
    p = build_private_backtest_replay_plan(points=points)
    assert p.points is points and all(a is b for a, b in zip(p.points, points))
    assert p.first_point is points[0] and p.last_point is points[2] and p.as_of_mode is SO


def test_strictly_increasing_utc_frontier_is_required() -> None:
    plan(pt(T0), pt(T1), pt(T2))
    for bad in ((pt(T0), pt(T0)), (pt(T1), pt(T0)), (pt(T0), pt(T2), pt(T1)), (pt(T0), pt(T1), pt(T1))):
        with pytest.raises(ValueError):
            plan(*bad)
    plan(pt(T0), pt(T0 + timedelta(microseconds=1)))                                   # no tolerance: one microsecond is strictly later


def test_same_instant_with_a_different_offset_is_a_duplicate_frontier() -> None:
    same_instant = datetime(2026, 1, 10, 10, 0, tzinfo=PLUS3)
    assert same_instant == T0
    with pytest.raises(ValueError):
        plan(pt(T0), pt(same_instant))
    with pytest.raises(ValueError):
        plan(pt(same_instant), pt(T0))
    later_other_offset = datetime(2026, 1, 10, 11, 0, tzinfo=PLUS3)                    # 08:00 UTC, strictly later
    p = plan(pt(T0), pt(later_other_offset))
    assert p.last_point.knowledge_cutoff is later_other_offset                          # stored representation untouched


def test_reverse_order_is_rejected_and_the_input_is_not_sorted() -> None:
    points = (pt(T1), pt(T0))
    with pytest.raises(ValueError):
        build_private_backtest_replay_plan(points=points)
    assert points[0].knowledge_cutoff == T1 and points[1].knowledge_cutoff == T0


def test_mixed_pit_perspectives_are_rejected() -> None:
    for first, second in ((SO, SY), (SY, SO)):
        with pytest.raises(ValueError):
            plan(pt(T0, mode=first), pt(T1, mode=second))
    with pytest.raises(ValueError):
        plan(pt(T0, mode=SY), pt(T1, mode=SY), pt(T2, mode=SO))
    assert plan(pt(T0, mode=SY), pt(T1, mode=SY)).as_of_mode is SY


def test_evaluation_dates_are_not_order_authority() -> None:
    plan(pt(T0, evaluation_date=date(2026, 1, 10)), pt(T1, evaluation_date=date(2026, 1, 10)))        # intraday frontiers, one economic date
    plan(pt(T0, evaluation_date=date(2026, 1, 20)), pt(T1, evaluation_date=date(2026, 1, 15)))        # decreasing evaluation date
    plan(pt(T0, evaluation_date=date(2026, 1, 15)), pt(T1, evaluation_date=date(2026, 2, 1)))         # increasing
    future = plan(pt(T0, evaluation_date=date(2026, 6, 30)), pt(T1, evaluation_date=date(2026, 1, 1)), pt(T2, evaluation_date=date(2027, 1, 1)))
    assert future.points[0].evaluation_date > T0.date() and future.points[1].evaluation_date < T1.date()
    assert [p.evaluation_date for p in future.points] == [date(2026, 6, 30), date(2026, 1, 1), date(2027, 1, 1)]     # nothing sorted or generated


def test_exact_collection_and_member_types_in_builder_and_direct_construction() -> None:
    class SubTuple(tuple):
        pass

    class SubPoint(PrivateBacktestReplayPoint):
        pass

    good = pt(T0)
    sub = SubPoint(pit_context=good.pit_context, evaluation_date=good.evaluation_date)
    bad_collections = [(), [], [good], iter((good,)), (p for p in (good,)), {good}, frozenset({good}), SubTuple((good,)), None, "points", good]
    for bad in bad_collections:
        with pytest.raises((TypeError, ValueError)):
            build_private_backtest_replay_plan(points=bad)  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError)):
            PrivateBacktestReplayPlan(points=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_private_backtest_replay_plan(points=())
    for member in (None, object(), sub, good.pit_context, "point"):
        for position in range(3):
            members = [pt(T0), pt(T1), pt(T2)]
            members[position] = member  # type: ignore[call-overload]
            with pytest.raises(TypeError):
                build_private_backtest_replay_plan(points=tuple(members))
            with pytest.raises(TypeError):
                PrivateBacktestReplayPlan(points=tuple(members))
    with pytest.raises(ValueError):
        PrivateBacktestReplayPlan(points=(pt(T1), pt(T0)))
    with pytest.raises(ValueError):
        PrivateBacktestReplayPlan(points=(pt(T0, mode=SO), pt(T1, mode=SY)))


def test_no_points_are_generated_between_supplied_points() -> None:
    p = plan(pt(datetime(2026, 1, 1, tzinfo=UTC)), pt(datetime(2026, 1, 10, tzinfo=UTC)))
    assert len(p.points) == 2


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_replay_plan.py"


def test_module_is_registered_pure_and_clean_on_all_guards() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_only_the_replay_point_and_domain_authorities() -> None:
    imported = {(n.module, a.name) for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names if (n.module or "").startswith("backend")}
    assert imported <= {("backend.engine.private.backtest_replay_point", "PrivateBacktestReplayPoint"), ("backend.engine.private.domain", "AsOfMode")}
    assert ("backend.engine.private.backtest_replay_point", "PrivateBacktestReplayPoint") in imported
    modules = " ".join(m for m, _ in imported)
    for forbidden in ("market_data", "backtest_market_data_bridge", "scheduler", "portfolio", "macro", "game_changer"):
        assert forbidden not in modules
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]


def test_no_sort_dedup_generation_clock_hash_random_or_io() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"sorted", "sort", "set", "frozenset", "fromkeys", "reversed", "list", "date_range", "timedelta", "now", "utcnow", "today", "time",
                        "uuid4", "random", "secrets", "urandom", "sha256", "hashlib", "open", "client", "rpc", "table", "MarketDataResolutionMode", "start_date",
                        "end_date", "range"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]
    for node in ast.walk(_TREE):                                                    # a validation loop over the supplied tuple is allowed; nothing may step dates
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"range", "sorted", "set", "list"}


def test_documentation_states_the_ordering_rules() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_REPLAY_PLAN.md").read_text(encoding="utf-8")
    for needle in ("knowledge_cutoff", "not order authority", "caller order", "no sorting", "no dedup", "one PIT", "not generated", "calendar", "scheduler"):
        assert needle in doc, needle
