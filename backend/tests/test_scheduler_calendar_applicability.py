"""
backend/tests/test_scheduler_calendar_applicability.py
======================================================
Phase 24B3B: calendar applicability. One ELIGIBLE 24B2 civil decision + one PIT-bound 24B3A calendar artifact + one explicit calendar constraint
-> APPLICABLE / NOT_APPLICABLE / UNAVAILABLE for the candidate occurrence materialized by the closed 24B1. Date-level exchange rule, exact-key
release rules, a planned-time not-before rule and the anti-lookahead frontier (calendar cutoff <= scheduled occurrence). No dispatch, no provider.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_calendar_applicability as module_under_test
from backend.engine.private.scheduler_calendar_applicability import (
    PrivateSchedulerCalendarApplicability,
    PrivateSchedulerCalendarApplicabilityMode,
    PrivateSchedulerCalendarApplicabilityReason,
    PrivateSchedulerCalendarApplicabilityStatus,
    PrivateSchedulerCalendarConstraint,
    build_private_scheduler_calendar_constraint,
    evaluate_private_scheduler_calendar_applicability,
    require_private_scheduler_applicable_occurrence,
)
from backend.engine.private.scheduler_calendar_evidence import (
    PrivateSchedulerCalendarCoverage,
    PrivateSchedulerCalendarKind,
    PrivateSchedulerExchangeSessionWindow,
    PrivateSchedulerReleaseTimePrecision,
    PrivateSchedulerSourceReleasePlanEntry,
    bind_private_scheduler_calendar_pit,
    build_private_scheduler_calendar_date_evidence,
)
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerRecurrenceKind,
    PrivateSchedulerWeekday,
    build_private_scheduler_recurrence,
    evaluate_private_scheduler_civil_date,
)
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerAmbiguousTimePolicy
from backend.engine.private.scheduler_trigger import PrivateSchedulerScope, PrivateSchedulerWorkKind
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
MODE, ST, RS = PrivateSchedulerCalendarApplicabilityMode, PrivateSchedulerCalendarApplicabilityStatus, PrivateSchedulerCalendarApplicabilityReason
CK, CV, PR = PrivateSchedulerCalendarKind, PrivateSchedulerCalendarCoverage, PrivateSchedulerReleaseTimePrecision
AP = PrivateSchedulerAmbiguousTimePolicy
DAY = date(2026, 10, 2)
SHA = "a" * 64


def U(h, mi=0, s=0, us=0, day=2) -> datetime:
    return datetime(2026, 10, day, h, mi, s, us, tzinfo=UTC)


def recurrence(**changes):
    base = dict(schedule_key="private.schedule.test", schedule_revision=1, timezone_key="Europe/Istanbul", local_time=time(9, 55),
                ambiguous_time_policy=AP.REJECT, recurrence_kind=PrivateSchedulerRecurrenceKind.DAILY, weekdays=(), day_of_month=None,
                work_kind=PrivateSchedulerWorkKind.SOURCE_DATA_REFRESH, scope=PrivateSchedulerScope.SYSTEM, owner_id=None, portfolio_id=None,
                policy_key="private.scheduler.v1", policy_revision=1)
    base.update(changes)
    return build_private_scheduler_recurrence(**base)


def decision(day=DAY, **changes):
    return evaluate_private_scheduler_civil_date(recurrence=recurrence(**changes), local_date=day)


def window(open_h, close_h, open_m=0, close_m=0, day=2):
    return PrivateSchedulerExchangeSessionWindow(opens_at=U(open_h, open_m, day=day), closes_at=U(close_h, close_m, day=day))


def exchange_evidence(sessions=(window(7, 15),), coverage=CV.COMPLETE_FOR_DATE, tz="Europe/Istanbul", day=DAY, key="exchange.test.core", revision=1,
                      observed=None):
    return build_private_scheduler_calendar_date_evidence(
        calendar_key=key, calendar_revision=revision, calendar_kind=CK.EXCHANGE_SESSION, timezone_key=tz, local_date=day, coverage=coverage,
        exchange_sessions=tuple(sessions), source_releases=(), source_key="source.test", published_at=None, observed_at=observed or U(3),
        source_content_sha256=SHA)


def release_evidence(entries=(), coverage=CV.COMPLETE_FOR_DATE, tz="Europe/Istanbul", day=DAY, key="release.test.plan", revision=1, observed=None):
    return build_private_scheduler_calendar_date_evidence(
        calendar_key=key, calendar_revision=revision, calendar_kind=CK.SOURCE_RELEASE_PLAN, timezone_key=tz, local_date=day, coverage=coverage,
        exchange_sessions=(), source_releases=tuple(entries), source_key="source.test", published_at=None, observed_at=observed or U(3),
        source_content_sha256=SHA)


def date_only(key):
    return PrivateSchedulerSourceReleasePlanEntry(release_key=key, time_precision=PR.DATE_ONLY, planned_for=None)


def exact(key, planned):
    return PrivateSchedulerSourceReleasePlanEntry(release_key=key, time_precision=PR.EXACT_TIME, planned_for=planned)


def binding(evidence, cutoff=None):
    return bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=cutoff or U(5))


def exchange_constraint(key="exchange.test.core", revision=1):
    return build_private_scheduler_calendar_constraint(calendar_key=key, calendar_revision=revision, calendar_kind=CK.EXCHANGE_SESSION,
                                                       applicability_mode=MODE.EXCHANGE_SESSION_EXISTS, release_key=None)


def release_constraint(mode=MODE.SOURCE_RELEASE_EXISTS, release_key="CPI", key="release.test.plan", revision=1):
    return build_private_scheduler_calendar_constraint(calendar_key=key, calendar_revision=revision, calendar_kind=CK.SOURCE_RELEASE_PLAN,
                                                       applicability_mode=mode, release_key=release_key)


def evaluate(dec, bound, constraint) -> PrivateSchedulerCalendarApplicability:
    return evaluate_private_scheduler_calendar_applicability(decision=dec, calendar_binding=bound, constraint=constraint)


def outcome(result):
    return (result.status, result.reason)


# --- enums / shapes ----------------------------------------------------------------------------------------------

def test_enums_and_stored_shapes() -> None:
    assert [(m.name, m.value) for m in MODE] == [("EXCHANGE_SESSION_EXISTS", "exchange_session_exists"), ("SOURCE_RELEASE_EXISTS", "source_release_exists"),
                                                 ("SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME", "source_release_not_before_planned_time")]
    assert [(m.name, m.value) for m in ST] == [("APPLICABLE", "applicable"), ("NOT_APPLICABLE", "not_applicable"), ("UNAVAILABLE", "unavailable")]
    assert [(m.name, m.value) for m in RS] == [
        ("EXCHANGE_SESSION_PRESENT", "exchange_session_present"), ("EXCHANGE_NO_SESSION", "exchange_no_session"),
        ("SOURCE_RELEASE_PRESENT", "source_release_present"), ("SOURCE_RELEASE_NOT_PRESENT", "source_release_not_present"),
        ("SOURCE_RELEASE_TIME_REACHED", "source_release_time_reached"), ("SOURCE_RELEASE_BEFORE_PLANNED_TIME", "source_release_before_planned_time"),
        ("SOURCE_RELEASE_DATE_ONLY", "source_release_date_only"), ("CALENDAR_COVERAGE_UNAVAILABLE", "calendar_coverage_unavailable")]
    constraint_fields = dataclasses.fields(PrivateSchedulerCalendarConstraint)
    result_fields = dataclasses.fields(PrivateSchedulerCalendarApplicability)
    assert [f.name for f in constraint_fields] == ["calendar_key", "calendar_revision", "calendar_kind", "applicability_mode", "release_key"]
    assert [f.name for f in result_fields] == ["decision", "calendar_binding", "constraint", "candidate_occurrence", "status", "reason"]
    for fields in (constraint_fields, result_fields):
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        exchange_constraint().calendar_key = "x"  # type: ignore[misc]
    for function, count in ((build_private_scheduler_calendar_constraint, 5), (evaluate_private_scheduler_calendar_applicability, 3),
                            (require_private_scheduler_applicable_occurrence, 1)):
        parameters = list(inspect.signature(function).parameters.values())
        assert len(parameters) == count and all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)


def test_constraint_contract() -> None:
    class SubStr(str):
        pass

    assert exchange_constraint().release_key is None
    assert release_constraint(release_key="CpI-1/x y").release_key == "CpI-1/x y"                  # case and inner spaces preserved
    for bad in ("", " x", "x ", "X", "a" * 129, "a b", "x\n"):
        with pytest.raises(ValueError):
            exchange_constraint(key=bad)
    for bad in (None, 1, SubStr("x")):
        with pytest.raises(TypeError):
            exchange_constraint(key=bad)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            exchange_constraint(revision=bad)
    for bad in (True, 1.0, "1", None):
        with pytest.raises(TypeError):
            exchange_constraint(revision=bad)
    for bad in ("", " x", "x ", "x" * 129, "a\nb", "a\tb"):
        with pytest.raises(ValueError):
            release_constraint(release_key=bad)
    for bad in (None, 1, b"x", SubStr("x")):
        with pytest.raises(TypeError):
            release_constraint(release_key=bad)
    with pytest.raises(ValueError):                                                              # exchange mode: no release key
        build_private_scheduler_calendar_constraint(calendar_key="x", calendar_revision=1, calendar_kind=CK.EXCHANGE_SESSION,
                                                    applicability_mode=MODE.EXCHANGE_SESSION_EXISTS, release_key="CPI")
    with pytest.raises(ValueError):                                                              # exchange mode needs the exchange kind
        build_private_scheduler_calendar_constraint(calendar_key="x", calendar_revision=1, calendar_kind=CK.SOURCE_RELEASE_PLAN,
                                                    applicability_mode=MODE.EXCHANGE_SESSION_EXISTS, release_key=None)
    for mode in (MODE.SOURCE_RELEASE_EXISTS, MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME):
        with pytest.raises(ValueError):                                                          # release modes need the release-plan kind
            build_private_scheduler_calendar_constraint(calendar_key="x", calendar_revision=1, calendar_kind=CK.EXCHANGE_SESSION,
                                                        applicability_mode=mode, release_key="CPI")
        with pytest.raises(TypeError):                                                           # and an exact release key
            build_private_scheduler_calendar_constraint(calendar_key="x", calendar_revision=1, calendar_kind=CK.SOURCE_RELEASE_PLAN,
                                                        applicability_mode=mode, release_key=None)
    for bad in ("exchange_session", None, 1, MODE.SOURCE_RELEASE_EXISTS):
        with pytest.raises(TypeError):
            build_private_scheduler_calendar_constraint(calendar_key="x", calendar_revision=1, calendar_kind=bad,
                                                        applicability_mode=MODE.EXCHANGE_SESSION_EXISTS, release_key=None)
    for bad in ("exchange_session_exists", None, 1, CK.EXCHANGE_SESSION):
        with pytest.raises(TypeError):
            build_private_scheduler_calendar_constraint(calendar_key="x", calendar_revision=1, calendar_kind=CK.EXCHANGE_SESSION,
                                                        applicability_mode=bad, release_key=None)


# --- preconditions -----------------------------------------------------------------------------------------------

def test_civil_ineligible_decision_is_rejected_not_turned_into_not_applicable() -> None:
    monday_only = recurrence(recurrence_kind=PrivateSchedulerRecurrenceKind.WEEKLY, weekdays=(PrivateSchedulerWeekday.MONDAY,))
    ineligible = evaluate_private_scheduler_civil_date(recurrence=monday_only, local_date=DAY)                # 2026-10-02 is a Friday
    with pytest.raises(ValueError):
        evaluate(ineligible, binding(exchange_evidence()), exchange_constraint())


def test_input_types_are_exact() -> None:
    dec, bound, constraint = decision(), binding(exchange_evidence()), exchange_constraint()
    for bad in (object(), None, dec.recurrence):
        with pytest.raises(TypeError):
            evaluate(bad, bound, constraint)
    for bad in (object(), None, bound.evidence):
        with pytest.raises(TypeError):
            evaluate(dec, bad, constraint)
    for bad in (object(), None):
        with pytest.raises(TypeError):
            evaluate(dec, bound, bad)
    with pytest.raises(TypeError):
        evaluate_private_scheduler_calendar_applicability(dec, bound, constraint)  # type: ignore[misc]


def test_inputs_are_retained_by_identity_and_the_candidate_comes_from_24b1() -> None:
    dec, bound, constraint = decision(), binding(exchange_evidence()), exchange_constraint()
    result = evaluate(dec, bound, constraint)
    assert result.decision is dec and result.calendar_binding is bound and result.constraint is constraint
    from backend.engine.private.scheduler_recurrence import materialize_private_scheduler_civil_occurrence
    assert result.candidate_occurrence == materialize_private_scheduler_civil_occurrence(decision=dec)
    assert result.candidate_occurrence.trigger.scheduled_for == U(6, 55)                       # 09:55 Istanbul (UTC+3)


def test_dst_failures_propagate_from_the_closed_24b1() -> None:
    nonexistent = decision(date(2024, 3, 10), timezone_key="America/New_York", local_time=time(2, 30))
    ambiguous = decision(date(2024, 11, 3), timezone_key="America/New_York", local_time=time(1, 30))
    for dec, stamp in ((nonexistent, date(2024, 3, 10)), (ambiguous, date(2024, 11, 3))):
        bound = binding(exchange_evidence(sessions=(), tz="America/New_York", day=stamp, observed=datetime(2023, 1, 1, tzinfo=UTC)),
                        cutoff=datetime(2024, 1, 1, tzinfo=UTC))
        with pytest.raises(ValueError):
            evaluate(dec, bound, exchange_constraint())


# --- identity / date / timezone matching -------------------------------------------------------------------------

def test_calendar_identity_must_match_exactly() -> None:
    dec = decision()
    for key, revision in (("exchange.other.core", 1), ("exchange.test.core", 2)):
        with pytest.raises(ValueError):
            evaluate(dec, binding(exchange_evidence()), exchange_constraint(key=key, revision=revision))
    with pytest.raises(ValueError):                                                              # kind mismatch (constraint expects releases)
        evaluate(dec, binding(exchange_evidence()), release_constraint(key="exchange.test.core"))
    with pytest.raises(ValueError):
        evaluate(dec, binding(release_evidence((date_only("CPI"),))), exchange_constraint(key="release.test.plan"))


def test_local_date_and_timezone_must_match_exactly_without_alias_equivalence() -> None:
    with pytest.raises(ValueError):
        evaluate(decision(), binding(exchange_evidence(sessions=(window(7, 15, day=3),), day=date(2026, 10, 3))), exchange_constraint())
    alias = exchange_evidence(tz="Etc/GMT-3")                                                    # same UTC+3 offset, different configuration identity
    assert alias.timezone_key != decision().recurrence.timezone_key
    with pytest.raises(ValueError):
        evaluate(decision(), binding(alias), exchange_constraint())


# --- anti-lookahead frontier -------------------------------------------------------------------------------------

def test_calendar_cutoff_must_not_exceed_the_scheduled_occurrence() -> None:
    dec = decision()
    occurrence = U(6, 55)
    evidence = exchange_evidence(observed=U(3))
    for cutoff in (occurrence, occurrence - timedelta(microseconds=1), U(3), occurrence - timedelta(hours=2)):
        result = evaluate(dec, binding(evidence, cutoff), exchange_constraint())
        assert result.status is ST.APPLICABLE
    with pytest.raises(ValueError):
        evaluate(dec, binding(evidence, occurrence + timedelta(microseconds=1)), exchange_constraint())
    with pytest.raises(ValueError):
        evaluate(dec, binding(evidence, U(17)), exchange_constraint())                          # a 17:00 frontier cannot adjudicate a 09:55 occurrence


def test_frontier_comparison_uses_utc_instants_across_offsets() -> None:
    plus9 = timezone(timedelta(hours=9))
    dec, evidence = decision(), exchange_evidence()
    exact_cutoff = datetime(2026, 10, 2, 15, 55, tzinfo=plus9)                                   # 06:55Z == occurrence
    assert evaluate(dec, binding(evidence, exact_cutoff), exchange_constraint()).status is ST.APPLICABLE
    with pytest.raises(ValueError):
        evaluate(dec, binding(evidence, exact_cutoff + timedelta(microseconds=1)), exchange_constraint())


def test_observed_before_cutoff_before_occurrence_chain() -> None:
    dec, evidence = decision(), exchange_evidence(observed=U(3))
    bound = binding(evidence, U(5))
    result = evaluate(dec, bound, exchange_constraint())
    assert result.calendar_binding.evidence.observed_at <= result.calendar_binding.knowledge_cutoff <= result.candidate_occurrence.trigger.scheduled_for


# --- coverage precedence -----------------------------------------------------------------------------------------

def test_unavailable_coverage_never_becomes_closed_or_no_release() -> None:
    dec = decision()
    unavailable_exchange = evaluate(dec, binding(exchange_evidence(sessions=(), coverage=CV.UNAVAILABLE)), exchange_constraint())
    assert outcome(unavailable_exchange) == (ST.UNAVAILABLE, RS.CALENDAR_COVERAGE_UNAVAILABLE)
    complete_closed = evaluate(dec, binding(exchange_evidence(sessions=(), coverage=CV.COMPLETE_FOR_DATE)), exchange_constraint())
    assert outcome(complete_closed) == (ST.NOT_APPLICABLE, RS.EXCHANGE_NO_SESSION)
    for mode in MODE:
        if mode is MODE.EXCHANGE_SESSION_EXISTS:
            continue
        unavailable = evaluate(dec, binding(release_evidence((), coverage=CV.UNAVAILABLE)), release_constraint(mode))
        assert outcome(unavailable) == (ST.UNAVAILABLE, RS.CALENDAR_COVERAGE_UNAVAILABLE)
        none_planned = evaluate(dec, binding(release_evidence((), coverage=CV.COMPLETE_FOR_DATE)), release_constraint(mode))
        assert outcome(none_planned) == (ST.NOT_APPLICABLE, RS.SOURCE_RELEASE_NOT_PRESENT)


# --- exchange: date-level ----------------------------------------------------------------------------------------

def test_exchange_session_exists_is_date_level_pre_open_and_post_close_are_applicable() -> None:
    session = (window(7, 15),)                                                                   # 10:00-18:00 Istanbul
    pre_open = evaluate(decision(local_time=time(9, 55)), binding(exchange_evidence(session), U(5)), exchange_constraint())
    post_close = evaluate(decision(local_time=time(18, 30)), binding(exchange_evidence(session), U(5)), exchange_constraint())
    inside = evaluate(decision(local_time=time(12, 0)), binding(exchange_evidence(session), U(5)), exchange_constraint())
    for result in (pre_open, post_close, inside):
        assert outcome(result) == (ST.APPLICABLE, RS.EXCHANGE_SESSION_PRESENT)
    assert pre_open.candidate_occurrence.trigger.scheduled_for < session[0].opens_at              # really outside the session
    assert post_close.candidate_occurrence.trigger.scheduled_for > session[0].closes_at


def test_half_day_session_is_still_a_session() -> None:
    shortened = evaluate(decision(), binding(exchange_evidence((window(7, 10),))), exchange_constraint())
    assert outcome(shortened) == (ST.APPLICABLE, RS.EXCHANGE_SESSION_PRESENT)
    two = evaluate(decision(), binding(exchange_evidence((window(7, 9), window(10, 12)))), exchange_constraint())
    assert outcome(two) == (ST.APPLICABLE, RS.EXCHANGE_SESSION_PRESENT)


# --- source releases ---------------------------------------------------------------------------------------------

def test_source_release_exists_is_date_presence_only() -> None:
    dec = decision()
    for entry in (date_only("CPI"), exact("CPI", U(7))):
        present = evaluate(dec, binding(release_evidence((entry,))), release_constraint(MODE.SOURCE_RELEASE_EXISTS))
        assert outcome(present) == (ST.APPLICABLE, RS.SOURCE_RELEASE_PRESENT)
    late_planned = evaluate(dec, binding(release_evidence((exact("CPI", U(20)),))), release_constraint(MODE.SOURCE_RELEASE_EXISTS))
    assert outcome(late_planned) == (ST.APPLICABLE, RS.SOURCE_RELEASE_PRESENT)                   # no timing statement at all
    absent = evaluate(dec, binding(release_evidence((date_only("PPI"),))), release_constraint(MODE.SOURCE_RELEASE_EXISTS))
    assert outcome(absent) == (ST.NOT_APPLICABLE, RS.SOURCE_RELEASE_NOT_PRESENT)


def test_release_key_matching_is_exact_and_case_sensitive() -> None:
    dec, bound = decision(), binding(release_evidence((date_only("CPI"),)))
    for key in ("cpi", "CPI ", "CP", "CPI2", "Cpi"):
        if key == "CPI ":
            with pytest.raises(ValueError):                                                      # untrimmed keys cannot even be declared
                release_constraint(release_key=key)
            continue
        assert outcome(evaluate(dec, bound, release_constraint(release_key=key))) == (ST.NOT_APPLICABLE, RS.SOURCE_RELEASE_NOT_PRESENT)
    assert outcome(evaluate(dec, bound, release_constraint(release_key="CPI"))) == (ST.APPLICABLE, RS.SOURCE_RELEASE_PRESENT)
    two = binding(release_evidence((date_only("A"), date_only("B"))))
    assert outcome(evaluate(dec, two, release_constraint(release_key="B"))) == (ST.APPLICABLE, RS.SOURCE_RELEASE_PRESENT)


def test_not_before_mode_with_a_date_only_entry_is_unavailable_never_fabricated() -> None:
    result = evaluate(decision(), binding(release_evidence((date_only("CPI"),))), release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME))
    assert outcome(result) == (ST.UNAVAILABLE, RS.SOURCE_RELEASE_DATE_ONLY)
    absent = evaluate(decision(), binding(release_evidence((date_only("PPI"),))), release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME))
    assert outcome(absent) == (ST.NOT_APPLICABLE, RS.SOURCE_RELEASE_NOT_PRESENT)


def test_not_before_mode_exact_time_has_no_tolerance() -> None:
    planned = U(10)
    cases = ((time(9, 59, 59, 999999), ST.NOT_APPLICABLE, RS.SOURCE_RELEASE_BEFORE_PLANNED_TIME), (time(10, 0), ST.APPLICABLE, RS.SOURCE_RELEASE_TIME_REACHED),
             (time(10, 0, 0, 1), ST.APPLICABLE, RS.SOURCE_RELEASE_TIME_REACHED), (time(11, 30), ST.APPLICABLE, RS.SOURCE_RELEASE_TIME_REACHED),
             (time(8, 0), ST.NOT_APPLICABLE, RS.SOURCE_RELEASE_BEFORE_PLANNED_TIME))
    for wall, status, reason in cases:
        dec = decision(timezone_key="UTC", local_time=wall)
        bound = binding(release_evidence((exact("CPI", planned),), tz="UTC"), cutoff=U(7))
        result = evaluate(dec, bound, release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME))
        assert outcome(result) == (status, reason), wall
    plus_five = evaluate(decision(timezone_key="UTC", local_time=time(10, 5)), binding(release_evidence((exact("CPI", planned),), tz="UTC"), U(7)),
                         release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME))
    assert plus_five.status is ST.APPLICABLE                                                      # a +5 minute wall time is the recurrence's choice, no offset here


# --- helper ------------------------------------------------------------------------------------------------------

def test_require_applicable_occurrence_helper() -> None:
    dec = decision()
    applicable = evaluate(dec, binding(exchange_evidence()), exchange_constraint())
    assert require_private_scheduler_applicable_occurrence(applicability=applicable) is applicable.candidate_occurrence
    for result in (evaluate(dec, binding(exchange_evidence(sessions=())), exchange_constraint()),
                   evaluate(dec, binding(exchange_evidence(sessions=(), coverage=CV.UNAVAILABLE)), exchange_constraint())):
        with pytest.raises(ValueError):
            require_private_scheduler_applicable_occurrence(applicability=result)
    for bad in (object(), None, dec):
        with pytest.raises(TypeError):
            require_private_scheduler_applicable_occurrence(applicability=bad)  # type: ignore[arg-type]


# --- forge resistance --------------------------------------------------------------------------------------------

def _forge(good: PrivateSchedulerCalendarApplicability, **changes) -> PrivateSchedulerCalendarApplicability:
    values = {f.name: getattr(good, f.name) for f in dataclasses.fields(good)}
    values.update(changes)
    return PrivateSchedulerCalendarApplicability(**values)


def test_direct_construction_recomputes_and_rejects_forgeries() -> None:
    dec, bound = decision(), binding(exchange_evidence())
    good = evaluate(dec, bound, exchange_constraint())
    assert _forge(good) == good
    closed = evaluate(dec, binding(exchange_evidence(sessions=())), exchange_constraint())
    unavailable = evaluate(dec, binding(exchange_evidence(sessions=(), coverage=CV.UNAVAILABLE)), exchange_constraint())
    other_time = evaluate(decision(local_time=time(10, 5)), bound, exchange_constraint()).candidate_occurrence
    other_date = evaluate(decision(date(2026, 10, 3)), binding(exchange_evidence((window(7, 15, day=3),), day=date(2026, 10, 3))),
                          exchange_constraint()).candidate_occurrence
    other_recurrence = evaluate(decision(schedule_key="private.schedule.other"), bound, exchange_constraint()).candidate_occurrence
    forged = [
        dict(status=ST.NOT_APPLICABLE), dict(status=ST.UNAVAILABLE), dict(reason=RS.EXCHANGE_NO_SESSION), dict(reason=RS.SOURCE_RELEASE_PRESENT),
        dict(status=ST.APPLICABLE, reason=RS.CALENDAR_COVERAGE_UNAVAILABLE), dict(candidate_occurrence=other_time),
        dict(candidate_occurrence=other_date), dict(candidate_occurrence=other_recurrence),
        dict(constraint=exchange_constraint(key="exchange.other.core")), dict(constraint=exchange_constraint(revision=2)),
        dict(calendar_binding=binding(exchange_evidence(), U(17))),
    ]
    for changes in forged:
        with pytest.raises(ValueError):
            _forge(good, **changes)
    with pytest.raises(ValueError):
        _forge(closed, status=ST.APPLICABLE, reason=RS.EXCHANGE_SESSION_PRESENT)
    with pytest.raises(ValueError):
        _forge(unavailable, status=ST.NOT_APPLICABLE, reason=RS.EXCHANGE_NO_SESSION)
    for field, bad in (("decision", object()), ("calendar_binding", object()), ("constraint", object()), ("candidate_occurrence", object()),
                       ("status", "applicable"), ("reason", "exchange_session_present"), ("status", None)):
        with pytest.raises(TypeError):
            _forge(good, **{field: bad})


def test_result_status_reason_pairs_are_exactly_the_specified_matrix() -> None:
    allowed = {ST.APPLICABLE: {RS.EXCHANGE_SESSION_PRESENT, RS.SOURCE_RELEASE_PRESENT, RS.SOURCE_RELEASE_TIME_REACHED},
               ST.NOT_APPLICABLE: {RS.EXCHANGE_NO_SESSION, RS.SOURCE_RELEASE_NOT_PRESENT, RS.SOURCE_RELEASE_BEFORE_PLANNED_TIME},
               ST.UNAVAILABLE: {RS.CALENDAR_COVERAGE_UNAVAILABLE, RS.SOURCE_RELEASE_DATE_ONLY}}
    seen = set()
    dec = decision()
    results = [evaluate(dec, binding(exchange_evidence()), exchange_constraint()), evaluate(dec, binding(exchange_evidence(sessions=())), exchange_constraint()),
               evaluate(dec, binding(exchange_evidence(sessions=(), coverage=CV.UNAVAILABLE)), exchange_constraint()),
               evaluate(dec, binding(release_evidence((date_only("CPI"),))), release_constraint()),
               evaluate(dec, binding(release_evidence(())), release_constraint()),
               evaluate(dec, binding(release_evidence((date_only("CPI"),))), release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME)),
               evaluate(decision(timezone_key="UTC", local_time=time(9, 0)), binding(release_evidence((exact("CPI", U(10)),), tz="UTC"), U(7)),
                        release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME)),
               evaluate(decision(timezone_key="UTC", local_time=time(11, 0)), binding(release_evidence((exact("CPI", U(10)),), tz="UTC"), U(7)),
                        release_constraint(MODE.SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME))]
    for result in results:
        assert result.reason in allowed[result.status]
        seen.add(result.reason)
    assert seen == set(RS)                                                                       # every one of the eight reasons is reachable


def test_deterministic_evaluation() -> None:
    dec, bound, constraint = decision(), binding(exchange_evidence()), exchange_constraint()
    assert evaluate(dec, bound, constraint) == evaluate(dec, bound, constraint)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_calendar_applicability.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_24b_public_surface_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "backend.engine.private.scheduler_recurrence",
                     "backend.engine.private.scheduler_scheduled_occurrence", "backend.engine.private.scheduler_calendar_evidence"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)            # no private helper of a closed module
    assert imported["backend.engine.private.scheduler_recurrence"] <= {
        "PrivateSchedulerCivilDateDecision", "PrivateSchedulerCivilDateEligibility", "materialize_private_scheduler_civil_occurrence"}
    assert imported["backend.engine.private.scheduler_scheduled_occurrence"] <= {"PrivateSchedulerScheduledOccurrence"}
    assert imported["backend.engine.private.scheduler_calendar_evidence"] <= {
        "PrivateSchedulerCalendarCoverage", "PrivateSchedulerCalendarKind", "PrivateSchedulerCalendarPITBinding", "PrivateSchedulerReleaseTimePrecision"}
    for forbidden in ("infrastructure", "job_queue", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis", "socket", "urllib", "kap", "mkk",
                      "game_changer", "allocation", "rebalance", "portfolio", "analysis_pit", "zoneinfo", "hashlib", "json", "holidays", "dateutil"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_clock_containment_inference_or_arithmetic() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "sleep", "timedelta", "timestamp", "weekday", "isoweekday", "float",
                        "random", "opens_at", "closes_at", "exchange_sessions_overlap", "combine"}                  # no session-containment check
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert ints <= {1, 128}                                                                      # no +1 / +5 minute offset, no clock slot


_FRAGMENTS = ("scheduler_interval", "legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio", "fastapi",
              "supabase", "redis", "database", "repository", "provider", "scrap", "browser", "weekend", "weekday", "holiday", "dispatch", "execut",
              "run_job", "pending", "running", "success", "succe", "failed", "retry", "backoff", "dlq", "queue", "worker", "available_at",
              "actual_release", "data_available", "fetch_", "ingest", "bist", "nyse", "tuik", "tcmb", "game_changer", "allocation", "rebalance")
_EXACT = {"lease", "lock", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today", "run"}


def test_no_dispatch_provider_availability_or_calendar_inference_identifiers() -> None:
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
    names = {f.name for cls in (PrivateSchedulerCalendarConstraint, PrivateSchedulerCalendarApplicability) for f in dataclasses.fields(cls)}
    assert not names & {"available_at", "actual_release_at", "data_available", "fetch_succeeded", "ingestion_succeeded"}


def test_documents_the_applicability_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("civil eligibility", "external evidence", "anti-lookahead", "UNAVAILABLE", "date-level", "half-day", "exact key", "not-before",
                   "DATE_ONLY", "planned release", "not actual", "no dispatch", "24C"):
        assert needle in doc, needle
