"""
backend/tests/test_scheduler_scheduled_occurrence.py
====================================================
Phase 24B1: timezone-safe materialization of ONE explicit local wall-clock occurrence into the closed Phase 24A trigger, with
deterministic domain-separated idempotency. IANA zone authority, nonexistent local times always rejected, ambiguous local times only by
explicit policy. No recurrence, no market calendar, no ambient clock, no dispatch.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from backend.engine.private import scheduler_scheduled_occurrence as module_under_test
from backend.engine.private.scheduler_scheduled_occurrence import (
    PrivateSchedulerAmbiguousTimePolicy,
    PrivateSchedulerScheduledOccurrence,
    build_private_scheduler_scheduled_occurrence,
)
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
AP, SC, WK, TK = PrivateSchedulerAmbiguousTimePolicy, PrivateSchedulerScope, PrivateSchedulerWorkKind, PrivateSchedulerTriggerKind
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
NY = ZoneInfo("America/New_York")


def kwargs(**changes):
    base = dict(schedule_key="private.schedule.test", schedule_revision=1, timezone_key="UTC", local_date=date(2026, 10, 2),
                local_time=time(9, 55), ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM,
                owner_id=None, portfolio_id=None, policy_key="private.scheduler.v1", policy_revision=1)
    base.update(changes)
    return base


def portfolio(**changes):
    return kwargs(**{**dict(work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO), **changes})


def build(**changes) -> PrivateSchedulerScheduledOccurrence:
    return build_private_scheduler_scheduled_occurrence(**kwargs(**changes))


def ny(day: date, wall: time, policy=AP.REJECT) -> PrivateSchedulerScheduledOccurrence:
    return build(timezone_key="America/New_York", local_date=day, local_time=wall, ambiguous_time_policy=policy)


# --- enum / stored shape -----------------------------------------------------------------------------------------

def test_enum_stored_fields_and_builder_shape() -> None:
    assert [(m.name, m.value) for m in AP] == [("REJECT", "reject"), ("EARLIER", "earlier"), ("LATER", "later")]
    fields = dataclasses.fields(PrivateSchedulerScheduledOccurrence)
    assert [f.name for f in fields] == ["schedule_key", "schedule_revision", "timezone_key", "local_date", "local_time",
                                        "ambiguous_time_policy", "trigger"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        build().schedule_key = "x"  # type: ignore[misc]
    parameters = list(inspect.signature(build_private_scheduler_scheduled_occurrence).parameters.values())
    assert [p.name for p in parameters] == ["schedule_key", "schedule_revision", "timezone_key", "local_date", "local_time",
                                            "ambiguous_time_policy", "work_kind", "scope", "owner_id", "portfolio_id", "policy_key",
                                            "policy_revision"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    for forbidden in ("scheduled_for", "idempotency_sha256"):                                  # derived, never caller supplied
        with pytest.raises(TypeError):
            build_private_scheduler_scheduled_occurrence(**kwargs(), **{forbidden: None})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_private_scheduler_scheduled_occurrence(*kwargs().values())  # type: ignore[misc]


def test_ambiguous_policy_is_an_exact_enum() -> None:
    for bad in ("reject", None, 1, WK.SOURCE_DATA_REFRESH):
        with pytest.raises(TypeError):
            build(ambiguous_time_policy=bad)


# --- schedule identity -------------------------------------------------------------------------------------------

def test_schedule_key_and_revision() -> None:
    class SubStr(str):
        pass

    for ok in ("private.schedule.pre_open", "x", "a" * 128, "0-a_b.c"):
        assert build(schedule_key=ok).schedule_key == ok
    for bad in ("", " x", "x ", "X", "a" * 129, "a b", "x\n", "-x"):
        with pytest.raises(ValueError):
            build(schedule_key=bad)
    for bad in (None, 1, b"x", SubStr("x")):
        with pytest.raises(TypeError):
            build(schedule_key=bad)
    for ok in (1, 2, 10 ** 9):
        assert build(schedule_revision=ok).schedule_revision == ok
    for bad in (0, -1):
        with pytest.raises(ValueError):
            build(schedule_revision=bad)
    for bad in (True, False, 1.0, "1", None):
        with pytest.raises(TypeError):
            build(schedule_revision=bad)


# --- timezone authority ------------------------------------------------------------------------------------------

def test_timezone_key_is_an_exact_iana_key_with_no_fallback() -> None:
    class SubStr(str):
        pass

    for ok in ("Europe/Istanbul", "America/New_York", "UTC"):
        assert build(timezone_key=ok).timezone_key == ok
    for bad in ("", " UTC", "UTC ", "Not/AZone", "utcx", "+03:00", "../etc/passwd", "Europe", "x" * 129, "UTC\n", "Europe\tIstanbul"):
        with pytest.raises(ValueError):
            build(timezone_key=bad)
    for bad in (None, 1, b"UTC", SubStr("UTC"), ZoneInfo("UTC")):
        with pytest.raises(TypeError):
            build(timezone_key=bad)


def test_istanbul_slot_materializes_deterministically() -> None:
    occurrence = build(timezone_key="Europe/Istanbul", local_date=date(2026, 10, 2), local_time=time(9, 55))
    instant = occurrence.trigger.scheduled_for
    assert type(instant) is datetime and instant.tzinfo is UTC
    back = instant.astimezone(ZoneInfo("Europe/Istanbul"))                                # the timezone database result, not a table
    assert (back.date(), back.time().replace(tzinfo=None)) == (date(2026, 10, 2), time(9, 55))
    assert occurrence == build(timezone_key="Europe/Istanbul", local_date=date(2026, 10, 2), local_time=time(9, 55))


# --- local date / time strictness --------------------------------------------------------------------------------

def test_local_date_and_time_strictness() -> None:
    class SubDate(date):
        pass

    class SubTime(time):
        pass

    for bad in (datetime(2026, 10, 2), datetime(2026, 10, 2, tzinfo=UTC), SubDate(2026, 10, 2), "2026-10-02", None, 20261002):
        with pytest.raises(TypeError):
            build(local_date=bad)
    for bad in (time(9, 55, tzinfo=UTC), SubTime(9, 55), "09:55", None, 955, datetime(2026, 1, 1, 9, 55)):
        with pytest.raises(TypeError):
            build(local_time=bad)
    with pytest.raises(ValueError):
        build(local_time=time(9, 55, fold=1))
    assert build(local_time=time(9, 55, 7, 123456)).local_time == time(9, 55, 7, 123456)    # microseconds are preserved exactly


def test_extreme_dates_fail_closed() -> None:
    with pytest.raises(ValueError):
        build(timezone_key="America/New_York", local_date=date.max, local_time=time(23, 59))
    with pytest.raises(ValueError):
        build(timezone_key="Europe/Istanbul", local_date=date.min, local_time=time(0, 0))


# --- DST resolution ----------------------------------------------------------------------------------------------

def test_unambiguous_wall_time_resolves_identically_under_every_policy() -> None:
    results = {p: ny(date(2024, 6, 15), time(12, 0), p) for p in AP}
    instants = {o.trigger.scheduled_for for o in results.values()}
    assert instants == {datetime(2024, 6, 15, 16, 0, tzinfo=UTC)}                          # EDT (UTC-4)
    assert len({o.trigger.idempotency_sha256 for o in results.values()}) == 3               # the policy is versioned schedule configuration


def test_nonexistent_wall_time_is_rejected_under_every_policy_without_shifting() -> None:
    for policy in AP:
        with pytest.raises(ValueError):
            ny(date(2024, 3, 10), time(2, 30), policy)
        with pytest.raises(ValueError):
            ny(date(2024, 3, 10), time(2, 0), policy)                                       # first skipped instant
        with pytest.raises(ValueError):
            ny(date(2024, 3, 10), time(2, 59, 59, 999999), policy)
    assert ny(date(2024, 3, 10), time(1, 59, 59, 999999)).trigger.scheduled_for == datetime(2024, 3, 10, 6, 59, 59, 999999, tzinfo=UTC)
    assert ny(date(2024, 3, 10), time(3, 0)).trigger.scheduled_for == datetime(2024, 3, 10, 7, 0, tzinfo=UTC)


def test_ambiguous_wall_time_follows_the_explicit_policy_by_utc_ordering() -> None:
    with pytest.raises(ValueError):
        ny(date(2024, 11, 3), time(1, 30), AP.REJECT)
    earlier = ny(date(2024, 11, 3), time(1, 30), AP.EARLIER).trigger.scheduled_for
    later = ny(date(2024, 11, 3), time(1, 30), AP.LATER).trigger.scheduled_for
    assert earlier < later and later - earlier == timedelta(hours=1)
    assert earlier == datetime(2024, 11, 3, 5, 30, tzinfo=UTC) and later == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)   # EDT then EST
    for instant in (earlier, later):
        assert instant.astimezone(NY).replace(tzinfo=None) == datetime(2024, 11, 3, 1, 30)   # both really are 01:30 local
    with pytest.raises(ValueError):
        ny(date(2024, 11, 3), time(1, 0), AP.REJECT)                                         # the whole repeated hour is ambiguous
    assert ny(date(2024, 11, 3), time(2, 0), AP.REJECT).trigger.scheduled_for == datetime(2024, 11, 3, 7, 0, tzinfo=UTC)


def test_round_trip_requires_exact_wall_fields_including_microseconds() -> None:
    for wall in (time(1, 30, 0, 1), time(1, 30, 59, 999999)):
        e, l = ny(date(2024, 11, 3), wall, AP.EARLIER), ny(date(2024, 11, 3), wall, AP.LATER)
        for occurrence in (e, l):
            back = occurrence.trigger.scheduled_for.astimezone(NY)
            assert (back.date(), back.hour, back.minute, back.second, back.microsecond) == (date(2024, 11, 3), 1, wall.minute, wall.second, wall.microsecond)
    assert module_under_test.__doc__ and "round" in module_under_test.__doc__


def test_materialized_trigger_is_scheduled_utc_and_event_free() -> None:
    trigger = build(timezone_key="Europe/Istanbul").trigger
    assert trigger.trigger_kind is TK.SCHEDULED and trigger.scheduled_for.tzinfo is UTC
    assert (trigger.event_cause_kind, trigger.cause_key, trigger.cause_available_at) == (None, None, None)
    assert type(trigger) is PrivateSchedulerTrigger and (trigger.policy_key, trigger.policy_revision) == ("private.scheduler.v1", 1)


# --- Phase 24A delegation ----------------------------------------------------------------------------------------

def test_phase_24a_remains_the_work_scope_owner_policy_authority() -> None:
    class SubUUID(UUID):
        pass

    occurrence = build_private_scheduler_scheduled_occurrence(**portfolio())
    assert (occurrence.trigger.owner_id, occurrence.trigger.portfolio_id, occurrence.trigger.scope) == (OWNER, PORTFOLIO, SC.PORTFOLIO)
    for work, scope in ((WK.SOURCE_DATA_REFRESH, SC.PORTFOLIO), (WK.PORTFOLIO_ANALYSIS_REFRESH, SC.SYSTEM), (WK.GAME_CHANGER_REVIEW, SC.SYSTEM),
                        (WK.PORTFOLIO_HEALTH_CHECK, SC.SYSTEM)):
        owner, pid = (OWNER, PORTFOLIO) if scope is SC.PORTFOLIO else (None, None)
        with pytest.raises(ValueError):
            build(work_kind=work, scope=scope, owner_id=owner, portfolio_id=pid)
    with pytest.raises(ValueError):
        build(owner_id=OWNER)                                                                # SYSTEM with an owner
    for bad in ({"owner_id": None}, {"portfolio_id": None}, {"owner_id": str(OWNER)}, {"portfolio_id": SubUUID(int=2)}):
        with pytest.raises(TypeError):
            build_private_scheduler_scheduled_occurrence(**portfolio(**bad))
    for bad in ({"policy_key": "X"}, {"policy_revision": 0}):
        with pytest.raises(ValueError):
            build(**bad)
    for bad in ({"work_kind": "source_data_refresh"}, {"scope": "system"}, {"policy_revision": True}):
        with pytest.raises(TypeError):
            build(**bad)


# --- deterministic idempotency -----------------------------------------------------------------------------------

def test_hash_is_deterministic_and_a_lowercase_sha256() -> None:
    first, second = build(), build()
    assert first == second and first.trigger.idempotency_sha256 == second.trigger.idempotency_sha256
    assert len(first.trigger.idempotency_sha256) == 64 and set(first.trigger.idempotency_sha256) <= set("0123456789abcdef")


def test_every_schedule_identity_dimension_changes_the_hash() -> None:
    base = build().trigger.idempotency_sha256
    variants = [
        dict(schedule_key="private.schedule.other"), dict(schedule_revision=2), dict(timezone_key="Etc/UTC"),
        dict(local_date=date(2026, 10, 3)), dict(local_time=time(9, 56)), dict(ambiguous_time_policy=AP.EARLIER),
        dict(ambiguous_time_policy=AP.LATER), dict(policy_key="private.scheduler.v2"), dict(policy_revision=2),
        dict(local_time=time(9, 55, 0, 1)),
    ]
    hashes = {base}
    for change in variants:
        value = build(**change).trigger.idempotency_sha256
        assert value != base, change
        hashes.add(value)
    assert len(hashes) == len(variants) + 1                                                  # and all variants differ from each other
    portfolio_base = build_private_scheduler_scheduled_occurrence(**portfolio()).trigger.idempotency_sha256
    portfolio_variants = [portfolio(work_kind=WK.PORTFOLIO_HEALTH_CHECK), portfolio(work_kind=WK.GAME_CHANGER_REVIEW),
                          portfolio(owner_id=UUID(int=3)), portfolio(portfolio_id=UUID(int=4))]
    values = {portfolio_base, base}
    for change in portfolio_variants:
        value = build_private_scheduler_scheduled_occurrence(**change).trigger.idempotency_sha256
        assert value not in values, change
        values.add(value)
    assert portfolio_base != base                                                            # SYSTEM vs PORTFOLIO identity differs


def test_resolved_utc_instant_participates_in_the_hash() -> None:
    a, b = ny(date(2024, 11, 3), time(1, 30), AP.EARLIER), ny(date(2024, 11, 3), time(1, 30), AP.LATER)
    assert a.trigger.scheduled_for != b.trigger.scheduled_for and a.trigger.idempotency_sha256 != b.trigger.idempotency_sha256


def test_same_utc_instant_with_different_configuration_has_a_different_identity() -> None:
    utc_slot = build(timezone_key="UTC", local_date=date(2026, 10, 2), local_time=time(6, 55))
    istanbul_slot = build(timezone_key="Europe/Istanbul", local_date=date(2026, 10, 2), local_time=time(9, 55))
    etc_slot = build(timezone_key="Etc/UTC", local_date=date(2026, 10, 2), local_time=time(6, 55))
    renamed = build(schedule_key="private.schedule.renamed", timezone_key="UTC", local_date=date(2026, 10, 2), local_time=time(6, 55))
    assert utc_slot.trigger.scheduled_for == istanbul_slot.trigger.scheduled_for == etc_slot.trigger.scheduled_for == renamed.trigger.scheduled_for
    assert len({o.trigger.idempotency_sha256 for o in (utc_slot, istanbul_slot, etc_slot, renamed)}) == 4


def test_domain_separation_and_canonical_serialization_are_explicit() -> None:
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    assert "sentinax.private.scheduler.scheduled-occurrence.v1" in source
    assert "sort_keys=True" in source and 'separators=(",", ":")' in source and "sha256" in source
    assert "pickle" not in source and "repr(" not in source and "hash(" not in source.replace("sha256(", "")


# --- direct forge resistance -------------------------------------------------------------------------------------

def test_direct_construction_rejects_forged_triggers() -> None:
    good = build_private_scheduler_scheduled_occurrence(**portfolio())
    fields = {f.name: getattr(good, f.name) for f in dataclasses.fields(good)}
    assert PrivateSchedulerScheduledOccurrence(**fields) == good
    trigger = good.trigger

    def forged(**changes) -> PrivateSchedulerTrigger:
        values = {f.name: getattr(trigger, f.name) for f in dataclasses.fields(trigger)}
        values.update(changes)
        return build_private_scheduler_trigger(**values)

    cases = [
        forged(scheduled_for=trigger.scheduled_for + timedelta(minutes=1)),
        forged(idempotency_sha256="0" * 64),
        forged(policy_revision=2),
        forged(policy_key="private.scheduler.other"),
        forged(work_kind=WK.PORTFOLIO_HEALTH_CHECK),
        forged(owner_id=UUID(int=9)),
        forged(scheduled_for=trigger.scheduled_for.astimezone(timezone(timedelta(hours=3)))),   # right instant, non-UTC representation
        build_private_scheduler_trigger(
            trigger_kind=TK.EVENT_DRIVEN, work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO,
            scheduled_for=None, event_cause_kind=PrivateSchedulerEventCauseKind.PORTFOLIO_CHANGED, cause_key="k",
            cause_available_at=trigger.scheduled_for, policy_key="private.scheduler.v1", policy_revision=1, idempotency_sha256="a" * 64),
    ]
    for bad in cases:
        with pytest.raises(ValueError):
            PrivateSchedulerScheduledOccurrence(**{**fields, "trigger": bad})
    other_schedule = build(schedule_key="private.schedule.other").trigger                   # same instant, another schedule's identity
    assert other_schedule.scheduled_for == good.trigger.scheduled_for
    with pytest.raises(ValueError):
        PrivateSchedulerScheduledOccurrence(**{**fields, "trigger": other_schedule})
    own_system = PrivateSchedulerScheduledOccurrence(**{**fields, "trigger": build().trigger})   # a trigger consistent with the stored schedule IS valid
    assert own_system.trigger.scope is SC.SYSTEM
    for bad in (object(), None, {"x": 1}):
        with pytest.raises(TypeError):
            PrivateSchedulerScheduledOccurrence(**{**fields, "trigger": bad})
    for change in (dict(schedule_revision=0), dict(timezone_key="Not/AZone"), dict(local_time=time(9, 55, fold=1)), dict(local_date=datetime(2026, 10, 2)),
                   dict(ambiguous_time_policy="reject"), dict(schedule_key="X")):
        with pytest.raises((ValueError, TypeError)):
            PrivateSchedulerScheduledOccurrence(**{**fields, **change})
    for change in (dict(local_date=date(2026, 10, 3)), dict(local_time=time(10, 0)), dict(schedule_key="private.schedule.other")):
        with pytest.raises(ValueError):                                                      # stored schedule no longer explains the trigger
            PrivateSchedulerScheduledOccurrence(**{**fields, **change})


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_scheduled_occurrence.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_24a_surface_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "hashlib", "json", "re", "uuid", "zoneinfo",
                     "backend.engine.private.scheduler_trigger"}
    assert imported["backend.engine.private.scheduler_trigger"] <= {
        "PrivateSchedulerScope", "PrivateSchedulerTrigger", "PrivateSchedulerTriggerKind", "PrivateSchedulerWorkKind", "build_private_scheduler_trigger"}
    assert imported["datetime"] <= {"date", "datetime", "time", "timezone"}
    for forbidden in ("infrastructure", "job_queue", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis", "socket", "urllib",
                      "kap", "mkk", "game_changer", "allocation", "rebalance", "portfolio", "analysis_pit", "domain", "pickle"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_ambient_clock_recurrence_calendar_dispatch_or_legacy_surface() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "perf_counter", "sleep", "weekday", "isoweekday", "timestamp",
                        "float", "random", "pickle", "getcontext"}
    assert "time" in {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}               # the datetime.time TYPE is deliberately allowed
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert not ints & {840, 14, 955, 1015, 1035, 1830, 2310, 10}                              # no polling interval, no methodology clock slot


_FRAGMENTS = ("scheduler_interval", "legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio",
              "fastapi", "supabase", "redis", "database", "repository", "cron", "rrule", "recurr", "next_run", "previous_run", "business",
              "holiday", "calendar", "market", "trading", "dispatch", "execut", "pending", "running", "success", "failed", "retry", "backoff",
              "dlq", "game_changer", "allocation", "rebalance", "optimizer", "provider")
_EXACT = {"lease", "lock", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today"}


def test_no_recurrence_calendar_dispatch_runtime_or_legacy_identifiers() -> None:
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
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_private_scheduler_scheduled_occurrence")
    assert not function.args.args and len(function.args.kwonlyargs) == 12


def test_documents_the_materialization_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("IANA", "ambiguous", "nonexistent", "no silent", "UTC", "idempotency", "domain", "24B2", "no recurrence",
                   "no market calendar", "no ambient clock", "no dispatch", "legacy", "tzdata", "fold", "round"):
        assert needle in doc, needle
