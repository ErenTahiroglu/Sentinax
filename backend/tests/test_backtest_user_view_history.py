"""
backend/tests/test_backtest_user_view_history.py
================================================
Phase 26C2D1: the pure immutable historical user-view revision envelope (stable view_id, explicit revision, knowledge-availability instant, exact
canonical instrument basis for positional loadings). No cutoff selection, no view set, no posterior, no completeness claim.
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import fields
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private import backtest_user_view_history as module_under_test
from backend.engine.private.allocation_user_views import UserReturnView, UserReturnViewKind
from backend.engine.private.backtest_user_view_history import (
    PrivateBacktestUserViewRevision,
    PrivateBacktestUserViewRevisionAction,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
UP, WD = PrivateBacktestUserViewRevisionAction.UPSERT, PrivateBacktestUserViewRevisionAction.WITHDRAW
AT = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
IDS = (UUID("00000000-0000-0000-0000-000000000001"), UUID("00000000-0000-0000-0000-000000000002"), UUID("00000000-0000-0000-0000-000000000003"))
VIEW_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def absolute_view() -> UserReturnView:
    return UserReturnView(kind=UserReturnViewKind.ABSOLUTE, loadings=(Decimal(1), Decimal(0), Decimal(0)), target_return=Decimal("0.05"), confidence=Decimal("0.5"))


def upsert(**over) -> PrivateBacktestUserViewRevision:
    args = dict(view_id=VIEW_ID, revision=1, action=UP, instrument_ids=IDS, view=absolute_view(), available_at=AT)
    args.update(over)
    return PrivateBacktestUserViewRevision(**args)


def withdraw(**over) -> PrivateBacktestUserViewRevision:
    args = dict(view_id=VIEW_ID, revision=2, action=WD, instrument_ids=(), view=None, available_at=AT)
    args.update(over)
    return PrivateBacktestUserViewRevision(**args)


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_shape_is_exactly_six_fields_without_defaults() -> None:
    fs = fields(PrivateBacktestUserViewRevision)
    assert [f.name for f in fs] == ["view_id", "revision", "action", "instrument_ids", "view", "available_at"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestUserViewRevision.__dataclass_params__.frozen is True


def test_action_enum_is_exactly_two_members() -> None:
    assert {m.name: m.value for m in PrivateBacktestUserViewRevisionAction} == {"UPSERT": "upsert", "WITHDRAW": "withdraw"}


def test_frozen_envelope_cannot_be_mutated() -> None:
    rev = upsert()
    for name in ("view_id", "revision", "action", "instrument_ids", "view", "available_at"):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(rev, name, getattr(rev, name))
    with pytest.raises((AttributeError, TypeError)):
        rev.extra = 1  # type: ignore[attr-defined]


# --- view_id / revision ----------------------------------------------------------------------------------------------

class _UUIDSub(UUID):
    pass


@pytest.mark.parametrize("bad", [str(VIEW_ID), VIEW_ID.hex, None, 1, _UUIDSub(str(VIEW_ID))])
def test_view_id_must_be_exact_uuid(bad) -> None:
    with pytest.raises(TypeError):
        upsert(view_id=bad)
    with pytest.raises(TypeError):
        withdraw(view_id=bad)


class _IntSub(int):
    pass


@pytest.mark.parametrize("bad", [True, False, _IntSub(1), 1.0, "1", None, Decimal(1)])
def test_revision_must_be_exact_int_and_rejects_bool(bad) -> None:
    with pytest.raises(TypeError):
        upsert(revision=bad)


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_revision_must_be_at_least_one(bad) -> None:
    with pytest.raises(ValueError):
        upsert(revision=bad)
    assert upsert(revision=1).revision == 1
    assert upsert(revision=10**9).revision == 10**9


def test_no_cross_revision_rules_here() -> None:
    assert upsert(revision=7).revision == 7 and withdraw(revision=1).revision == 1   # no ordering/gap/monotonic check on a single envelope


def test_action_must_be_exact_enum_member() -> None:
    for bad in ("upsert", "UPSERT", None, 1):
        with pytest.raises(TypeError):
            upsert(action=bad)


# --- available_at ----------------------------------------------------------------------------------------------------

class _DTSub(datetime):
    pass


class _BadTZ(tzinfo):
    def __init__(self, offset):
        self._offset = offset

    def utcoffset(self, dt):
        return self._offset

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "bad"


@pytest.mark.parametrize("bad", [datetime(2026, 9, 10, 10, 0), "2026-09-10T10:00:00+00:00", None, 1, AT.date()])
def test_available_at_requires_exact_aware_datetime(bad) -> None:
    with pytest.raises(TypeError):
        upsert(available_at=bad)


def test_available_at_rejects_datetime_subclass() -> None:
    with pytest.raises(TypeError):
        upsert(available_at=_DTSub(2026, 9, 10, 10, 0, tzinfo=UTC))


@pytest.mark.parametrize("offset", [timedelta(hours=24), timedelta(hours=-24), timedelta(hours=30), timedelta(days=-2)])
def test_available_at_rejects_out_of_range_offset(offset) -> None:
    with pytest.raises((TypeError, ValueError)):
        upsert(available_at=datetime(2026, 9, 10, 10, 0, tzinfo=_BadTZ(offset)))


def test_available_at_rejects_tzinfo_without_offset() -> None:
    with pytest.raises(TypeError):
        upsert(available_at=datetime(2026, 9, 10, 10, 0, tzinfo=_BadTZ(None)))


def test_available_at_rejects_unconvertible_utc_instant() -> None:
    edge = datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5)))   # UTC conversion underflows
    with pytest.raises(TypeError):
        upsert(available_at=edge)


def test_available_at_keeps_caller_representation_without_normalizing() -> None:
    plus3 = datetime(2026, 9, 10, 13, 0, tzinfo=PLUS3)
    rev_a, rev_b = upsert(available_at=AT), upsert(available_at=plus3)
    assert AT == plus3
    assert rev_a.available_at is AT and rev_b.available_at is plus3
    assert rev_b.available_at.utcoffset() == timedelta(hours=3)
    assert withdraw(available_at=plus3).available_at is plus3


# --- UPSERT ----------------------------------------------------------------------------------------------------------

def test_upsert_retains_exact_view_identity_and_basis() -> None:
    view = absolute_view()
    rev = upsert(view=view)
    assert rev.view is view
    assert rev.instrument_ids is IDS
    assert rev.action is UP
    assert (view.kind, view.loadings, view.target_return, view.confidence) == (UserReturnViewKind.ABSOLUTE, (Decimal(1), Decimal(0), Decimal(0)), Decimal("0.05"), Decimal("0.5"))


def test_upsert_requires_exact_tuple_instrument_ids() -> None:
    class T(tuple):
        pass

    for bad in (list(IDS), set(IDS), (u for u in IDS), T(IDS), None, "abc", frozenset(IDS)):
        with pytest.raises(TypeError):
            upsert(instrument_ids=bad)


def test_upsert_rejects_non_uuid_and_uuid_subclass_members() -> None:
    for bad in ((str(IDS[0]), IDS[1], IDS[2]), (IDS[0], IDS[1], _UUIDSub(str(IDS[2]))), (IDS[0], IDS[1], None), (IDS[0], IDS[1], 3)):
        with pytest.raises(TypeError):
            upsert(instrument_ids=bad)


def test_upsert_rejects_duplicate_instrument_ids() -> None:
    with pytest.raises(ValueError):
        upsert(instrument_ids=(IDS[0], IDS[0], IDS[2]))
    with pytest.raises(ValueError):
        upsert(instrument_ids=(IDS[0], IDS[1], IDS[1]))


def test_upsert_rejects_noncanonical_instrument_order_and_does_not_reorder() -> None:
    for bad in ((IDS[1], IDS[0], IDS[2]), (IDS[2], IDS[1], IDS[0])):
        with pytest.raises(ValueError):
            upsert(instrument_ids=bad)


def test_canonical_order_is_ascending_uuid_string_order() -> None:
    ids = tuple(UUID(int=n) for n in (3, 9, 0xA, 0xF, 0x10, 0xFF))
    assert [str(u) for u in ids] == sorted(str(u) for u in ids)
    view = UserReturnView(kind=UserReturnViewKind.ABSOLUTE, loadings=(Decimal(1),) + (Decimal(0),) * 5, target_return=Decimal("0.1"), confidence=Decimal("1"))
    assert upsert(instrument_ids=ids, view=view).instrument_ids is ids


def test_upsert_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError):
        upsert(instrument_ids=IDS[:2])
    view2 = UserReturnView(kind=UserReturnViewKind.ABSOLUTE, loadings=(Decimal(1), Decimal(0)), target_return=Decimal("0.05"), confidence=Decimal("0.5"))
    with pytest.raises(ValueError):
        upsert(view=view2)
    assert upsert(view=view2, instrument_ids=IDS[:2]).view is view2


def test_upsert_requires_view_and_basis() -> None:
    with pytest.raises(TypeError):
        upsert(view=None)
    with pytest.raises(ValueError):
        upsert(instrument_ids=())


def test_upsert_rejects_user_return_view_subclass_and_foreign_types() -> None:
    class V(UserReturnView):
        pass

    sub = V(kind=UserReturnViewKind.ABSOLUTE, loadings=(Decimal(1), Decimal(0), Decimal(0)), target_return=Decimal("0.05"), confidence=Decimal("0.5"))
    for bad in (sub, {"kind": "absolute"}, object(), "view"):
        with pytest.raises(TypeError):
            upsert(view=bad)


# --- WITHDRAW --------------------------------------------------------------------------------------------------------

def test_withdraw_carries_no_economic_payload() -> None:
    rev = withdraw()
    assert rev.view is None and rev.instrument_ids == () and rev.action is WD
    assert type(rev.instrument_ids) is tuple


def test_withdraw_requires_exact_empty_tuple_not_a_subclass_or_equality_spoof() -> None:
    class T(tuple):
        pass

    class Spoof:
        def __eq__(self, other):
            return True

        __hash__ = None  # type: ignore[assignment]

    for bad in (T(), Spoof()):
        with pytest.raises((TypeError, ValueError)):
            withdraw(instrument_ids=bad)
    exact = ()
    assert withdraw(instrument_ids=exact).instrument_ids is exact


def test_withdraw_rejects_view_or_instrument_ids() -> None:
    with pytest.raises(ValueError):
        withdraw(view=absolute_view())
    with pytest.raises(ValueError):
        withdraw(instrument_ids=IDS)
    with pytest.raises(ValueError):
        withdraw(view=absolute_view(), instrument_ids=IDS)
    for bad in ([], None, "", frozenset()):
        with pytest.raises((TypeError, ValueError)):
            withdraw(instrument_ids=bad)


# --- no selection / construction / completeness ---------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_user_view_history.py"


def test_module_is_registered_pure_and_clean_on_all_guards() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_public_surface_is_only_the_revision_primitive() -> None:
    public = {n for n in dir(module_under_test) if not n.startswith("_")}
    for banned in ("resolve_user_views_as_of", "latest", "active", "filter", "group", "build_user_return_view_set", "UserReturnViewSet", "ExpectedReturnPrior",
                   "complete", "ready", "required_views", "missing_views", "decision_ready"):
        assert not [n for n in public if banned.lower() in n.lower() and n not in {"UserReturnView"}], banned
    defined = {n.name for n in _TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert defined == {"PrivateBacktestUserViewRevisionAction", "PrivateBacktestUserViewRevision"}
    methods = {n.name for c in _TREE.body if isinstance(c, ast.ClassDef) and c.name == "PrivateBacktestUserViewRevision" for n in c.body if isinstance(n, ast.FunctionDef)}
    assert methods == {"__post_init__"}


def test_imports_are_only_user_return_view_and_stdlib_primitives() -> None:
    imported = {(n.module, a.name) for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names if (n.module or "").startswith("backend")}
    assert imported == {("backend.engine.private.allocation_user_views", "UserReturnView")}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]
    stdlib = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and not (n.module or "").startswith("backend")}
    assert stdlib <= {"__future__", "dataclasses", "datetime", "enum", "uuid"}


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "uuid5", "random", "secrets", "urandom", "sha256", "hashlib", "open", "client", "rpc",
                        "table", "environ", "getenv", "sorted", "sort"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await))]


def test_documentation_states_required_boundaries() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_USER_VIEW_HISTORY.md").read_text(encoding="utf-8")
    for needle in ("view_id", "revision", "available_at", "knowledge time", "UPSERT", "WITHDRAW", "positional", "instrument basis", "C2D2", "completeness",
                   "persistence", "C2B2B3", "C2C2", "no hash"):
        assert needle in doc, needle
