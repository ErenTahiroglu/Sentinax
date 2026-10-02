"""
backend/tests/test_scheduler_recurrence.py
==========================================
Phase 24B2: versioned CIVIL recurrence (daily / weekly / monthly day-of-month) evaluated against ONE explicit local date, materialized only
through the closed Phase 24B1. The Gregorian civil calendar is not a market calendar: weekends are ordinary eligible dates, short months skip,
there is no next-run search, no ambient clock and no DST logic here.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_recurrence as module_under_test
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerCivilDateDecision,
    PrivateSchedulerCivilDateEligibility,
    PrivateSchedulerRecurrence,
    PrivateSchedulerRecurrenceKind,
    PrivateSchedulerWeekday,
    build_private_scheduler_recurrence,
    evaluate_private_scheduler_civil_date,
    materialize_private_scheduler_civil_occurrence,
)
from backend.engine.private.scheduler_scheduled_occurrence import (
    PrivateSchedulerAmbiguousTimePolicy,
    PrivateSchedulerScheduledOccurrence,
    build_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_trigger import PrivateSchedulerScope, PrivateSchedulerWorkKind
from backend.tests.invariants import static_guards as sg

RK, WD, EL = PrivateSchedulerRecurrenceKind, PrivateSchedulerWeekday, PrivateSchedulerCivilDateEligibility
AP, SC, WK = PrivateSchedulerAmbiguousTimePolicy, PrivateSchedulerScope, PrivateSchedulerWorkKind
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
ALL_DAYS = tuple(WD)
WEEKDAYS = (WD.MONDAY, WD.TUESDAY, WD.WEDNESDAY, WD.THURSDAY, WD.FRIDAY)


def kwargs(**changes):
    base = dict(schedule_key="private.schedule.test", schedule_revision=1, timezone_key="UTC", local_time=time(9, 55),
                ambiguous_time_policy=AP.REJECT, recurrence_kind=RK.DAILY, weekdays=(), day_of_month=None,
                work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None, policy_key="private.scheduler.v1",
                policy_revision=1)
    base.update(changes)
    return base


def build(**changes) -> PrivateSchedulerRecurrence:
    return build_private_scheduler_recurrence(**kwargs(**changes))


def weekly(*days) -> PrivateSchedulerRecurrence:
    return build(recurrence_kind=RK.WEEKLY, weekdays=tuple(days))


def monthly(day: int) -> PrivateSchedulerRecurrence:
    return build(recurrence_kind=RK.MONTHLY_DAY_OF_MONTH, day_of_month=day)


def decide(recurrence, local_date) -> PrivateSchedulerCivilDateDecision:
    return evaluate_private_scheduler_civil_date(recurrence=recurrence, local_date=local_date)


MONDAY = date(2026, 10, 5)
WEEK = [MONDAY + timedelta(days=i) for i in range(7)]                                    # Monday 2026-10-05 .. Sunday 2026-10-11


# --- enums / shapes ----------------------------------------------------------------------------------------------

def test_enums_and_stored_shapes() -> None:
    assert [(m.name, m.value) for m in RK] == [("DAILY", "daily"), ("WEEKLY", "weekly"), ("MONTHLY_DAY_OF_MONTH", "monthly_day_of_month")]
    assert [(m.name, m.value) for m in WD] == [(d.upper(), d) for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
    assert [(m.name, m.value) for m in EL] == [("ELIGIBLE", "eligible"), ("INELIGIBLE", "ineligible")]
    recurrence_fields = dataclasses.fields(PrivateSchedulerRecurrence)
    assert [f.name for f in recurrence_fields] == ["schedule_key", "schedule_revision", "timezone_key", "local_time", "ambiguous_time_policy",
                                                   "recurrence_kind", "weekdays", "day_of_month", "work_kind", "scope", "owner_id",
                                                   "portfolio_id", "policy_key", "policy_revision"]
    decision_fields = dataclasses.fields(PrivateSchedulerCivilDateDecision)
    assert [f.name for f in decision_fields] == ["recurrence", "local_date", "eligibility"]
    for fields in (recurrence_fields, decision_fields):
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        build().schedule_key = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decide(build(), MONDAY).eligibility = EL.INELIGIBLE  # type: ignore[misc]
    for function, count in ((build_private_scheduler_recurrence, 14), (evaluate_private_scheduler_civil_date, 2),
                            (materialize_private_scheduler_civil_occurrence, 1)):
        parameters = list(inspect.signature(function).parameters.values())
        assert len(parameters) == count and all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    with pytest.raises(TypeError):
        build_private_scheduler_recurrence(**kwargs(), start_date=date(2026, 1, 1))  # type: ignore[call-arg]       # no active date range


def test_kind_and_weekday_members_are_exact() -> None:
    class SubTuple(tuple):
        pass

    for bad in ("daily", None, 1, WD.MONDAY):
        with pytest.raises(TypeError):
            build(recurrence_kind=bad)
    for bad in (["monday"], None, SubTuple(), ("monday",), (RK.DAILY,), [WD.MONDAY]):
        with pytest.raises(TypeError):
            build(recurrence_kind=RK.WEEKLY, weekdays=bad)


# --- recurrence shapes -------------------------------------------------------------------------------------------

def test_daily_shape() -> None:
    assert build().recurrence_kind is RK.DAILY
    with pytest.raises(ValueError):
        build(weekdays=(WD.MONDAY,))
    for day in (1, 15, 31):
        with pytest.raises(ValueError):
            build(day_of_month=day)


def test_weekly_shape() -> None:
    for days in ((WD.MONDAY,), (WD.MONDAY, WD.WEDNESDAY, WD.FRIDAY), WEEKDAYS, ALL_DAYS, (WD.SATURDAY, WD.SUNDAY)):
        assert weekly(*days).weekdays == days
    for bad in ((), (WD.FRIDAY, WD.MONDAY), (WD.MONDAY, WD.MONDAY), (WD.TUESDAY, WD.MONDAY, WD.WEDNESDAY), tuple(reversed(ALL_DAYS))):
        with pytest.raises(ValueError):
            build(recurrence_kind=RK.WEEKLY, weekdays=bad)
    with pytest.raises(ValueError):
        build(recurrence_kind=RK.WEEKLY, weekdays=(WD.MONDAY,), day_of_month=1)


def test_monthly_shape() -> None:
    for day in (1, 15, 28, 29, 30, 31):
        assert monthly(day).day_of_month == day
    for bad in (0, 32, -1):
        with pytest.raises(ValueError):
            monthly(bad)
    for bad in (True, False, 1.0, Decimal(1), "1", None, [1]):
        with pytest.raises(TypeError):
            monthly(bad)
    with pytest.raises(ValueError):
        build(recurrence_kind=RK.MONTHLY_DAY_OF_MONTH, day_of_month=15, weekdays=(WD.MONDAY,))


# --- closed-contract validation at construction ------------------------------------------------------------------

def test_closed_contract_fields_fail_at_construction_not_at_first_occurrence() -> None:
    class SubStr(str):
        pass

    class SubUUID(UUID):
        pass

    for change, error in (
        (dict(schedule_key="X"), ValueError), (dict(schedule_key=1), TypeError), (dict(schedule_key=SubStr("x")), TypeError),
        (dict(schedule_revision=0), ValueError), (dict(schedule_revision=True), TypeError),
        (dict(timezone_key="Not/AZone"), ValueError), (dict(timezone_key=" UTC"), ValueError), (dict(timezone_key=None), TypeError),
        (dict(local_time=time(9, 55, fold=1)), ValueError), (dict(local_time=time(9, 55, tzinfo=timezone.utc)), TypeError), (dict(local_time="09:55"), TypeError),
        (dict(ambiguous_time_policy="reject"), TypeError),
        (dict(policy_key="X"), ValueError), (dict(policy_revision=0), ValueError), (dict(policy_revision=True), TypeError),
        (dict(work_kind="source_data_refresh"), TypeError), (dict(scope="system"), TypeError),
        (dict(owner_id=OWNER), ValueError),                                                         # SYSTEM scope with an owner
        (dict(work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH), ValueError),                                # portfolio work in SYSTEM scope
        (dict(work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=None), TypeError),
        (dict(work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=SubUUID(int=2)), TypeError),
        (dict(work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO), ValueError),
    ):
        with pytest.raises(error):
            build(**change)
    portfolio = build(work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO)
    assert (portfolio.owner_id, portfolio.portfolio_id) == (OWNER, PORTFOLIO)


def test_local_validation_has_exact_parity_with_the_closed_24b1_authority() -> None:
    """Small local checks must never loosen or tighten the closed 24B1 contract: same exception class for the same input."""
    samples = [
        ("schedule_key", ["X", "", " x", "a" * 129, 1, None]), ("schedule_revision", [0, -1, True, 1.0, "1", None]),
        ("timezone_key", ["Not/AZone", "", " UTC", "UTC ", "Europe", "../x", "x" * 129, 1, None, "UTC\n"]),
        ("local_time", [time(9, 55, fold=1), time(9, 55, tzinfo=timezone.utc), "09:55", None]),
        ("ambiguous_time_policy", ["reject", None, 1]),
    ]
    for field, bads in samples:
        for bad in bads:
            with pytest.raises(Exception) as local:
                build(**{field: bad})
            occurrence_kwargs = dict(
                schedule_key="private.schedule.test", schedule_revision=1, timezone_key="UTC", local_date=date(2026, 10, 2), local_time=time(9, 55),
                ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
                policy_key="private.scheduler.v1", policy_revision=1)
            occurrence_kwargs[field] = bad
            with pytest.raises(Exception) as closed:
                build_private_scheduler_scheduled_occurrence(**occurrence_kwargs)
            assert type(local.value) is type(closed.value), (field, bad)
    for good in ("a", "a" * 128, "0-a_b.c"):
        assert build(schedule_key=good).schedule_key == good
        assert build_private_scheduler_scheduled_occurrence(**{**dict(
            schedule_key=good, schedule_revision=1, timezone_key="UTC", local_date=date(2026, 10, 2), local_time=time(9, 55),
            ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
            policy_key="private.scheduler.v1", policy_revision=1)}).schedule_key == good


def test_a_valid_recurrence_is_not_rejected_for_a_dst_sensitive_wall_time() -> None:
    nonexistent_on_one_day = build(timezone_key="America/New_York", local_time=time(2, 30))
    ambiguous_on_one_day = build(timezone_key="America/New_York", local_time=time(1, 30))
    assert nonexistent_on_one_day.local_time == time(2, 30) and ambiguous_on_one_day.ambiguous_time_policy is AP.REJECT


# --- eligibility -------------------------------------------------------------------------------------------------

def test_daily_is_eligible_for_every_civil_date_including_weekends() -> None:
    recurrence = build()
    for stamp in (*WEEK, date(2024, 2, 29), date(1971, 1, 1), date(2999, 12, 31), date(2026, 1, 1)):
        assert decide(recurrence, stamp).eligibility is EL.ELIGIBLE


def test_weekly_eligibility_matches_exactly_the_listed_weekdays() -> None:
    assert [d.weekday() for d in WEEK] == [0, 1, 2, 3, 4, 5, 6]
    recurrence = weekly(WD.MONDAY, WD.WEDNESDAY, WD.FRIDAY)
    assert [decide(recurrence, d).eligibility for d in WEEK] == [EL.ELIGIBLE, EL.INELIGIBLE, EL.ELIGIBLE, EL.INELIGIBLE, EL.ELIGIBLE,
                                                                  EL.INELIGIBLE, EL.INELIGIBLE]
    for index, day in enumerate(WD):
        single = weekly(day)
        assert [decide(single, d).eligibility is EL.ELIGIBLE for d in WEEK] == [i == index for i in range(7)]
    assert all(decide(weekly(*ALL_DAYS), d).eligibility is EL.ELIGIBLE for d in WEEK)


def test_weekend_is_civil_eligibility_not_market_closure() -> None:
    sunday, saturday = WEEK[6], WEEK[5]
    assert decide(build(), sunday).eligibility is EL.ELIGIBLE
    assert decide(weekly(WD.SUNDAY), sunday).eligibility is EL.ELIGIBLE
    assert decide(weekly(WD.SATURDAY, WD.SUNDAY), saturday).eligibility is EL.ELIGIBLE
    assert decide(weekly(*WEEKDAYS), sunday).eligibility is EL.INELIGIBLE                       # absent from the explicit list, not "closed"


def test_monthly_eligibility_across_months() -> None:
    recurrence = monthly(15)
    for year, month in ((2026, 1), (2026, 2), (2026, 10), (2024, 2), (2025, 12)):
        assert decide(recurrence, date(year, month, 14)).eligibility is EL.INELIGIBLE
        assert decide(recurrence, date(year, month, 15)).eligibility is EL.ELIGIBLE
        assert decide(recurrence, date(year, month, 16)).eligibility is EL.INELIGIBLE


def test_short_months_skip_without_any_adjustment() -> None:
    thirty_first = monthly(31)
    assert all(decide(thirty_first, date(2026, 4, d)).eligibility is EL.INELIGIBLE for d in range(1, 31))   # April has no 31st: no eligible date
    assert decide(thirty_first, date(2026, 5, 31)).eligibility is EL.ELIGIBLE
    assert decide(thirty_first, date(2026, 5, 30)).eligibility is EL.INELIGIBLE
    twenty_ninth = monthly(29)
    assert decide(twenty_ninth, date(2024, 2, 29)).eligibility is EL.ELIGIBLE
    assert all(decide(twenty_ninth, date(2023, 2, d)).eligibility is EL.INELIGIBLE for d in range(1, 29))    # February 2023 has no 29th
    with pytest.raises(ValueError):
        date(2023, 2, 29)                                                                      # nothing to evaluate: the date cannot even be built
    assert decide(monthly(30), date(2026, 2, 28)).eligibility is EL.INELIGIBLE                 # not moved to month end
    assert decide(monthly(30), date(2026, 3, 1)).eligibility is EL.INELIGIBLE                  # not moved to the next month


# --- decision contract / forge resistance ------------------------------------------------------------------------

def test_decision_retains_the_recurrence_by_identity() -> None:
    recurrence = weekly(WD.MONDAY)
    decision = decide(recurrence, MONDAY)
    assert decision.recurrence is recurrence and decision.local_date == MONDAY and decision.eligibility is EL.ELIGIBLE


def test_decision_inputs_are_exact() -> None:
    class SubDate(date):
        pass

    recurrence = build()
    for bad in (datetime(2026, 10, 5), datetime(2026, 10, 5, tzinfo=timezone.utc), SubDate(2026, 10, 5), "2026-10-05", None, 20261005):
        with pytest.raises(TypeError):
            decide(recurrence, bad)
    for bad in (object(), None, recurrence.schedule_key, dataclasses.asdict(recurrence)):
        with pytest.raises(TypeError):
            decide(bad, MONDAY)
    with pytest.raises(TypeError):
        evaluate_private_scheduler_civil_date(recurrence, MONDAY)  # type: ignore[misc]


def test_direct_decision_construction_recomputes_and_rejects_forged_eligibility() -> None:
    daily, monday_only, fifteenth = build(), weekly(WD.MONDAY), monthly(15)
    assert PrivateSchedulerCivilDateDecision(recurrence=daily, local_date=MONDAY, eligibility=EL.ELIGIBLE).recurrence is daily
    for recurrence, stamp, forged in ((daily, MONDAY, EL.INELIGIBLE), (monday_only, WEEK[1], EL.ELIGIBLE), (monday_only, MONDAY, EL.INELIGIBLE),
                                      (fifteenth, date(2026, 10, 16), EL.ELIGIBLE), (fifteenth, date(2026, 10, 15), EL.INELIGIBLE)):
        with pytest.raises(ValueError):
            PrivateSchedulerCivilDateDecision(recurrence=recurrence, local_date=stamp, eligibility=forged)
    for kwargs_ in (dict(recurrence=object(), local_date=MONDAY, eligibility=EL.ELIGIBLE),
                    dict(recurrence=daily, local_date=datetime(2026, 10, 5), eligibility=EL.ELIGIBLE),
                    dict(recurrence=daily, local_date=MONDAY, eligibility="eligible"),
                    dict(recurrence=daily, local_date="2026-10-05", eligibility=EL.ELIGIBLE),
                    dict(recurrence=daily, local_date=MONDAY, eligibility=None)):
        with pytest.raises(TypeError):
            PrivateSchedulerCivilDateDecision(**kwargs_)


# --- materialization ---------------------------------------------------------------------------------------------

def test_eligible_decision_materializes_through_the_closed_24b1_authority() -> None:
    recurrence = build(schedule_key="private.schedule.a", schedule_revision=3, timezone_key="Europe/Istanbul", local_time=time(9, 55, 7),
                       ambiguous_time_policy=AP.LATER, work_kind=WK.PORTFOLIO_HEALTH_CHECK, scope=SC.PORTFOLIO, owner_id=OWNER,
                       portfolio_id=PORTFOLIO, policy_key="private.scheduler.v2", policy_revision=5, recurrence_kind=RK.WEEKLY,
                       weekdays=(WD.MONDAY,))
    occurrence = materialize_private_scheduler_civil_occurrence(decision=decide(recurrence, MONDAY))
    assert type(occurrence) is PrivateSchedulerScheduledOccurrence
    expected = build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.a", schedule_revision=3, timezone_key="Europe/Istanbul", local_date=MONDAY, local_time=time(9, 55, 7),
        ambiguous_time_policy=AP.LATER, work_kind=WK.PORTFOLIO_HEALTH_CHECK, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO,
        policy_key="private.scheduler.v2", policy_revision=5)
    assert occurrence == expected                                                              # identical to calling 24B1 directly
    assert (occurrence.local_date, occurrence.local_time, occurrence.timezone_key) == (MONDAY, time(9, 55, 7), "Europe/Istanbul")
    assert (occurrence.trigger.work_kind, occurrence.trigger.scope, occurrence.trigger.owner_id, occurrence.trigger.portfolio_id) == \
           (WK.PORTFOLIO_HEALTH_CHECK, SC.PORTFOLIO, OWNER, PORTFOLIO)
    assert (occurrence.trigger.policy_key, occurrence.trigger.policy_revision) == ("private.scheduler.v2", 5)


def test_ineligible_decision_raises_and_never_fabricates_an_occurrence() -> None:
    decision = decide(weekly(WD.MONDAY), WEEK[1])
    assert decision.eligibility is EL.INELIGIBLE
    with pytest.raises(ValueError):
        materialize_private_scheduler_civil_occurrence(decision=decision)
    short = decide(monthly(31), date(2026, 4, 30))
    with pytest.raises(ValueError):
        materialize_private_scheduler_civil_occurrence(decision=short)                          # no month-end shift, no alternate date
    for bad in (object(), None, build()):
        with pytest.raises(TypeError):
            materialize_private_scheduler_civil_occurrence(decision=bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        materialize_private_scheduler_civil_occurrence(decide(build(), MONDAY))  # type: ignore[misc]


def test_dst_nonexistent_time_is_civil_eligible_but_rejected_by_24b1() -> None:
    recurrence = build(timezone_key="America/New_York", local_time=time(2, 30))
    decision = decide(recurrence, date(2024, 3, 10))
    assert decision.eligibility is EL.ELIGIBLE                                                  # civil eligibility != valid timezone occurrence
    with pytest.raises(ValueError):
        materialize_private_scheduler_civil_occurrence(decision=decision)
    assert materialize_private_scheduler_civil_occurrence(decision=decide(recurrence, date(2024, 3, 11))).local_date == date(2024, 3, 11)
    assert decision.recurrence.local_time == time(2, 30) and decision.local_date == date(2024, 3, 10)      # nothing was altered to rescue it


def test_dst_ambiguous_time_follows_the_closed_24b1_policy() -> None:
    stamp = date(2024, 11, 3)
    rejecting = build(timezone_key="America/New_York", local_time=time(1, 30), ambiguous_time_policy=AP.REJECT)
    assert decide(rejecting, stamp).eligibility is EL.ELIGIBLE
    with pytest.raises(ValueError):
        materialize_private_scheduler_civil_occurrence(decision=decide(rejecting, stamp))
    instants = {}
    for policy in (AP.EARLIER, AP.LATER):
        occurrence = materialize_private_scheduler_civil_occurrence(
            decision=decide(build(timezone_key="America/New_York", local_time=time(1, 30), ambiguous_time_policy=policy), stamp))
        instants[policy] = occurrence.trigger.scheduled_for
        closed = build_private_scheduler_scheduled_occurrence(
            schedule_key="private.schedule.test", schedule_revision=1, timezone_key="America/New_York", local_date=stamp, local_time=time(1, 30),
            ambiguous_time_policy=policy, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
            policy_key="private.scheduler.v1", policy_revision=1)
        assert occurrence == closed
    assert instants[AP.EARLIER] < instants[AP.LATER]


def test_determinism() -> None:
    recurrence = weekly(WD.MONDAY, WD.FRIDAY)
    assert decide(recurrence, MONDAY) == decide(recurrence, MONDAY)
    assert materialize_private_scheduler_civil_occurrence(decision=decide(recurrence, MONDAY)) == \
           materialize_private_scheduler_civil_occurrence(decision=decide(recurrence, MONDAY))


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_recurrence.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_24a_24b1_surface_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "uuid", "zoneinfo", "backend.engine.private.scheduler_trigger",
                     "backend.engine.private.scheduler_scheduled_occurrence"}
    assert imported["backend.engine.private.scheduler_trigger"] <= {
        "PrivateSchedulerScope", "PrivateSchedulerTriggerKind", "PrivateSchedulerWorkKind", "build_private_scheduler_trigger"}
    assert imported["backend.engine.private.scheduler_scheduled_occurrence"] <= {
        "PrivateSchedulerAmbiguousTimePolicy", "PrivateSchedulerScheduledOccurrence", "build_private_scheduler_scheduled_occurrence"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)          # no private helper of a closed module
    for forbidden in ("infrastructure", "job_queue", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis", "socket", "urllib", "kap",
                      "mkk", "game_changer", "allocation", "rebalance", "portfolio", "analysis_pit", "calendar", "holidays", "dateutil", "pytz",
                      "croniter", "hashlib", "json"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_search_iteration_clock_calendar_dispatch_or_legacy_surface() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "sleep", "timedelta", "timestamp", "float", "random", "toordinal",
                        "fromordinal", "isocalendar", "calendar", "monthrange", "replace", "combine", "astimezone"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.GeneratorExp,
                                                             ast.ListComp, ast.SetComp, ast.DictComp, ast.Yield, ast.YieldFrom))]
    for loop in (n for n in ast.walk(_TREE) if isinstance(n, ast.For)):                        # only finite tuple validation, never a date range
        assert not any(isinstance(c, ast.Call) and getattr(c.func, "id", "") in ("range", "count", "cycle") for c in ast.walk(loop.iter))
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert not ints & {840, 14, 955, 1015, 1035, 1830, 2310, 10, 2000, 2001}                    # no polling interval, no clock slot, no probe date


_FRAGMENTS = ("scheduler_interval", "legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio",
              "fastapi", "supabase", "redis", "database", "repository", "cron", "rrule", "next_run", "previous_run", "next_eligible", "business",
              "holiday", "calendar", "market", "trading", "bist", "nyse", "nasdaq", "tefas", "dispatch", "execut", "pending", "running", "success",
              "failed", "retry", "backoff", "dlq", "queue", "worker", "game_changer", "allocation", "rebalance", "optimizer", "provider",
              "start_date", "end_date", "effective_from", "effective_until")
_EXACT = {"lease", "lock", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today"}


def test_no_market_calendar_search_runtime_or_legacy_identifiers() -> None:
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
    enum_names = {m.name for enum in (RK, WD, EL) for m in enum}
    assert not enum_names & {"UNKNOWN", "MARKET_CLOSED", "HOLIDAY", "BUSINESS_DAY", "CRON", "RRULE"}


def test_documents_the_recurrence_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("civil calendar", "not a market calendar", "weekend", "short month", "no business-day adjustment", "no next-run search",
                   "one explicit local date", "24B1", "24B3", "no ambient clock", "no dispatch", "probe", "DST"):
        assert needle in doc, needle
