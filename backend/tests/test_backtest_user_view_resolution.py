"""
backend/tests/test_backtest_user_view_resolution.py
===================================================
Phase 26C2D2: point-in-time resolution of Phase 26C2D1 user-view revisions at one analysis knowledge frontier. Knowledge-time filtering happens BEFORE any
cross-revision validation, so a future revision has zero influence. Pure: no view set, no posterior, no prior binding, no completeness/readiness claim.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import backtest_user_view_resolution as module_under_test
from backend.engine.private.allocation_user_views import UserReturnView, UserReturnViewKind
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext, build_private_backtest_analysis_context
from backend.engine.private.backtest_replay_point import build_private_backtest_replay_point
from backend.engine.private.backtest_user_view_history import PrivateBacktestUserViewRevision, PrivateBacktestUserViewRevisionAction
from backend.engine.private.backtest_user_view_resolution import (
    PrivateBacktestUserViewHistoryCoverage,
    PrivateBacktestUserViewResolution,
    PrivateBacktestUserViewResolutionStatus,
    resolve_private_backtest_user_views,
)
from backend.engine.private.domain import AsOfMode, Horizon
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
UP, WD = PrivateBacktestUserViewRevisionAction.UPSERT, PrivateBacktestUserViewRevisionAction.WITHDRAW
COMPLETE = PrivateBacktestUserViewHistoryCoverage.COMPLETE_AT_CUTOFF
INCOMPLETE = PrivateBacktestUserViewHistoryCoverage.INCOMPLETE_AT_CUTOFF
RESOLVED = PrivateBacktestUserViewResolutionStatus.RESOLVED
INCOMPLETE_STATUS = PrivateBacktestUserViewResolutionStatus.INCOMPLETE_COVERAGE
CUTOFF = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
V1, V2, V3 = UUID(int=0xA1), UUID(int=0xB2), UUID(int=0xC3)
BASIS = (UUID(int=1),)


def at(minutes: int, tz=UTC) -> datetime:
    return (CUTOFF + timedelta(minutes=minutes)).astimezone(tz)


def analysis(mode=SY, cutoff=CUTOFF, evaluation_date=date(2026, 9, 9)) -> PrivateBacktestAnalysisContext:
    point = build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)
    return build_private_backtest_analysis_context(replay_point=point, horizon=Horizon.ALLOCATION_12M)


def view() -> UserReturnView:
    return UserReturnView(kind=UserReturnViewKind.ABSOLUTE, loadings=(Decimal(1),), target_return=Decimal("0.05"), confidence=Decimal("0.5"))


def rev(view_id, revision, minutes=-10, action=UP, available_at=None) -> PrivateBacktestUserViewRevision:
    moment = available_at if available_at is not None else at(minutes)
    if action is UP:
        return PrivateBacktestUserViewRevision(view_id=view_id, revision=revision, action=UP, instrument_ids=BASIS, view=view(), available_at=moment)
    return PrivateBacktestUserViewRevision(view_id=view_id, revision=revision, action=WD, instrument_ids=(), view=None, available_at=moment)


def resolve(revisions, coverage=COMPLETE, context=None) -> PrivateBacktestUserViewResolution:
    return resolve_private_backtest_user_views(analysis_context=context or analysis(), revisions=tuple(revisions), coverage=coverage)


# --- contract ------------------------------------------------------------------------------------------------------

def test_result_shape_is_exactly_six_fields() -> None:
    fs = fields(PrivateBacktestUserViewResolution)
    assert [f.name for f in fs] == ["analysis_context", "coverage", "eligible_revisions", "status", "terminal_revisions", "active_revisions"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestUserViewResolution.__dataclass_params__.frozen is True
    result = resolve([])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = INCOMPLETE_STATUS  # type: ignore[misc]


def test_enums_are_exactly_two_members_each() -> None:
    assert {m.name: m.value for m in PrivateBacktestUserViewHistoryCoverage} == {"COMPLETE_AT_CUTOFF": "complete_at_cutoff", "INCOMPLETE_AT_CUTOFF": "incomplete_at_cutoff"}
    assert {m.name: m.value for m in PrivateBacktestUserViewResolutionStatus} == {"RESOLVED": "resolved", "INCOMPLETE_COVERAGE": "incomplete_coverage"}


def test_resolver_signature_is_keyword_only() -> None:
    params = inspect.signature(resolve_private_backtest_user_views).parameters
    assert list(params) == ["analysis_context", "revisions", "coverage"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_resolver_requires_exact_types() -> None:
    class Ctx(PrivateBacktestAnalysisContext):
        pass

    class T(tuple):
        pass

    class R(PrivateBacktestUserViewRevision):
        pass

    good = rev(V1, 1)
    sub = R(view_id=V1, revision=1, action=UP, instrument_ids=BASIS, view=view(), available_at=at(-10))
    base = analysis()
    sub_ctx = Ctx(replay_point=base.replay_point, temporal_context=base.temporal_context)
    for kwargs in (
        dict(analysis_context=sub_ctx, revisions=(good,), coverage=COMPLETE), dict(analysis_context=object(), revisions=(good,), coverage=COMPLETE),
        dict(analysis_context=base, revisions=[good], coverage=COMPLETE), dict(analysis_context=base, revisions=T((good,)), coverage=COMPLETE),
        dict(analysis_context=base, revisions=(sub,), coverage=COMPLETE), dict(analysis_context=base, revisions=(object(),), coverage=COMPLETE),
        dict(analysis_context=base, revisions=(good,), coverage="complete_at_cutoff"), dict(analysis_context=base, revisions=(good,), coverage=None),
    ):
        with pytest.raises(TypeError):
            resolve_private_backtest_user_views(**kwargs)


def test_future_revisions_are_type_validated_too() -> None:
    with pytest.raises(TypeError):
        resolve([rev(V1, 1), object()])


# --- empty ---------------------------------------------------------------------------------------------------------

def test_empty_complete_history_resolves_to_no_views() -> None:
    result = resolve([])
    assert result.status is RESOLVED and result.coverage is COMPLETE
    assert result.eligible_revisions == result.terminal_revisions == result.active_revisions == ()
    assert all(type(t) is tuple for t in (result.eligible_revisions, result.terminal_revisions, result.active_revisions))


def test_empty_incomplete_history_is_incomplete_coverage() -> None:
    result = resolve([], INCOMPLETE)
    assert result.status is INCOMPLETE_STATUS and result.coverage is INCOMPLETE
    assert result.eligible_revisions == result.terminal_revisions == result.active_revisions == ()


# --- knowledge cutoff ------------------------------------------------------------------------------------------------

def test_revision_exactly_at_cutoff_is_eligible_and_one_microsecond_after_is_not() -> None:
    exact = rev(V1, 1, available_at=CUTOFF)
    future = rev(V2, 1, available_at=CUTOFF + timedelta(microseconds=1))
    result = resolve([exact, future])
    assert result.eligible_revisions == (exact,) and result.eligible_revisions[0] is exact
    assert result.active_revisions == (exact,)
    assert all(future is not r for t in (result.eligible_revisions, result.terminal_revisions, result.active_revisions) for r in t)


def test_equivalent_offsets_compare_by_utc_instant() -> None:
    plus3_equal = rev(V1, 1, available_at=CUTOFF.astimezone(PLUS3))
    plus3_late = rev(V2, 1, available_at=(CUTOFF + timedelta(microseconds=1)).astimezone(PLUS3))
    result = resolve([plus3_equal, plus3_late])
    assert result.eligible_revisions == (plus3_equal,)
    assert result.eligible_revisions[0].available_at.utcoffset() == timedelta(hours=3)


def test_cutoff_given_in_non_utc_offset_uses_utc_instant() -> None:
    context = analysis(cutoff=CUTOFF.astimezone(PLUS3))
    on = rev(V1, 1, available_at=CUTOFF)
    after = rev(V2, 1, available_at=CUTOFF + timedelta(microseconds=1))
    assert resolve([on, after], context=context).eligible_revisions == (on,)


def test_source_and_system_modes_share_the_same_availability_rule() -> None:
    revisions = [rev(V1, 1, minutes=-5), rev(V2, 1, minutes=5)]
    for mode in (SO, SY):
        result = resolve(revisions, context=analysis(mode=mode))
        assert [r.view_id for r in result.eligible_revisions] == [V1]
    a, b = resolve(revisions, context=analysis(mode=SO)), resolve(revisions, context=analysis(mode=SY))
    assert a.eligible_revisions == b.eligible_revisions and a.status is b.status


def test_evaluation_date_does_not_change_eligibility() -> None:
    revisions = [rev(V1, 1, minutes=-5), rev(V2, 1, minutes=5)]
    results = [resolve(revisions, context=analysis(evaluation_date=d)) for d in (date(2020, 1, 1), date(2026, 9, 9), date(2031, 1, 1))]
    assert all(r.eligible_revisions == results[0].eligible_revisions for r in results)


# --- future isolation ------------------------------------------------------------------------------------------------

def _baseline():
    return [rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD), rev(V2, 1, minutes=-10)]


def _same_resolution(a: PrivateBacktestUserViewResolution, b: PrivateBacktestUserViewResolution) -> bool:
    return all(len(x) == len(y) and all(p is q for p, q in zip(x, y)) for x, y in (
        (a.eligible_revisions, b.eligible_revisions), (a.terminal_revisions, b.terminal_revisions), (a.active_revisions, b.active_revisions))) and a.status is b.status


@pytest.mark.parametrize("future", [
    lambda: rev(V1, 2, minutes=5),                                   # future duplicate of an eligible revision number
    lambda: rev(V1, 100, minutes=5),                                 # future huge revision: would be a gap
    lambda: rev(V2, 2, minutes=5, action=WD),                        # future withdraw
    lambda: rev(V3, 1, minutes=5),                                   # future new view
    lambda: rev(V3, 7, minutes=5, action=WD),                        # future gap and bad start
    lambda: rev(V2, 1, minutes=5),                                   # future duplicate of an eligible identity
    lambda: rev(V1, 3, minutes=1),                                   # future reactivation
])
def test_future_revisions_have_zero_influence(future) -> None:
    known = _baseline()
    base = resolve(known)
    with_future = resolve(known + [future()])
    assert _same_resolution(base, with_future)
    assert with_future.status is RESOLVED and [r.view_id for r in with_future.active_revisions] == [V2]
    for revision in with_future.eligible_revisions + with_future.terminal_revisions + with_future.active_revisions:
        assert revision.available_at <= CUTOFF


def test_future_revision_is_not_retained_anywhere() -> None:
    future = rev(V3, 1, minutes=5)
    result = resolve(_baseline() + [future])
    for f in fields(result):
        value = getattr(result, f.name)
        if type(value) is tuple:
            assert not any(item is future for item in value)


def test_future_isolation_holds_under_incomplete_coverage() -> None:
    known = _baseline()
    base = resolve(known, INCOMPLETE)
    assert _same_resolution(base, resolve(known + [rev(V1, 2, minutes=5)], INCOMPLETE))


# --- canonical order / duplicates ------------------------------------------------------------------------------------

def test_input_permutation_gives_identical_canonical_resolution() -> None:
    revisions = [rev(V2, 1, minutes=-10), rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD), rev(V3, 1, minutes=-5), rev(V3, 2, minutes=-4)]
    expected = resolve(revisions)
    for permutation in itertools.permutations(revisions):
        assert _same_resolution(expected, resolve(permutation))


def test_canonical_eligible_order_is_view_id_string_then_revision() -> None:
    a1, a2, b1 = rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20), rev(V2, 1, minutes=-10)
    result = resolve([b1, a2, a1])
    assert result.eligible_revisions == (a1, a2, b1)
    assert all(x is y for x, y in zip(result.eligible_revisions, (a1, a2, b1)))
    assert [str(r.view_id) for r in result.eligible_revisions] == sorted(str(r.view_id) for r in result.eligible_revisions)


@pytest.mark.parametrize("coverage", [COMPLETE, INCOMPLETE])
def test_eligible_duplicate_identity_fails_even_with_equal_contents(coverage) -> None:
    with pytest.raises(ValueError):
        resolve([rev(V1, 1), rev(V1, 1)], coverage)


@pytest.mark.parametrize("coverage", [COMPLETE, INCOMPLETE])
def test_repeated_exact_object_is_a_duplicate(coverage) -> None:
    same = rev(V1, 1)
    with pytest.raises(ValueError):
        resolve([same, same], coverage)


# --- complete-family rules -------------------------------------------------------------------------------------------

def test_complete_family_must_start_at_revision_one() -> None:
    with pytest.raises(ValueError):
        resolve([rev(V1, 2)])


def test_complete_family_must_be_contiguous() -> None:
    with pytest.raises(ValueError):
        resolve([rev(V1, 1, minutes=-30), rev(V1, 3, minutes=-20)])


def test_incomplete_history_may_have_gaps_and_exposes_no_state() -> None:
    first, third = rev(V1, 1, minutes=-30), rev(V1, 3, minutes=-20)
    result = resolve([third, first], INCOMPLETE)
    assert result.status is INCOMPLETE_STATUS
    assert result.eligible_revisions == (first, third)
    assert result.terminal_revisions == () and result.active_revisions == ()
    missing_start = resolve([rev(V1, 5), rev(V2, 2, action=WD)], INCOMPLETE)
    assert missing_start.status is INCOMPLETE_STATUS and len(missing_start.eligible_revisions) == 2
    assert missing_start.terminal_revisions == () and missing_start.active_revisions == ()


def test_complete_family_revision_one_must_be_upsert() -> None:
    with pytest.raises(ValueError):
        resolve([rev(V1, 1, action=WD)])


def test_complete_availability_must_be_non_decreasing_by_revision() -> None:
    with pytest.raises(ValueError):
        resolve([rev(V1, 1, minutes=-10), rev(V1, 2, minutes=-20)])
    with pytest.raises(ValueError):
        resolve([rev(V1, 1, available_at=at(-10)), rev(V1, 2, available_at=at(-10) - timedelta(microseconds=1))])


def test_equal_availability_for_consecutive_revisions_is_valid() -> None:
    first, second = rev(V1, 1, minutes=-10), rev(V1, 2, minutes=-10, action=WD)
    assert resolve([first, second]).terminal_revisions == (second,)


def test_availability_order_is_compared_as_utc_instants() -> None:
    first = rev(V1, 1, available_at=at(-10, PLUS3))
    second = rev(V1, 2, available_at=at(-10, UTC))          # same instant, different offset
    assert resolve([first, second]).terminal_revisions == (second,)
    third = rev(V2, 1, available_at=at(-5, PLUS3))
    fourth = rev(V2, 2, available_at=at(-6, UTC))
    with pytest.raises(ValueError):
        resolve([third, fourth])


def test_revision_number_not_timestamp_is_lifecycle_authority() -> None:
    low, high = rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-5)
    result = resolve([high, low])
    assert result.terminal_revisions[0] is high
    big_confidence = UserReturnView(kind=UserReturnViewKind.ABSOLUTE, loadings=(Decimal(1),), target_return=Decimal("0.9"), confidence=Decimal("1"))
    bigger = PrivateBacktestUserViewRevision(view_id=V1, revision=2, action=UP, instrument_ids=BASIS, view=big_confidence, available_at=at(-5))
    assert resolve([low, bigger]).terminal_revisions[0] is bigger


# --- terminal / active -----------------------------------------------------------------------------------------------

def test_terminal_is_exact_highest_revision_object() -> None:
    r1, r2, r3 = rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20), rev(V1, 3, minutes=-10)
    result = resolve([r2, r3, r1])
    assert result.terminal_revisions == (r3,) and result.terminal_revisions[0] is r3
    assert result.active_revisions[0] is r3


def test_terminal_withdraw_removes_active_without_reviving_earlier_upsert() -> None:
    r1, r2 = rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD)
    result = resolve([r1, r2])
    assert result.status is RESOLVED
    assert result.terminal_revisions == (r2,) and result.terminal_revisions[0] is r2
    assert result.active_revisions == ()


def test_later_upsert_reactivates_after_withdraw() -> None:
    r1, r2, r3 = rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD), rev(V1, 3, minutes=-10)
    result = resolve([r1, r2, r3])
    assert result.terminal_revisions == (r3,) and result.active_revisions == (r3,)
    assert result.active_revisions[0] is r3


def test_multiple_views_resolve_independently_in_canonical_order() -> None:
    a1, a2 = rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD)
    b1 = rev(V2, 1, minutes=-10)
    c1, c2 = rev(V3, 1, minutes=-9), rev(V3, 2, minutes=-8)
    result = resolve([c2, b1, a2, c1, a1])
    assert result.terminal_revisions == (a2, b1, c2)
    assert all(x is y for x, y in zip(result.terminal_revisions, (a2, b1, c2)))
    assert result.active_revisions == (b1, c2)
    assert [str(r.view_id) for r in result.terminal_revisions] == sorted(str(r.view_id) for r in result.terminal_revisions)
    assert [str(r.view_id) for r in result.active_revisions] == sorted(str(r.view_id) for r in result.active_revisions)
    assert a2 in result.terminal_revisions and a2 not in result.active_revisions


def test_active_revision_retains_exact_view_and_instrument_basis() -> None:
    r1 = rev(V1, 1)
    result = resolve([r1])
    active = result.active_revisions[0]
    assert active is r1 and active.view is r1.view and active.instrument_ids is r1.instrument_ids


def test_zero_active_views_can_be_resolved() -> None:
    result = resolve([rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD)])
    assert result.status is RESOLVED and result.active_revisions == () and len(result.terminal_revisions) == 1


# --- direct construction / forgery -----------------------------------------------------------------------------------

def _build(result: PrivateBacktestUserViewResolution, **over) -> PrivateBacktestUserViewResolution:
    args = {f.name: getattr(result, f.name) for f in fields(result)}
    args.update(over)
    return PrivateBacktestUserViewResolution(**args)


def _rich() -> PrivateBacktestUserViewResolution:
    return resolve([rev(V1, 1, minutes=-30), rev(V1, 2, minutes=-20, action=WD), rev(V2, 1, minutes=-10), rev(V3, 1, minutes=-9)])


def test_direct_construction_accepts_resolver_output() -> None:
    result = _rich()
    assert _build(result) == result
    assert _same_resolution(_build(result), result)


def test_forged_status_is_rejected() -> None:
    result = _rich()
    with pytest.raises(ValueError):
        _build(result, status=INCOMPLETE_STATUS)
    incomplete = resolve([rev(V1, 1)], INCOMPLETE)
    with pytest.raises(ValueError):
        _build(incomplete, status=RESOLVED)
    with pytest.raises(TypeError):
        _build(result, status="resolved")


def test_forged_terminal_and_active_are_rejected() -> None:
    result = _rich()
    e = result.eligible_revisions
    for forged in ((), result.terminal_revisions[:-1], tuple(reversed(result.terminal_revisions)), e, result.terminal_revisions + (e[0],)):
        with pytest.raises(ValueError):
            _build(result, terminal_revisions=forged)
    for forged in ((), result.active_revisions[:-1], tuple(reversed(result.active_revisions)), result.terminal_revisions, result.active_revisions + (e[0],)):
        with pytest.raises(ValueError):
            _build(result, active_revisions=forged)
    incomplete = resolve([rev(V1, 1)], INCOMPLETE)
    with pytest.raises(ValueError):
        _build(incomplete, terminal_revisions=incomplete.eligible_revisions)
    with pytest.raises(ValueError):
        _build(incomplete, active_revisions=incomplete.eligible_revisions)


def test_equal_valued_clone_is_not_the_selected_object() -> None:
    result = resolve([rev(V1, 1, minutes=-30)])
    original = result.terminal_revisions[0]
    clone = dataclasses.replace(original)
    assert clone == original and clone is not original
    with pytest.raises(ValueError):
        _build(result, terminal_revisions=(clone,))
    with pytest.raises(ValueError):
        _build(result, active_revisions=(clone,))
    with pytest.raises(ValueError):
        _build(result, eligible_revisions=(clone,))


def test_direct_construction_rejects_future_unsorted_duplicate_and_foreign_eligible() -> None:
    result = _rich()
    future = rev(V3, 1, minutes=5)
    with pytest.raises(ValueError):
        _build(resolve([]), eligible_revisions=(future,))
    with pytest.raises(ValueError):
        _build(result, eligible_revisions=tuple(reversed(result.eligible_revisions)))
    with pytest.raises(ValueError):
        _build(result, eligible_revisions=result.eligible_revisions + (result.eligible_revisions[0],))
    with pytest.raises(TypeError):
        _build(result, eligible_revisions=list(result.eligible_revisions))
    with pytest.raises(TypeError):
        _build(result, eligible_revisions=result.eligible_revisions + (object(),))
    with pytest.raises(TypeError):
        _build(result, analysis_context=object())
    with pytest.raises(TypeError):
        _build(result, coverage="complete_at_cutoff")

    class T(tuple):
        pass

    with pytest.raises(TypeError):
        _build(result, active_revisions=T(result.active_revisions))
    with pytest.raises(TypeError):
        _build(result, terminal_revisions=T(result.terminal_revisions))


def test_direct_construction_revalidates_complete_family_rules() -> None:
    gap = (rev(V1, 1, minutes=-30), rev(V1, 3, minutes=-20))
    with pytest.raises(ValueError):
        PrivateBacktestUserViewResolution(analysis_context=analysis(), coverage=COMPLETE, eligible_revisions=gap, status=RESOLVED, terminal_revisions=(gap[1],),
                                          active_revisions=(gap[1],))


# --- module boundary -------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_user_view_resolution.py"


def test_module_is_registered_pure_and_clean_on_all_guards() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_only_the_closed_context_and_revision_authorities() -> None:
    imported = {(n.module, a.name) for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names if (n.module or "").startswith("backend")}
    assert imported == {
        ("backend.engine.private.backtest_analysis_context", "PrivateBacktestAnalysisContext"),
        ("backend.engine.private.backtest_user_view_history", "PrivateBacktestUserViewRevision"),
        ("backend.engine.private.backtest_user_view_history", "PrivateBacktestUserViewRevisionAction"),
    }
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]
    stdlib = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and not (n.module or "").startswith("backend")}
    assert stdlib <= {"__future__", "dataclasses", "datetime", "enum", "uuid", "typing"}


def test_no_view_set_posterior_prior_or_readiness_surface() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"UserReturnViewSet", "UserReturnView", "ExpectedReturnPrior", "AllocationReturnPanel", "posterior", "build_user_view_posterior"}
    public = {n for n in dir(module_under_test) if not n.startswith("_")}
    for banned in ("decision_ready", "ready", "required_views", "coverage_percentage", "minimum_view_count", "recommendation", "allocation", "target_weight", "hash", "digest"):
        assert not [n for n in public if banned in n.lower()], banned
    defined = {n.name for n in _TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_")}
    assert defined == {"PrivateBacktestUserViewHistoryCoverage", "PrivateBacktestUserViewResolutionStatus", "PrivateBacktestUserViewResolution",
                       "resolve_private_backtest_user_views"}


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "uuid5", "random", "secrets", "urandom", "sha256", "hashlib", "open", "client", "rpc",
                        "table", "environ", "getenv", "max", "min"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await))]


def test_documentation_states_required_boundaries() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_USER_VIEW_RESOLUTION.md").read_text(encoding="utf-8")
    for needle in ("knowledge cutoff", "SOURCE_AS_OF", "SYSTEM_AS_OF", "BEFORE", "future revisions", "canonical", "duplicate", "contiguous", "INCOMPLETE_AT_CUTOFF",
                   "terminal", "active", "reactivate", "positional", "UserReturnViewSet", "posterior", "decision readiness", "C2E", "C2B2B3", "C2C2"):
        assert needle in doc, needle
