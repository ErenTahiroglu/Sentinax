"""
backend/tests/test_scheduler_run_persistence_codec.py
=====================================================
Phase 24D2A: the canonical `admission_payload` codec for migration 024. A JSON-native, versioned, source-discriminated payload that is lossless
for the three admission paths, with hydration only through the CLOSED public builders, persisted derived-result assertions, exact nested key sets,
exact primitive types and strict date / time / UUID parsing. No database, no repository, no hashing, no clock.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_run_persistence_codec as codec
from backend.engine.private.scheduler_calendar_applicability import (
    PrivateSchedulerCalendarApplicabilityMode,
    build_private_scheduler_calendar_constraint,
    evaluate_private_scheduler_calendar_applicability,
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
from backend.engine.private.scheduler_event_occurrence import build_private_scheduler_event_occurrence
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerRecurrenceKind,
    PrivateSchedulerWeekday,
    build_private_scheduler_recurrence,
    evaluate_private_scheduler_civil_date,
)
from backend.engine.private.scheduler_run_admission import (
    PrivateSchedulerRunAdmission,
    PrivateSchedulerRunAdmissionSource,
    admit_private_scheduler_calendar_applicability,
    admit_private_scheduler_event_occurrence,
    admit_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_run_persistence_codec import (
    hydrate_private_scheduler_run_admission,
    serialize_private_scheduler_run_admission,
)
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerAmbiguousTimePolicy, build_private_scheduler_scheduled_occurrence
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
AP, SC, WK, EC = PrivateSchedulerAmbiguousTimePolicy, PrivateSchedulerScope, PrivateSchedulerWorkKind, PrivateSchedulerEventCauseKind
CK, CV, PR, MODE = (PrivateSchedulerCalendarKind, PrivateSchedulerCalendarCoverage, PrivateSchedulerReleaseTimePrecision,
                    PrivateSchedulerCalendarApplicabilityMode)
SRC = PrivateSchedulerRunAdmissionSource
DAY = date(2026, 10, 2)
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
PROTOCOL = "sentinax.private.scheduler.run-admission-persistence.v1"
SHA = "a" * 64


def U(h, mi=0, day=2) -> datetime:
    return datetime(2026, 10, day, h, mi, tzinfo=UTC)


# --- fixtures ----------------------------------------------------------------------------------------------------

def direct_admission():
    return admit_private_scheduler_scheduled_occurrence(occurrence=build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.direct", schedule_revision=7, timezone_key="America/New_York", local_date=date(2024, 6, 15),
        local_time=time(9, 30, 15, 123456), ambiguous_time_policy=AP.LATER, work_kind=WK.PORTFOLIO_HEALTH_CHECK, scope=SC.PORTFOLIO,
        owner_id=OWNER, portfolio_id=PORTFOLIO, policy_key="private.scheduler.v2", policy_revision=5))


def system_direct_admission():
    return admit_private_scheduler_scheduled_occurrence(occurrence=build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.system", schedule_revision=1, timezone_key="Europe/Istanbul", local_date=DAY, local_time=time(9, 55),
        ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
        policy_key="private.scheduler.v1", policy_revision=1))


def event_admission():
    return admit_private_scheduler_event_occurrence(occurrence=build_private_scheduler_event_occurrence(
        work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, event_cause_kind=EC.DISCLOSURE_INGESTED,
        cause_key="KAP-Disclosure-AbC-1", cause_available_at=datetime(2026, 10, 2, 10, 0, 0, 250000, tzinfo=PLUS3),
        policy_key="private.scheduler.v3", policy_revision=12))


def recurrence(**changes):
    base = dict(schedule_key="private.schedule.cal", schedule_revision=2, timezone_key="Europe/Istanbul", local_time=time(9, 55, 0, 500),
                ambiguous_time_policy=AP.EARLIER, recurrence_kind=PrivateSchedulerRecurrenceKind.WEEKLY,
                weekdays=(PrivateSchedulerWeekday.MONDAY, PrivateSchedulerWeekday.FRIDAY), day_of_month=None, work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH,
                scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, policy_key="private.scheduler.v1", policy_revision=3)
    base.update(changes)
    return build_private_scheduler_recurrence(**base)


def exchange_evidence(sessions, coverage=CV.COMPLETE_FOR_DATE, **changes):
    base = dict(calendar_key="exchange.test.core", calendar_revision=4, calendar_kind=CK.EXCHANGE_SESSION, timezone_key="Europe/Istanbul", local_date=DAY,
                coverage=coverage, exchange_sessions=tuple(sessions), source_releases=(), source_key="source.test",
                published_at=datetime(2026, 10, 2, 5, 30, tzinfo=PLUS3), observed_at=U(3), source_content_sha256=SHA)
    base.update(changes)
    return build_private_scheduler_calendar_date_evidence(**base)


def release_evidence(entries, coverage=CV.COMPLETE_FOR_DATE, **changes):
    base = dict(calendar_key="release.test.plan", calendar_revision=1, calendar_kind=CK.SOURCE_RELEASE_PLAN, timezone_key="Europe/Istanbul", local_date=DAY,
                coverage=coverage, exchange_sessions=(), source_releases=tuple(entries), source_key="source.test", published_at=None, observed_at=U(3),
                source_content_sha256="b" * 64)
    base.update(changes)
    return build_private_scheduler_calendar_date_evidence(**base)


def calendar_admission(evidence, constraint, cutoff=None, **recurrence_changes):
    decision = evaluate_private_scheduler_civil_date(recurrence=recurrence(**recurrence_changes), local_date=DAY)
    binding = bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=cutoff or datetime(2026, 10, 2, 7, 50, tzinfo=PLUS3))
    return admit_private_scheduler_calendar_applicability(applicability=evaluate_private_scheduler_calendar_applicability(
        decision=decision, calendar_binding=binding, constraint=constraint))


def exchange_constraint(key="exchange.test.core", revision=4):
    return build_private_scheduler_calendar_constraint(calendar_key=key, calendar_revision=revision, calendar_kind=CK.EXCHANGE_SESSION,
                                                       applicability_mode=MODE.EXCHANGE_SESSION_EXISTS, release_key=None)


def release_constraint(mode=MODE.SOURCE_RELEASE_EXISTS, release_key="CPI"):
    return build_private_scheduler_calendar_constraint(calendar_key="release.test.plan", calendar_revision=1, calendar_kind=CK.SOURCE_RELEASE_PLAN,
                                                       applicability_mode=mode, release_key=release_key)


def multi_session_calendar():
    sessions = (PrivateSchedulerExchangeSessionWindow(opens_at=U(7), closes_at=U(9)), PrivateSchedulerExchangeSessionWindow(opens_at=U(10), closes_at=U(14)))
    return calendar_admission(exchange_evidence(sessions), exchange_constraint())


def exact_release_calendar():
    return calendar_admission(release_evidence((PrivateSchedulerSourceReleasePlanEntry(release_key="CPI", time_precision=PR.EXACT_TIME, planned_for=U(7)),
                                                PrivateSchedulerSourceReleasePlanEntry(release_key="PPI", time_precision=PR.DATE_ONLY, planned_for=None))),
                              release_constraint())


def date_only_calendar():
    return calendar_admission(release_evidence((PrivateSchedulerSourceReleasePlanEntry(release_key="Zeta", time_precision=PR.DATE_ONLY, planned_for=None),
                                                PrivateSchedulerSourceReleasePlanEntry(release_key="Alpha", time_precision=PR.EXACT_TIME, planned_for=U(7)))),
                              release_constraint(release_key="Zeta"))


ALL = {"direct": direct_admission, "system_direct": system_direct_admission, "event": event_admission, "calendar_exchange": multi_session_calendar,
       "calendar_release": exact_release_calendar, "calendar_date_only": date_only_calendar}


def payload_of(name="calendar_exchange"):
    return serialize_private_scheduler_run_admission(admission=ALL[name]())


def wire(payload):
    return json.loads(json.dumps(payload))


def hydrate(payload):
    return hydrate_private_scheduler_run_admission(payload=payload)


def walk(node, path=()):
    """Yield (path, container) for every dict / list node and (path, leaf) for every scalar."""
    yield path, node
    if type(node) is dict:
        for key, value in node.items():
            yield from walk(value, path + (key,))
    elif type(node) is list:
        for index, value in enumerate(node):
            yield from walk(value, path + (index,))


def at_path(root, path):
    for step in path:
        root = root[step]
    return root


def set_path(root, path, value):
    parent = at_path(root, path[:-1])
    parent[path[-1]] = value


def assert_fails(payload):
    with pytest.raises((ValueError, TypeError)):
        hydrate(payload)


# --- signatures / protocol / top-level shape ---------------------------------------------------------------------

def test_functions_are_keyword_only_with_one_exact_input() -> None:
    for function, name in ((serialize_private_scheduler_run_admission, "admission"), (hydrate_private_scheduler_run_admission, "payload")):
        parameters = list(inspect.signature(function).parameters.values())
        assert [p.name for p in parameters] == [name]
        assert parameters[0].kind is inspect.Parameter.KEYWORD_ONLY and parameters[0].default is inspect.Parameter.empty
    admission = direct_admission()
    with pytest.raises(TypeError):
        serialize_private_scheduler_run_admission(admission)  # type: ignore[misc]
    for bad in (None, object(), admission.trigger, admission.scheduled_occurrence, "x"):
        with pytest.raises(TypeError):
            serialize_private_scheduler_run_admission(admission=bad)  # type: ignore[arg-type]

    class SubAdmission(PrivateSchedulerRunAdmission):
        pass

    sub = SubAdmission(**{f.name: getattr(admission, f.name) for f in dataclasses.fields(admission)})
    with pytest.raises(TypeError):
        serialize_private_scheduler_run_admission(admission=sub)
    with pytest.raises(TypeError):
        hydrate_private_scheduler_run_admission(payload=json.dumps(payload_of("direct")))  # type: ignore[arg-type]    # no json.loads convenience


def test_top_level_payload_has_exactly_four_keys() -> None:
    for name in ALL:
        payload = payload_of(name)
        assert set(payload) == {"protocol", "source", "run_idempotency_sha256", "provenance"}
        assert payload["protocol"] == PROTOCOL
        admission = ALL[name]()
        assert payload["source"] == admission.source.value and payload["run_idempotency_sha256"] == admission.run_idempotency_sha256
    assert not {"payload_sha256", "codec_hash", "row_id", "uuid", "serialized_at", "created_at"} & set(payload_of("direct"))
    assert not [key for _, node in walk(payload_of("calendar_exchange")) if type(node) is dict for key in node if "tzdb" in key or "tz_version" in key]


def test_unknown_protocol_and_source_are_rejected() -> None:
    for bad in ("sentinax.private.scheduler.run-admission-persistence.v2", "", None, 1, PROTOCOL.upper()):
        payload = payload_of("direct")
        payload["protocol"] = bad
        assert_fails(payload)
    for bad in ("raw_trigger", "SCHEDULED_DIRECT", "scheduled-direct", None, SRC.SCHEDULED_DIRECT, "legacy_job"):
        payload = payload_of("direct")
        payload["source"] = bad
        assert_fails(payload)


def test_top_level_input_type_is_an_exact_dict() -> None:
    class SubDict(dict):
        pass

    payload = payload_of("event")
    for bad in (SubDict(payload), [payload], None, "x", tuple(payload.items())):
        with pytest.raises(TypeError):
            hydrate_private_scheduler_run_admission(payload=bad)  # type: ignore[arg-type]


# --- JSON-native contract ----------------------------------------------------------------------------------------

def test_payload_is_json_native_only() -> None:
    allowed = (dict, list, str, int, bool, type(None))
    for name in ALL:
        for _, node in walk(payload_of(name)):
            assert type(node) in allowed, (name, type(node))
        assert not any(type(node) is float for _, node in walk(payload_of(name)))


def test_serialization_is_deterministic_and_adds_no_ambient_field() -> None:
    for name in ALL:
        assert payload_of(name) == payload_of(name)
        assert json.dumps(payload_of(name), sort_keys=True) == json.dumps(payload_of(name), sort_keys=True)


# --- round trips -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ALL))
def test_round_trip_through_json_hydrates_an_equal_admission(name: str) -> None:
    original = ALL[name]()
    payload = serialize_private_scheduler_run_admission(admission=original)
    for candidate in (payload, wire(payload)):
        hydrated = hydrate(candidate)
        assert hydrated == original
        assert hydrated.source is original.source and hydrated.run_idempotency_sha256 == original.run_idempotency_sha256
        assert hydrated.trigger == original.trigger
        assert hydrated is not original and hydrated.trigger is not original.trigger          # identities are reconstructed, never reused
    assert serialize_private_scheduler_run_admission(admission=hydrate(wire(payload))) == payload


def test_direct_round_trip_preserves_nontrivial_authority() -> None:
    original = direct_admission()
    provenance = payload_of("direct")["provenance"]["scheduled_occurrence"]
    assert provenance["timezone_key"] == "America/New_York" and provenance["local_time"] == "09:30:15.123456"
    assert provenance["ambiguous_time_policy"] == "later" and provenance["local_date"] == "2024-06-15"
    assert provenance["owner_id"] == str(OWNER) and provenance["portfolio_id"] == str(PORTFOLIO)
    assert provenance["expected_scheduled_for"] == original.trigger.scheduled_for.isoformat(timespec="microseconds")
    assert provenance["expected_idempotency_sha256"] == original.run_idempotency_sha256
    system = payload_of("system_direct")["provenance"]["scheduled_occurrence"]
    assert system["owner_id"] is None and system["portfolio_id"] is None
    assert set(payload_of("direct")["provenance"]) == {"scheduled_occurrence"}
    assert set(provenance) == {"schedule_key", "schedule_revision", "timezone_key", "local_date", "local_time", "ambiguous_time_policy", "work_kind",
                               "scope", "owner_id", "portfolio_id", "policy_key", "policy_revision", "expected_scheduled_for",
                               "expected_idempotency_sha256"}


def test_event_round_trip_preserves_offset_case_and_uuids() -> None:
    original = event_admission()
    provenance = payload_of("event")["provenance"]["event_occurrence"]
    assert set(payload_of("event")["provenance"]) == {"event_occurrence"}
    assert set(provenance) == {"work_kind", "scope", "owner_id", "portfolio_id", "event_cause_kind", "cause_key", "cause_available_at", "policy_key",
                               "policy_revision", "expected_idempotency_sha256"}
    assert provenance["cause_available_at"] == "2026-10-02T10:00:00.250000+03:00"              # the explicit offset is preserved, not normalized
    assert provenance["cause_key"] == "KAP-Disclosure-AbC-1" and provenance["policy_revision"] == 12
    hydrated = hydrate(wire(payload_of("event")))
    assert hydrated.trigger.cause_available_at == original.trigger.cause_available_at
    assert hydrated.trigger.cause_available_at.utcoffset() == timedelta(hours=3)
    assert hydrated.trigger.cause_key == "KAP-Disclosure-AbC-1"


SECOND_OFFSET = timezone(timedelta(seconds=30))
SUBSECOND_OFFSET = timezone(timedelta(seconds=30, microseconds=500000))


def event_with_offset(tz) -> PrivateSchedulerRunAdmission:
    return admit_private_scheduler_event_occurrence(occurrence=build_private_scheduler_event_occurrence(
        work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, event_cause_kind=EC.DISCLOSURE_INGESTED,
        cause_key="KAP-Offset-1", cause_available_at=datetime(2026, 10, 2, 10, 0, 0, 250000, tzinfo=tz), policy_key="private.scheduler.v3", policy_revision=2))


def test_closed_contract_accepts_non_minute_aligned_aware_offsets() -> None:
    stamp = datetime(2026, 10, 2, 10, 0, 0, 250000, tzinfo=SECOND_OFFSET)
    assert stamp.utcoffset() == timedelta(seconds=30)
    assert event_with_offset(SECOND_OFFSET).trigger.cause_available_at is not None          # the closed 24A / 24C1 authority has no minute-alignment rule


def test_event_with_a_second_level_offset_round_trips_losslessly() -> None:
    original = event_with_offset(SECOND_OFFSET)
    payload = serialize_private_scheduler_run_admission(admission=original)
    assert payload["provenance"]["event_occurrence"]["cause_available_at"] == "2026-10-02T10:00:00.250000+00:00:30"
    for candidate in (payload, wire(payload)):
        hydrated = hydrate(candidate)
        assert hydrated == original and hydrated.run_idempotency_sha256 == original.run_idempotency_sha256
        assert hydrated.trigger.cause_available_at.utcoffset() == timedelta(seconds=30)
        assert hydrated.trigger.idempotency_sha256 == original.trigger.idempotency_sha256


def test_event_with_a_fractional_second_offset_round_trips_losslessly() -> None:
    original = event_with_offset(SUBSECOND_OFFSET)
    payload = serialize_private_scheduler_run_admission(admission=original)
    text = payload["provenance"]["event_occurrence"]["cause_available_at"]
    assert text == "2026-10-02T10:00:00.250000+00:00:30.500000" == original.trigger.cause_available_at.isoformat(timespec="microseconds")
    hydrated = hydrate(wire(payload))
    assert hydrated == original and hydrated.trigger.cause_available_at.utcoffset() == timedelta(seconds=30, microseconds=500000)
    assert serialize_private_scheduler_run_admission(admission=hydrated) == payload                # the audit representation is never normalized


def test_calendar_provenance_with_non_minute_aligned_offsets_round_trips() -> None:
    evidence = exchange_evidence(
        (PrivateSchedulerExchangeSessionWindow(opens_at=U(7), closes_at=U(15)),),
        published_at=datetime(2026, 10, 2, 2, 0, 0, 100, tzinfo=SECOND_OFFSET),                      # 01:59:30.0001Z
        observed_at=datetime(2026, 10, 2, 2, 30, 0, tzinfo=SUBSECOND_OFFSET))                       # 02:29:29.5Z
    original = calendar_admission(evidence, exchange_constraint(), cutoff=datetime(2026, 10, 2, 4, 59, 30, tzinfo=SECOND_OFFSET))
    payload = serialize_private_scheduler_run_admission(admission=original)
    binding = payload["provenance"]["calendar_binding"]
    assert binding["knowledge_cutoff"].endswith("+00:00:30") and binding["evidence"]["published_at"].endswith("+00:00:30")
    assert binding["evidence"]["observed_at"].endswith("+00:00:30.500000")
    hydrated = hydrate(wire(payload))
    assert hydrated == original and hydrated.run_idempotency_sha256 == original.run_idempotency_sha256
    rebuilt = hydrated.calendar_applicability.calendar_binding
    assert rebuilt.knowledge_cutoff.utcoffset() == timedelta(seconds=30)
    assert rebuilt.evidence.published_at.utcoffset() == timedelta(seconds=30)
    assert rebuilt.evidence.observed_at.utcoffset() == timedelta(seconds=30, microseconds=500000)
    assert serialize_private_scheduler_run_admission(admission=hydrated) == payload


def test_canonical_utc_fields_stay_utc_even_when_audit_offsets_are_unusual() -> None:
    payload = serialize_private_scheduler_run_admission(admission=calendar_admission(
        exchange_evidence((PrivateSchedulerExchangeSessionWindow(opens_at=U(7), closes_at=U(15)),),
                          published_at=datetime(2026, 10, 2, 2, 30, tzinfo=SECOND_OFFSET)), exchange_constraint()))
    for item in payload["provenance"]["calendar_binding"]["evidence"]["exchange_sessions"]:
        assert item["opens_at"].endswith("+00:00") and item["closes_at"].endswith("+00:00")
    sessions = wire(payload)
    sessions["provenance"]["calendar_binding"]["evidence"]["exchange_sessions"][0]["opens_at"] = "2026-10-02T07:00:00.000000+00:00:30"
    assert_fails(sessions)                                                                          # the closed contract still demands canonical UTC


def test_canonical_roundtrip_is_the_lexical_authority_for_offsets() -> None:
    event_path = ("provenance", "event_occurrence", "cause_available_at")
    for bad in ("2026-10-02T10:00:00.250000+00:00:30 ", "2026-10-02T10:00:00.250000+0:00:30", "2026-10-02T10:00:00.250000+00:00:30.5",
                "2026-10-02T10:00:00.250000+00:00:60", "2026-10-02T10:00:00.250000-00:00", "2026-10-02T10:00:00.250000Z",
                "2026-10-02T10:00:00.250000+24:00", "2026-10-02T10:00:00.250000+0030", "2026-10-02T10:00:00.250000+03"):
        payload = payload_of("event")
        set_path(payload, event_path, bad)
        assert_fails(payload)


def test_calendar_payload_schema_and_nested_reconstruction() -> None:
    provenance = payload_of("calendar_exchange")["provenance"]
    assert set(provenance) == {"civil_decision", "calendar_binding", "calendar_constraint", "expected_applicability_status", "expected_applicability_reason",
                               "expected_candidate_idempotency_sha256"}
    assert "candidate_occurrence" not in provenance                                          # re-derived, never serialized independently
    decision = provenance["civil_decision"]
    assert set(decision) == {"recurrence", "local_date", "expected_eligibility"} and decision["expected_eligibility"] == "eligible"
    assert set(decision["recurrence"]) == {"schedule_key", "schedule_revision", "timezone_key", "local_time", "ambiguous_time_policy", "recurrence_kind",
                                           "weekdays", "day_of_month", "work_kind", "scope", "owner_id", "portfolio_id", "policy_key", "policy_revision"}
    assert decision["recurrence"]["weekdays"] == ["monday", "friday"] and decision["recurrence"]["day_of_month"] is None
    binding = provenance["calendar_binding"]
    assert set(binding) == {"evidence", "knowledge_cutoff"} and binding["knowledge_cutoff"] == "2026-10-02T07:50:00.000000+03:00"
    assert set(binding["evidence"]) == {"calendar_key", "calendar_revision", "calendar_kind", "timezone_key", "local_date", "coverage", "exchange_sessions",
                                        "source_releases", "source_key", "published_at", "observed_at", "source_content_sha256"}
    assert binding["evidence"]["source_releases"] == [] and binding["evidence"]["published_at"] == "2026-10-02T05:30:00.000000+03:00"
    assert set(provenance["calendar_constraint"]) == {"calendar_key", "calendar_revision", "calendar_kind", "applicability_mode", "release_key"}
    assert provenance["expected_applicability_status"] == "applicable" and provenance["expected_applicability_reason"] == "exchange_session_present"
    original = multi_session_calendar()
    assert provenance["expected_candidate_idempotency_sha256"] == original.calendar_applicability.candidate_occurrence.trigger.idempotency_sha256
    hydrated = hydrate(wire(payload_of("calendar_exchange")))
    assert hydrated.calendar_applicability == original.calendar_applicability
    assert hydrated.calendar_applicability.calendar_binding.knowledge_cutoff.utcoffset() == timedelta(hours=3)


def test_session_and_release_order_is_preserved_without_sorting() -> None:
    sessions = hydrate(wire(payload_of("calendar_exchange"))).calendar_applicability.calendar_binding.evidence.exchange_sessions
    assert [(s.opens_at, s.closes_at) for s in sessions] == [(U(7), U(9)), (U(10), U(14))]
    releases = hydrate(wire(payload_of("calendar_date_only"))).calendar_applicability.calendar_binding.evidence.source_releases
    assert [r.release_key for r in releases] == ["Zeta", "Alpha"]                              # supplied order, not alphabetical
    items = payload_of("calendar_date_only")["provenance"]["calendar_binding"]["evidence"]["source_releases"]
    assert [i["release_key"] for i in items] == ["Zeta", "Alpha"] and all(set(i) == {"release_key", "time_precision", "planned_for"} for i in items)
    session_items = payload_of("calendar_exchange")["provenance"]["calendar_binding"]["evidence"]["exchange_sessions"]
    assert all(set(i) == {"opens_at", "closes_at"} for i in session_items)


def test_date_only_release_keeps_a_null_planned_time() -> None:
    items = payload_of("calendar_date_only")["provenance"]["calendar_binding"]["evidence"]["source_releases"]
    assert items[0]["time_precision"] == "date_only" and items[0]["planned_for"] is None and items[1]["planned_for"] == "2026-10-02T07:00:00.000000+00:00"
    hydrated = hydrate(wire(payload_of("calendar_date_only")))
    first = hydrated.calendar_applicability.calendar_binding.evidence.source_releases[0]
    assert first.time_precision is PR.DATE_ONLY and first.planned_for is None
    assert hydrated.calendar_applicability.constraint.release_key == "Zeta"


def test_exact_time_calendar_path_round_trips() -> None:
    original = exact_release_calendar()
    hydrated = hydrate(wire(serialize_private_scheduler_run_admission(admission=original)))
    assert hydrated == original and hydrated.calendar_applicability.status.value == "applicable"


def test_complete_and_unavailable_coverage_stay_distinct_for_exchange_and_release_evidence() -> None:
    cases = [(exchange_evidence((), CV.COMPLETE_FOR_DATE), exchange_evidence((), CV.UNAVAILABLE)),
             (release_evidence((), CV.COMPLETE_FOR_DATE), release_evidence((), CV.UNAVAILABLE))]
    for complete, unavailable in cases:
        left, right = codec._serialize_evidence(complete), codec._serialize_evidence(unavailable)
        assert left["coverage"] == "complete_for_date" and right["coverage"] == "unavailable" and left != right
        assert left["exchange_sessions"] == [] == right["exchange_sessions"] and left["source_releases"] == [] == right["source_releases"]
        assert codec._hydrate_evidence(wire(left)) == complete and codec._hydrate_evidence(wire(right)) == unavailable
        assert codec._hydrate_evidence(wire(left)) != codec._hydrate_evidence(wire(right))


# --- derived-output assertions / run hash / tampering ------------------------------------------------------------

def test_run_hash_mismatch_is_rejected_for_every_source() -> None:
    for name in ALL:
        payload = payload_of(name)
        payload["run_idempotency_sha256"] = "f" * 64 if payload["run_idempotency_sha256"] != "f" * 64 else "e" * 64
        assert_fails(payload)
        payload["run_idempotency_sha256"] = payload["run_idempotency_sha256"].upper()
        assert_fails(payload)


def test_persisted_derived_assertions_are_verified() -> None:
    direct = payload_of("direct")
    direct["provenance"]["scheduled_occurrence"]["expected_idempotency_sha256"] = "f" * 64
    assert_fails(direct)
    direct = payload_of("direct")
    direct["provenance"]["scheduled_occurrence"]["expected_scheduled_for"] = "2024-06-15T13:30:15.123457+00:00"
    assert_fails(direct)
    event = payload_of("event")
    event["provenance"]["event_occurrence"]["expected_idempotency_sha256"] = "f" * 64
    assert_fails(event)
    for key, bad in (("expected_eligibility", "ineligible"), ("expected_eligibility", "eligible "), ):
        calendar = payload_of("calendar_exchange")
        calendar["provenance"]["civil_decision"][key] = bad
        assert_fails(calendar)
    for key, bad in (("expected_applicability_status", "not_applicable"), ("expected_applicability_status", "unavailable"),
                     ("expected_applicability_reason", "exchange_no_session"), ("expected_applicability_reason", "source_release_present"),
                     ("expected_candidate_idempotency_sha256", "f" * 64)):
        calendar = payload_of("calendar_exchange")
        calendar["provenance"][key] = bad
        assert_fails(calendar)


def test_identity_bearing_provenance_tampering_fails() -> None:
    def tamper(name, path, value):
        payload = payload_of(name)
        set_path(payload, path, value)
        assert_fails(payload)

    sched = ("provenance", "scheduled_occurrence")
    tamper("direct", sched + ("schedule_revision",), 8)
    tamper("direct", sched + ("timezone_key",), "Europe/Istanbul")
    tamper("direct", sched + ("local_date",), "2024-06-16")
    tamper("direct", sched + ("local_time",), "09:30:15.123457")
    tamper("direct", sched + ("work_kind",), "game_changer_review")
    tamper("direct", sched + ("owner_id",), str(UUID(int=9)))
    tamper("direct", sched + ("portfolio_id",), str(UUID(int=9)))
    tamper("direct", sched + ("policy_revision",), 6)
    tamper("direct", sched + ("ambiguous_time_policy",), "earlier")
    event = ("provenance", "event_occurrence")
    tamper("event", event + ("event_cause_kind",), "macro_release_ingested")
    tamper("event", event + ("cause_key",), "kap-disclosure-abc-1")
    tamper("event", event + ("cause_available_at",), "2026-10-02T10:00:00.250001+03:00")
    tamper("event", event + ("policy_revision",), 13)
    tamper("event", event + ("owner_id",), str(UUID(int=9)))
    cal = ("provenance",)
    tamper("calendar_exchange", cal + ("civil_decision", "recurrence", "schedule_revision"), 3)
    tamper("calendar_exchange", cal + ("civil_decision", "recurrence", "timezone_key"), "UTC")
    tamper("calendar_exchange", cal + ("civil_decision", "local_date"), "2026-10-03")
    tamper("calendar_exchange", cal + ("civil_decision", "recurrence", "local_time"), "10:55:00.000500")
    tamper("calendar_exchange", cal + ("civil_decision", "recurrence", "policy_revision"), 4)
    tamper("calendar_exchange", cal + ("calendar_binding", "evidence", "calendar_revision"), 5)
    tamper("calendar_exchange", cal + ("calendar_binding", "evidence", "coverage"), "unavailable")
    tamper("calendar_exchange", cal + ("calendar_binding", "knowledge_cutoff"), "2026-10-02T20:00:00.000000+00:00")     # after the occurrence: frontier
    tamper("calendar_release", cal + ("calendar_constraint", "release_key"), "cpi")
    tamper("calendar_release", cal + ("calendar_constraint", "release_key"), "MISSING")


def test_non_identity_evidence_fields_cannot_be_authenticated_by_the_codec() -> None:
    """Documented limitation: with no payload hash (by design) a change that keeps the derivation and run identity valid hydrates to a different but valid
    admission. Integrity of such audit fields rests on the immutable database column, not on this codec."""
    original = multi_session_calendar()
    payload = payload_of("calendar_exchange")
    payload["provenance"]["calendar_binding"]["evidence"]["source_content_sha256"] = "c" * 64
    changed = hydrate(payload)
    assert changed.run_idempotency_sha256 == original.run_idempotency_sha256 and changed != original
    assert changed.calendar_applicability.calendar_binding.evidence.source_content_sha256 == "c" * 64
    event = payload_of("event")
    event["provenance"]["event_occurrence"]["cause_available_at"] = "2026-10-02T07:00:00.250000+00:00"       # the same instant written at another offset
    shifted = hydrate(event)
    original_event = event_admission()
    assert shifted.run_idempotency_sha256 == original_event.run_idempotency_sha256                          # identity is instant-based
    assert shifted.trigger.cause_available_at == original_event.trigger.cause_available_at
    assert shifted.trigger.cause_available_at.utcoffset() == timedelta(0) != original_event.trigger.cause_available_at.utcoffset()


# --- exact key sets / types / parsing ----------------------------------------------------------------------------

def test_every_nested_object_requires_its_exact_key_set() -> None:
    checked = 0
    for name in ALL:
        base = payload_of(name)
        for path, node in walk(base):
            if type(node) is not dict:
                continue
            for key in list(node):
                missing = copy.deepcopy(base)
                del at_path(missing, path)[key]
                assert_fails(missing)
                checked += 1
            extra = copy.deepcopy(base)
            at_path(extra, path)["unexpected_key"] = 1
            assert_fails(extra)
            renamed = copy.deepcopy(base)
            target = at_path(renamed, path)
            first = next(iter(target))
            target["renamed_" + first] = target.pop(first)
            assert_fails(renamed)
    assert checked > 150


def test_explicit_null_is_not_missing_and_missing_is_not_null() -> None:
    payload = payload_of("direct")
    del payload["provenance"]["scheduled_occurrence"]["owner_id"]
    assert_fails(payload)
    system = payload_of("system_direct")
    assert system["provenance"]["scheduled_occurrence"]["owner_id"] is None and hydrate(wire(system)).scheduled_occurrence.trigger.owner_id is None
    wrong = payload_of("system_direct")
    wrong["provenance"]["scheduled_occurrence"]["owner_id"] = str(OWNER)
    assert_fails(wrong)


def test_every_scalar_requires_its_exact_python_type() -> None:
    class SubStr(str):
        pass

    class SubDict(dict):
        pass

    class SubList(list):
        pass

    for name in ALL:
        base = payload_of(name)
        for path, node in walk(base):
            if not path:
                continue
            kind = type(node)
            if kind is dict:
                for replacement in (SubDict(node), list(node.values()), None, "x"):
                    mutated = copy.deepcopy(base)
                    set_path(mutated, path, replacement)
                    assert_fails(mutated)
            elif kind is list:
                for replacement in (tuple(node), SubList(node), None, {}):
                    mutated = copy.deepcopy(base)
                    set_path(mutated, path, replacement)
                    assert_fails(mutated)
            else:
                replacements = [object(), 1.5, b"x", ["x"]]
                if kind is str:
                    replacements += [1, True, SubStr(node)]
                elif kind is int:
                    replacements += ["1", 1.0, str(node)]
                    if node in (0, 1):
                        replacements.append(bool(node))
                    replacements.append(True)
                else:
                    replacements += [1, "x", False]
                for replacement in replacements:
                    if replacement == node and type(replacement) is type(node):
                        continue
                    mutated = copy.deepcopy(base)
                    set_path(mutated, path, replacement)
                    assert_fails(mutated)


def test_enum_values_only_never_names_objects_or_normalized_forms() -> None:
    payload = payload_of("direct")
    for bad in ("LATER", "Later", " later", "later ", AP.LATER, None, 1):
        mutated = copy.deepcopy(payload)
        mutated["provenance"]["scheduled_occurrence"]["ambiguous_time_policy"] = bad
        assert_fails(mutated)
    calendar = payload_of("calendar_exchange")
    for path, bad in ((("provenance", "civil_decision", "recurrence", "weekdays"), ["MONDAY", "friday"]),
                      (("provenance", "civil_decision", "recurrence", "weekdays"), ["monday", PrivateSchedulerWeekday.FRIDAY]),
                      (("provenance", "civil_decision", "recurrence", "weekdays"), ["friday", "monday"]),
                      (("provenance", "calendar_binding", "evidence", "coverage"), "COMPLETE_FOR_DATE")):
        mutated = copy.deepcopy(calendar)
        set_path(mutated, path, bad)
        assert_fails(mutated)


def test_datetime_strings_are_strictly_parsed() -> None:
    event_path = ("provenance", "event_occurrence", "cause_available_at")
    for bad in ("2026-10-02T10:00:00.250000", "2026-10-02", "2026-10-02T10:00:00.250000+03:00 ", "2026-10-02T10:00:00.250000+03:00x", "2026-13-02T10:00:00.250000+03:00",
                "2026-10-32T10:00:00.250000+03:00", "2026-10-02T25:00:00.250000+03:00", "2026-10-02T10:00:00+03:00", "2026-10-02T10:00:00.25+03:00",
                "2026-10-02T10:00:00.250000Z", "2026-10-02 10:00:00.250000+03:00", "20261002T100000.250000+0300", "", 20261002, None):
        payload = payload_of("event")
        set_path(payload, event_path, bad)
        assert_fails(payload)
    for bad in ("2024-06-15T09:30", "2024-6-15", "2024/06/15", "20240615", "2024-06-15T00:00:00", " 2024-06-15", "2024-02-30"):
        payload = payload_of("direct")
        set_path(payload, ("provenance", "scheduled_occurrence", "local_date"), bad)
        assert_fails(payload)
    for bad in ("09:30:15", "09:30:15.123", "9:30:15.123456", "09:30:15.123456+00:00", "24:00:00.000000", "09:30:15.123456Z", "", "09:60:00.000000"):
        payload = payload_of("direct")
        set_path(payload, ("provenance", "scheduled_occurrence", "local_time"), bad)
        assert_fails(payload)


def test_uuid_strings_must_be_canonical() -> None:
    owner = str(OWNER)
    letters = "abcdefab-cdef-abcd-efab-cdefabcdefab"
    assert letters.upper() != letters and str(UUID(letters.upper())) == letters                 # Python parses it, but it is not the canonical lexical form
    for bad in (letters.upper(), "{" + owner + "}", owner.replace("-", ""), "urn:uuid:" + owner, " " + owner, owner + " ", OWNER, OWNER.int, "not-a-uuid"):
        payload = payload_of("direct")
        set_path(payload, ("provenance", "scheduled_occurrence", "owner_id"), bad)
        assert_fails(payload)
    upper = str(UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")).upper()
    assert str(UUID(upper)) != upper
    payload = payload_of("event")
    set_path(payload, ("provenance", "event_occurrence", "portfolio_id"), upper)
    assert_fails(payload)


def test_integer_domain_is_delegated_to_the_closed_builders() -> None:
    for path, bad in ((("provenance", "scheduled_occurrence", "schedule_revision"), 0), (("provenance", "scheduled_occurrence", "schedule_revision"), -1),
                      (("provenance", "scheduled_occurrence", "policy_revision"), 0)):
        payload = payload_of("direct")
        set_path(payload, path, bad)
        assert_fails(payload)
    for bad in (0, 32, -1):
        payload = payload_of("calendar_exchange")
        payload["provenance"]["civil_decision"]["recurrence"]["recurrence_kind"] = "monthly_day_of_month"
        payload["provenance"]["civil_decision"]["recurrence"]["weekdays"] = []
        payload["provenance"]["civil_decision"]["recurrence"]["day_of_month"] = bad
        assert_fails(payload)


# --- closed-builder hydration / structure ------------------------------------------------------------------------

def test_hydration_cannot_produce_a_raw_trigger_admission_for_a_wrong_source() -> None:
    payload = payload_of("direct")
    payload["source"] = "event_driven"
    assert_fails(payload)
    payload = payload_of("event")
    payload["source"] = "scheduled_direct"
    assert_fails(payload)
    payload = payload_of("calendar_exchange")
    payload["source"] = "scheduled_direct"
    assert_fails(payload)


def test_a_non_applicable_calendar_proof_cannot_be_hydrated_into_an_admission() -> None:
    payload = payload_of("calendar_exchange")
    payload["provenance"]["calendar_binding"]["evidence"]["exchange_sessions"] = []
    assert_fails(payload)                                                                        # derives NOT_APPLICABLE, which closed admission rejects


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(codec.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_run_persistence_codec.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_public_surfaces_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "datetime", "re", "uuid", "enum",
                     "backend.engine.private.scheduler_trigger", "backend.engine.private.scheduler_scheduled_occurrence",
                     "backend.engine.private.scheduler_recurrence", "backend.engine.private.scheduler_calendar_evidence",
                     "backend.engine.private.scheduler_calendar_applicability", "backend.engine.private.scheduler_event_occurrence",
                     "backend.engine.private.scheduler_run_admission"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)
    for forbidden in ("hashlib", "json", "supabase", "postgrest", "httpx", "requests", "asyncio", "fastapi", "redis", "infrastructure", "job_queue",
                      "random", "secrets", "pickle", "lifecycle", "game_changer", "allocation", "rebalance"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_hydration_uses_only_closed_public_builders_and_never_forges_raw_authority() -> None:
    calls = {getattr(n.func, "id", getattr(n.func, "attr", "")) for n in ast.walk(_TREE) if isinstance(n, ast.Call)}
    for required in ("build_private_scheduler_scheduled_occurrence", "admit_private_scheduler_scheduled_occurrence", "build_private_scheduler_event_occurrence",
                     "admit_private_scheduler_event_occurrence", "build_private_scheduler_recurrence", "evaluate_private_scheduler_civil_date",
                     "build_private_scheduler_calendar_date_evidence", "bind_private_scheduler_calendar_pit", "build_private_scheduler_calendar_constraint",
                     "evaluate_private_scheduler_calendar_applicability", "admit_private_scheduler_calendar_applicability"):
        assert required in calls, required
    forbidden_calls = {"PrivateSchedulerTrigger", "PrivateSchedulerRunAdmission", "PrivateSchedulerScheduledOccurrence", "PrivateSchedulerEventOccurrence",
                       "PrivateSchedulerCalendarApplicability", "PrivateSchedulerCivilDateDecision", "PrivateSchedulerRecurrence",
                       "PrivateSchedulerCalendarDateEvidence", "PrivateSchedulerCalendarPITBinding", "PrivateSchedulerCalendarConstraint",
                       "build_private_scheduler_trigger", "materialize_private_scheduler_civil_occurrence",
                       "require_private_scheduler_applicable_occurrence"}
    assert not calls & forbidden_calls
    allowed_leaf_constructors = {"PrivateSchedulerExchangeSessionWindow", "PrivateSchedulerSourceReleasePlanEntry"}
    assert allowed_leaf_constructors <= calls                                                    # leaf objects have no public builder


def test_no_clock_randomness_hashing_database_or_network_surface() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "sleep", "timestamp", "uuid4", "uuid1", "random", "urandom", "hashlib",
                        "sha256", "md5", "digest", "hexdigest", "dumps", "loads", "float", "pickle", "repr"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) in (float, complex)]
    for fragment in ("supabase", "postgrest", "select ", "insert ", "rpc(", "tzdata_version", "payload_sha256", "codec_hash"):
        assert fragment not in _SOURCE.lower().replace("insert into", "x"), fragment


def test_documents_the_codec_boundaries() -> None:
    doc = codec.__doc__ or ""
    for needle in ("sentinax.private.scheduler.run-admission-persistence.v1", "exact key", "closed public builders", "derived", "JSON-native", "no lifecycle",
                   "no database", "no hash", "offset", "tzdb", "24D2B"):
        assert needle in doc, needle
