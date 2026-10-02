"""
backend/tests/test_scheduler_trigger.py
=======================================
Phase 24A: the immutable private scheduler trigger envelope. Pure validation of what work is requested, why (scheduled vs event-driven),
for which system / portfolio-owner scope, under which policy revision and with which idempotency identity. No clock, no schedule
calculation, no dispatch, no runtime state, no persistence, no notification, no legacy scheduler reuse.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_trigger as module_under_test
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerTrigger,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
TK, SC, WK, EC = PrivateSchedulerTriggerKind, PrivateSchedulerScope, PrivateSchedulerWorkKind, PrivateSchedulerEventCauseKind
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
SHA = "d" * 64
PORTFOLIO_WORK = (WK.PORTFOLIO_ANALYSIS_REFRESH, WK.PORTFOLIO_HEALTH_CHECK, WK.GAME_CHANGER_REVIEW)


def T(day=1, h=9, mi=0, s=0, us=0, tz=UTC) -> datetime:
    return datetime(2026, 9, day, h, mi, s, us, tzinfo=tz)


def kwargs(**changes):
    base = dict(trigger_kind=TK.SCHEDULED, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
                scheduled_for=T(), event_cause_kind=None, cause_key=None, cause_available_at=None, policy_key="private.scheduler.v1",
                policy_revision=1, idempotency_sha256=SHA)
    base.update(changes)
    return base


def event_kwargs(**changes):
    return kwargs(**{**dict(trigger_kind=TK.EVENT_DRIVEN, scheduled_for=None, event_cause_kind=EC.DISCLOSURE_INGESTED, cause_key="Evt-1",
                            cause_available_at=T()), **changes})


def portfolio_kwargs(**changes):
    return kwargs(**{**dict(work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO), **changes})


def build(**changes) -> PrivateSchedulerTrigger:
    return build_private_scheduler_trigger(**kwargs(**changes))


# --- enums / stored shape ----------------------------------------------------------------------------------------

def test_enums_and_stored_fields() -> None:
    assert [(m.name, m.value) for m in TK] == [("SCHEDULED", "scheduled"), ("EVENT_DRIVEN", "event_driven")]
    assert [(m.name, m.value) for m in SC] == [("SYSTEM", "system"), ("PORTFOLIO", "portfolio")]
    assert [(m.name, m.value) for m in WK] == [
        ("SOURCE_DATA_REFRESH", "source_data_refresh"), ("PORTFOLIO_ANALYSIS_REFRESH", "portfolio_analysis_refresh"),
        ("PORTFOLIO_HEALTH_CHECK", "portfolio_health_check"), ("GAME_CHANGER_REVIEW", "game_changer_review")]
    assert [(m.name, m.value) for m in EC] == [
        ("DISCLOSURE_INGESTED", "disclosure_ingested"), ("MACRO_RELEASE_INGESTED", "macro_release_ingested"),
        ("POLICY_CONFIGURATION_CHANGED", "policy_configuration_changed"), ("PORTFOLIO_CHANGED", "portfolio_changed"),
        ("NEW_CASH_CONFIRMED", "new_cash_confirmed"), ("USER_VIEW_CHANGED", "user_view_changed"), ("RISK_LIMIT_BREACH", "risk_limit_breach")]
    fields = dataclasses.fields(PrivateSchedulerTrigger)
    assert [f.name for f in fields] == ["trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for", "event_cause_kind",
                                        "cause_key", "cause_available_at", "policy_key", "policy_revision", "idempotency_sha256"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        build().policy_key = "x"  # type: ignore[misc]


def test_builder_shape() -> None:
    signature = inspect.signature(build_private_scheduler_trigger)
    parameters = list(signature.parameters.values())
    assert len(parameters) == 12 and all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    with pytest.raises(TypeError):
        build_private_scheduler_trigger(*kwargs().values())  # type: ignore[misc]
    with pytest.raises(TypeError):
        build_private_scheduler_trigger(**kwargs(), retry_count=3)  # type: ignore[call-arg]
    partial = kwargs()
    del partial["policy_key"]
    with pytest.raises(TypeError):
        build_private_scheduler_trigger(**partial)


def test_enum_fields_are_exact() -> None:
    for field, bads in (("trigger_kind", ["scheduled", None, 1, SC.SYSTEM]), ("work_kind", ["source_data_refresh", None, 1, SC.SYSTEM]),
                        ("scope", ["system", None, 1, TK.SCHEDULED])):
        for bad in bads:
            with pytest.raises(TypeError):
                build(**{field: bad})
    for bad in ("disclosure_ingested", 1, TK.EVENT_DRIVEN):
        with pytest.raises(TypeError):
            build_private_scheduler_trigger(**event_kwargs(event_cause_kind=bad))


# --- scope / owner isolation -------------------------------------------------------------------------------------

def test_system_scope_has_no_owner_or_portfolio() -> None:
    assert build().owner_id is None and build().portfolio_id is None
    for owner, portfolio in ((OWNER, None), (None, PORTFOLIO), (OWNER, PORTFOLIO)):
        with pytest.raises(ValueError):
            build(owner_id=owner, portfolio_id=portfolio)


def test_portfolio_scope_requires_both_exact_uuids() -> None:
    class SubUUID(UUID):
        pass

    for work in PORTFOLIO_WORK:
        built = build_private_scheduler_trigger(**portfolio_kwargs(work_kind=work))
        assert (built.owner_id, built.portfolio_id) == (OWNER, PORTFOLIO)
        for owner, portfolio in ((None, PORTFOLIO), (OWNER, None), (None, None)):
            with pytest.raises(TypeError):
                build_private_scheduler_trigger(**portfolio_kwargs(work_kind=work, owner_id=owner, portfolio_id=portfolio))
        for bad in (str(OWNER), OWNER.int, SubUUID(int=1), b"x"):
            with pytest.raises(TypeError):
                build_private_scheduler_trigger(**portfolio_kwargs(work_kind=work, owner_id=bad))
            with pytest.raises(TypeError):
                build_private_scheduler_trigger(**portfolio_kwargs(work_kind=work, portfolio_id=bad))


def test_work_scope_matrix_is_exhaustive() -> None:
    for work, scope, kind in itertools.product(WK, SC, TK):
        owner, portfolio = (None, None) if scope is SC.SYSTEM else (OWNER, PORTFOLIO)
        shape = kwargs() if kind is TK.SCHEDULED else event_kwargs()
        candidate = {**shape, "work_kind": work, "scope": scope, "owner_id": owner, "portfolio_id": portfolio}
        allowed = (work is WK.SOURCE_DATA_REFRESH) == (scope is SC.SYSTEM)
        if allowed:
            assert build_private_scheduler_trigger(**candidate).work_kind is work
        else:
            with pytest.raises(ValueError):
                build_private_scheduler_trigger(**candidate)


# --- scheduled vs event-driven -----------------------------------------------------------------------------------

def test_scheduled_trigger_discrimination() -> None:
    built = build()
    assert built.scheduled_for == T() and (built.event_cause_kind, built.cause_key, built.cause_available_at) == (None, None, None)
    for hybrid in (dict(event_cause_kind=EC.RISK_LIMIT_BREACH), dict(cause_key="x"), dict(cause_available_at=T()),
                   dict(event_cause_kind=EC.RISK_LIMIT_BREACH, cause_key="x", cause_available_at=T())):
        with pytest.raises(ValueError):
            build(**hybrid)
    with pytest.raises(TypeError):
        build(scheduled_for=None)                                                         # a scheduled trigger needs its occurrence instant


def test_event_driven_trigger_discrimination() -> None:
    built = build_private_scheduler_trigger(**event_kwargs())
    assert built.scheduled_for is None and built.event_cause_kind is EC.DISCLOSURE_INGESTED
    assert built.cause_key == "Evt-1" and built.cause_available_at == T()
    with pytest.raises(ValueError):
        build_private_scheduler_trigger(**event_kwargs(scheduled_for=T()))                   # never both
    for missing in ("event_cause_kind", "cause_key", "cause_available_at"):
        with pytest.raises(TypeError):
            build_private_scheduler_trigger(**event_kwargs(**{missing: None}))


def test_every_event_cause_constructs_without_implying_work_or_scope() -> None:
    for cause in EC:
        system = build_private_scheduler_trigger(**event_kwargs(event_cause_kind=cause))
        portfolio = build_private_scheduler_trigger(**{**event_kwargs(event_cause_kind=cause), **dict(
            work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO)})
        assert system.event_cause_kind is cause and system.work_kind is WK.SOURCE_DATA_REFRESH and system.scope is SC.SYSTEM
        assert portfolio.event_cause_kind is cause and portfolio.work_kind is WK.GAME_CHANGER_REVIEW
        with pytest.raises(ValueError):                                                      # a cause does not repair a wrong pairing
            build_private_scheduler_trigger(**event_kwargs(event_cause_kind=cause, work_kind=WK.GAME_CHANGER_REVIEW))


# --- cause key ---------------------------------------------------------------------------------------------------

def test_cause_key_fidelity() -> None:
    class SubStr(str):
        pass

    for ok in ("Evt-1", "MiXeD/Case:42", "a b", "x" * 128, "İstanbul-ß", "tx_0001"):
        assert build_private_scheduler_trigger(**event_kwargs(cause_key=ok)).cause_key == ok            # preserved exactly
    for bad in ("", " lead", "trail ", " ", "x" * 129, "a\nb", "a\tb", "nul\x00", "del\x7f", "nel\x85", " x", "x "):
        with pytest.raises(ValueError):
            build_private_scheduler_trigger(**event_kwargs(cause_key=bad))
    for bad in (1, b"x", SubStr("x"), ["x"]):
        with pytest.raises(TypeError):
            build_private_scheduler_trigger(**event_kwargs(cause_key=bad))


# --- timestamps --------------------------------------------------------------------------------------------------

def test_timestamp_hardening_on_both_surfaces() -> None:
    plus9, minus5 = timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))

    class Broken(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("boom")

    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

    class SubDatetime(datetime):
        pass

    surfaces = (("scheduled_for", lambda value: kwargs(scheduled_for=value)),
                ("cause_available_at", lambda value: event_kwargs(cause_available_at=value)))
    for attribute, make in surfaces:
        first = build_private_scheduler_trigger(**make(T(1, 9, tz=UTC)))
        same_instant = build_private_scheduler_trigger(**make(T(1, 18, tz=plus9)))
        other_instant = build_private_scheduler_trigger(**make(T(1, 4, tz=minus5)))
        instants = {getattr(t, attribute).astimezone(UTC) for t in (first, same_instant, other_instant)}
        assert instants == {T(1, 9)}                                                        # one UTC instant, identical admissibility
        for bad in (datetime(2026, 9, 1, 9), datetime(2026, 9, 1, 9, tzinfo=Broken()), datetime(2026, 9, 1, 9, tzinfo=NoOffset()),
                    SubDatetime(2026, 9, 1, 9, tzinfo=UTC), "2026-09-01T09:00:00Z", T().date(), 1):
            with pytest.raises(TypeError):
                build_private_scheduler_trigger(**make(bad))


def test_past_and_future_instants_are_accepted_without_reading_a_clock() -> None:
    for year in (1971, 2026, 2999):
        stamp = datetime(year, 1, 1, tzinfo=UTC)
        assert build(scheduled_for=stamp).scheduled_for == stamp
        assert build_private_scheduler_trigger(**event_kwargs(cause_available_at=stamp)).cause_available_at == stamp


def test_the_scheduled_instant_is_preserved_exactly_and_not_validated_against_a_slot() -> None:
    odd = T(1, 3, 7, 11, 13)
    assert build(scheduled_for=odd).scheduled_for is odd


# --- policy identity / idempotency -------------------------------------------------------------------------------

def test_policy_identity() -> None:
    class SubStr(str):
        pass

    for ok in ("private.scheduler.v1", "x", "a" * 128, "0-a_b.c", "other.policy.family"):
        assert build(policy_key=ok).policy_key == ok
    for bad in ("", " x", "x ", "X", "a" * 129, "a b", "x\n", "-x", "_x", "https://x.y"):
        with pytest.raises(ValueError):
            build(policy_key=bad)
    for bad in (None, 1, b"x", SubStr("x")):
        with pytest.raises(TypeError):
            build(policy_key=bad)
    for ok in (1, 2, 10 ** 12):
        assert build(policy_revision=ok).policy_revision == ok
    for bad in (0, -1):
        with pytest.raises(ValueError):
            build(policy_revision=bad)
    for bad in (True, False, 1.0, "1", Decimal(1), None, [1]):
        with pytest.raises(TypeError):
            build(policy_revision=bad)


def test_idempotency_hash_is_an_opaque_lowercase_sha256_reference() -> None:
    class SubStr(str):
        pass

    assert build(idempotency_sha256="0123456789abcdef" * 4).idempotency_sha256 == "0123456789abcdef" * 4
    for bad in ("D" * 64, "d" * 63, "d" * 65, "g" * 64, "", "d" * 64 + "\n", " " + "d" * 63):
        with pytest.raises(ValueError):
            build(idempotency_sha256=bad)
    for bad in (None, 1, b"d" * 64, SubStr("d" * 64)):
        with pytest.raises(TypeError):
            build(idempotency_sha256=bad)
    assert build(idempotency_sha256="e" * 64) != build()                                    # identity participates in equality, nothing is derived


def test_direct_construction_enforces_the_full_contract() -> None:
    good = build_private_scheduler_trigger(**event_kwargs())
    fields = {f.name: getattr(good, f.name) for f in dataclasses.fields(good)}
    assert PrivateSchedulerTrigger(**fields) == good
    for change in (dict(scheduled_for=T()), dict(cause_key=None), dict(owner_id=OWNER), dict(policy_revision=0), dict(policy_revision=True),
                   dict(idempotency_sha256="D" * 64), dict(work_kind=WK.GAME_CHANGER_REVIEW), dict(cause_available_at=datetime(2026, 9, 1))):
        with pytest.raises((ValueError, TypeError)):
            PrivateSchedulerTrigger(**{**fields, **change})
    assert build_private_scheduler_trigger(**event_kwargs()) == good                        # deterministic


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_trigger.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_the_standard_library_subset_only() -> None:
    names: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "uuid"}
    assert not any(name.startswith("backend") for name in names)                              # no other Sentinax module in 24A


def test_no_ambient_clock_loop_sleep_network_or_arithmetic() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "time", "sleep", "fromtimestamp", "monotonic", "perf_counter", "float", "random"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.comprehension, ast.AsyncFunctionDef, ast.Await))]
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert ints <= {1, 128}                                                                  # no polling interval, no hardcoded clock slot
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]


_FRAGMENTS = ("scheduler", "job_queue", "chat_orchestrator", "legacy", "watchlist", "radar", "macd", "bearish", "telegram", "email", "webhook",
              "notif", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis", "database", "repository", "cron", "holiday", "calendar",
              "next_run", "previous_run", "interval", "poll", "retry", "backoff", "jitter", "misfire", "dispatch", "execut",
              "pending", "running", "success", "failed", "dlq", "portfolio_snapshot", "target_weight", "allocation", "rebalance",
              "optimizer", "provider", "game_changer_gate")
_EXACT = {"lease", "lock", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today"}


def test_no_legacy_scheduler_runtime_state_retry_dispatch_or_trade_surface() -> None:
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
    allowed = {"PrivateSchedulerTrigger", "PrivateSchedulerTriggerKind", "PrivateSchedulerScope", "PrivateSchedulerWorkKind",
               "PrivateSchedulerEventCauseKind", "build_private_scheduler_trigger"}                # spec-mandated names containing "scheduler"
    for identifier in identifiers - allowed:
        lowered = identifier.lower()
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    enum_members = {m.name.lower() for enum in (TK, SC, WK, EC) for m in enum}
    assert not enum_members & {"pending", "running", "success", "failed", "retrying", "dlq", "polling", "continuous"}
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_private_scheduler_trigger")
    assert not function.args.args and len(function.args.kwonlyargs) == 12


def test_documents_the_envelope_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("occurrence", "scheduled", "event-driven", "owner", "idempotency", "no ambient clock", "no schedule calculation",
                   "no dispatch", "legacy", "24B", "24C", "24D", "policy_key", "cause_available_at"):
        assert needle in doc, needle
