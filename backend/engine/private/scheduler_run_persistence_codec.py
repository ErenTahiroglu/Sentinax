"""
backend/engine/private/scheduler_run_persistence_codec.py
=========================================================
Canonical `admission_payload` codec for scheduler run persistence (Phase 24D2A).

Migration 024 stores `admission_payload JSONB` as the immutable audit provenance of a closed Phase 24C2A `PrivateSchedulerRunAdmission` and is
deliberately schema-opaque at SQL level. This module defines exactly what that JSON object means: deterministic, versioned, source-discriminated and
complete enough to reconstruct the closed admission. It is a pure module: no database, no repository, no network, no clock, no randomness and no hash
(the logical run identity stays the closed `admission.run_idempotency_sha256`; the codec stores and verifies it and never hashes the payload or adds a
second identity).

Top level (exactly four keys, no more and no fewer): `protocol` (the fixed marker `sentinax.private.scheduler.run-admission-persistence.v1`; any other
value is rejected, there is no best-effort compatibility), `source` (the admission source value), `run_idempotency_sha256` and `provenance` (the exact
source-specific object):

    scheduled_direct               {scheduled_occurrence}: the 24B1 configuration and authority fields plus the audit assertions
                                   expected_scheduled_for and expected_idempotency_sha256
    event_driven                   {event_occurrence}: the 24C1 authority fields plus expected_idempotency_sha256
    scheduled_calendar_applicable  {civil_decision, calendar_binding, calendar_constraint, expected_applicability_status,
                                   expected_applicability_reason, expected_candidate_idempotency_sha256}: the candidate occurrence is never serialized
                                   independently, it is re-derived

JSON-native values only (dict, list, str, int, bool, None): enums by exact `.value`, UUIDs as canonical lowercase strings, dates as YYYY-MM-DD, local times
as HH:MM:SS.ffffff, and every datetime as an ISO-8601 string with microsecond precision that keeps its explicit UTC offset, including second-level and sub-second offsets such as
+00:00:30 or +00:00:30.500000 (audit instants are never normalized to UTC; canonical-UTC fields stay +00:00; the lexical authority is Python's own
canonical isoformat round trip). Tuples become lists in the supplied order (exchange sessions and source releases are never sorted
or deduplicated) and explicit empty lists and nulls are always written, so COMPLETE_FOR_DATE with no entries and UNAVAILABLE with no entries remain
distinct and a DATE_ONLY release keeps a null planned time.

Hydration rebuilds only through the closed public builders, evaluators and admission builders (never a forged raw trigger, occurrence or admission):
the 24B1 / 24B2 / 24B3A / 24B3B / 24C1 builders re-validate every field. The persisted `expected_*` values are derived-result assertions, not builder
inputs: historical adjudication must not silently change under future code drift, so hydration fails closed if the current closed logic no longer
derives the persisted result, and the reconstructed run hash must equal the stored top-level hash (the stored hash is never trusted over the
reconstructed admission). Every nested object requires its exact key set (a missing, extra or renamed key is rejected; explicit null is not missing, there
are no permissive defaults), exact primitive types (a bool is not an int, a tuple is not a list, no subclass), strict canonical date / time / datetime /
UUID parsing with round-trip equality, and integer domain ranges are left to the closed builders.

Limits: with no payload hash the codec cannot authenticate audit fields that influence neither the derivation nor the run identity (for example the
calendar source hash); their integrity rests on the immutable database column. The payload carries no tzdb version, so tzdb-version provenance for replay
remains deferred. This codec serializes no lifecycle (no lifecycle or transition objects: 24D2B hydrates the current row from this payload plus the
authoritative lifecycle columns). Hydration returns newly constructed objects, not the original identities.

Architectural Invariants:
    - Pure domain module: standard library plus the closed Phase 24 public surfaces; no hashing, JSON transport, database, network, clock or randomness.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from uuid import UUID

from backend.engine.private.scheduler_calendar_applicability import (
    PrivateSchedulerCalendarApplicabilityMode,
    PrivateSchedulerCalendarApplicabilityStatus,
    build_private_scheduler_calendar_constraint,
    evaluate_private_scheduler_calendar_applicability,
)
from backend.engine.private.scheduler_calendar_evidence import (
    PrivateSchedulerCalendarCoverage,
    PrivateSchedulerCalendarDateEvidence,
    PrivateSchedulerCalendarKind,
    PrivateSchedulerExchangeSessionWindow,
    PrivateSchedulerReleaseTimePrecision,
    PrivateSchedulerSourceReleasePlanEntry,
    bind_private_scheduler_calendar_pit,
    build_private_scheduler_calendar_date_evidence,
)
from backend.engine.private.scheduler_event_occurrence import build_private_scheduler_event_occurrence
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerRecurrence,
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
from backend.engine.private.scheduler_scheduled_occurrence import (
    PrivateSchedulerAmbiguousTimePolicy,
    build_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerWorkKind,
)

_PROTOCOL = "sentinax.private.scheduler.run-admission-persistence.v1"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}")

_TOP_KEYS = ("protocol", "source", "run_idempotency_sha256", "provenance")
_SCHEDULED_KEYS = ("schedule_key", "schedule_revision", "timezone_key", "local_date", "local_time", "ambiguous_time_policy", "work_kind", "scope",
                   "owner_id", "portfolio_id", "policy_key", "policy_revision", "expected_scheduled_for", "expected_idempotency_sha256")
_EVENT_KEYS = ("work_kind", "scope", "owner_id", "portfolio_id", "event_cause_kind", "cause_key", "cause_available_at", "policy_key", "policy_revision",
               "expected_idempotency_sha256")
_CALENDAR_KEYS = ("civil_decision", "calendar_binding", "calendar_constraint", "expected_applicability_status", "expected_applicability_reason",
                  "expected_candidate_idempotency_sha256")
_DECISION_KEYS = ("recurrence", "local_date", "expected_eligibility")
_RECURRENCE_KEYS = ("schedule_key", "schedule_revision", "timezone_key", "local_time", "ambiguous_time_policy", "recurrence_kind", "weekdays",
                    "day_of_month", "work_kind", "scope", "owner_id", "portfolio_id", "policy_key", "policy_revision")
_BINDING_KEYS = ("evidence", "knowledge_cutoff")
_EVIDENCE_KEYS = ("calendar_key", "calendar_revision", "calendar_kind", "timezone_key", "local_date", "coverage", "exchange_sessions", "source_releases",
                  "source_key", "published_at", "observed_at", "source_content_sha256")
_SESSION_KEYS = ("opens_at", "closes_at")
_RELEASE_KEYS = ("release_key", "time_precision", "planned_for")
_CONSTRAINT_KEYS = ("calendar_key", "calendar_revision", "calendar_kind", "applicability_mode", "release_key")

_ERR_ADMISSION_TYPE = "admission must be an exact PrivateSchedulerRunAdmission instance"
_ERR_PAYLOAD_TYPE = "payload must be an exact dict instance"
_ERR_OBJECT = "payload object must be an exact dict with exactly the expected keys"
_ERR_TEXT = "payload value must be an exact str"
_ERR_INTEGER = "payload value must be an exact int"
_ERR_ARRAY = "payload value must be an exact list"
_ERR_ENUM = "payload value is not an exact known enum value"
_ERR_UUID = "payload value must be null or a canonical lowercase UUID string"
_ERR_DATE = "payload date must be an exact canonical YYYY-MM-DD string"
_ERR_TIME = "payload local time must be an exact canonical HH:MM:SS.ffffff string"
_ERR_INSTANT = "payload instant must be an aware datetime in the exact canonical ISO-8601 microsecond form"
_ERR_PROTOCOL = "unknown admission persistence protocol"
_ERR_ASSERTION = "a persisted derived assertion no longer matches the closed derivation"
_ERR_RUN_HASH = "the stored run hash does not match the reconstructed admission"
_ERR_NOT_APPLICABLE = "a persisted calendar proof must still derive APPLICABLE"


def _object(value: object, keys: tuple[str, ...]) -> dict:
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(_ERR_OBJECT)
    for key in value:
        if type(key) is not str:
            raise ValueError(_ERR_OBJECT)
    return value


def _text(value: object) -> str:
    if type(value) is not str:
        raise ValueError(_ERR_TEXT)
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError(_ERR_INTEGER)
    return value


def _array(value: object) -> list:
    if type(value) is not list:
        raise ValueError(_ERR_ARRAY)
    return value


def _enum(cls: type, value: object):
    if type(value) is not str:
        raise ValueError(_ERR_ENUM)
    for member in cls:
        if member.value == value:
            return member
    raise ValueError(_ERR_ENUM)


def _nullable_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _uuid(value: object) -> UUID | None:
    if value is None:
        return None
    text = _text(value)
    try:
        parsed = UUID(text)
    except ValueError:
        raise ValueError(_ERR_UUID) from None
    if str(parsed) != text:
        raise ValueError(_ERR_UUID)
    return parsed


def _date(value: object) -> date:
    text = _text(value)
    if _DATE.fullmatch(text) is None:
        raise ValueError(_ERR_DATE)
    parsed = date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError(_ERR_DATE)
    return parsed


def _time(value: object) -> time:
    text = _text(value)
    if _TIME.fullmatch(text) is None:
        raise ValueError(_ERR_TIME)
    parsed = time.fromisoformat(text)
    if parsed.tzinfo is not None or parsed.isoformat(timespec="microseconds") != text:
        raise ValueError(_ERR_TIME)
    return parsed


def _instant(value: object) -> datetime:
    """Strict persisted-datetime parser: any aware datetime the closed contracts accept (including second-level and sub-second UTC offsets), with
    Python's canonical ISO round trip as the single lexical authority (so `Z`, a missing fraction, spaces and compact forms are all rejected)."""
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(_ERR_INSTANT) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(_ERR_INSTANT)
    try:
        parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError(_ERR_INSTANT) from None
    if parsed.isoformat(timespec="microseconds") != text:
        raise ValueError(_ERR_INSTANT)
    return parsed


def _nullable_instant(value: object) -> datetime | None:
    return None if value is None else _instant(value)


def _uuid_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _instant_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _expect(actual: object, expected: object) -> None:
    if actual != expected:
        raise ValueError(_ERR_ASSERTION)


# --- recurrence --------------------------------------------------------------------------------------------------

def _serialize_recurrence(recurrence: PrivateSchedulerRecurrence) -> dict[str, object]:
    return {
        "schedule_key": recurrence.schedule_key,
        "schedule_revision": recurrence.schedule_revision,
        "timezone_key": recurrence.timezone_key,
        "local_time": recurrence.local_time.isoformat(timespec="microseconds"),
        "ambiguous_time_policy": recurrence.ambiguous_time_policy.value,
        "recurrence_kind": recurrence.recurrence_kind.value,
        "weekdays": [weekday.value for weekday in recurrence.weekdays],
        "day_of_month": recurrence.day_of_month,
        "work_kind": recurrence.work_kind.value,
        "scope": recurrence.scope.value,
        "owner_id": _uuid_text(recurrence.owner_id),
        "portfolio_id": _uuid_text(recurrence.portfolio_id),
        "policy_key": recurrence.policy_key,
        "policy_revision": recurrence.policy_revision,
    }


def _hydrate_recurrence(raw: object) -> PrivateSchedulerRecurrence:
    obj = _object(raw, _RECURRENCE_KEYS)
    day_of_month = obj["day_of_month"]
    return build_private_scheduler_recurrence(
        schedule_key=_text(obj["schedule_key"]),
        schedule_revision=_integer(obj["schedule_revision"]),
        timezone_key=_text(obj["timezone_key"]),
        local_time=_time(obj["local_time"]),
        ambiguous_time_policy=_enum(PrivateSchedulerAmbiguousTimePolicy, obj["ambiguous_time_policy"]),
        recurrence_kind=_enum(PrivateSchedulerRecurrenceKind, obj["recurrence_kind"]),
        weekdays=tuple(_enum(PrivateSchedulerWeekday, item) for item in _array(obj["weekdays"])),
        day_of_month=None if day_of_month is None else _integer(day_of_month),
        work_kind=_enum(PrivateSchedulerWorkKind, obj["work_kind"]),
        scope=_enum(PrivateSchedulerScope, obj["scope"]),
        owner_id=_uuid(obj["owner_id"]),
        portfolio_id=_uuid(obj["portfolio_id"]),
        policy_key=_text(obj["policy_key"]),
        policy_revision=_integer(obj["policy_revision"]),
    )


# --- calendar evidence -------------------------------------------------------------------------------------------

def _serialize_evidence(evidence: PrivateSchedulerCalendarDateEvidence) -> dict[str, object]:
    return {
        "calendar_key": evidence.calendar_key,
        "calendar_revision": evidence.calendar_revision,
        "calendar_kind": evidence.calendar_kind.value,
        "timezone_key": evidence.timezone_key,
        "local_date": evidence.local_date.isoformat(),
        "coverage": evidence.coverage.value,
        "exchange_sessions": [
            {"opens_at": _instant_text(window.opens_at), "closes_at": _instant_text(window.closes_at)} for window in evidence.exchange_sessions
        ],
        "source_releases": [
            {"release_key": release.release_key, "time_precision": release.time_precision.value, "planned_for": _instant_text(release.planned_for)}
            for release in evidence.source_releases
        ],
        "source_key": evidence.source_key,
        "published_at": _instant_text(evidence.published_at),
        "observed_at": _instant_text(evidence.observed_at),
        "source_content_sha256": evidence.source_content_sha256,
    }


def _hydrate_evidence(raw: object) -> PrivateSchedulerCalendarDateEvidence:
    obj = _object(raw, _EVIDENCE_KEYS)
    sessions = []
    for item in _array(obj["exchange_sessions"]):
        window = _object(item, _SESSION_KEYS)
        sessions.append(PrivateSchedulerExchangeSessionWindow(opens_at=_instant(window["opens_at"]), closes_at=_instant(window["closes_at"])))
    releases = []
    for item in _array(obj["source_releases"]):
        release = _object(item, _RELEASE_KEYS)
        releases.append(PrivateSchedulerSourceReleasePlanEntry(
            release_key=_text(release["release_key"]),
            time_precision=_enum(PrivateSchedulerReleaseTimePrecision, release["time_precision"]),
            planned_for=_nullable_instant(release["planned_for"]),
        ))
    return build_private_scheduler_calendar_date_evidence(
        calendar_key=_text(obj["calendar_key"]),
        calendar_revision=_integer(obj["calendar_revision"]),
        calendar_kind=_enum(PrivateSchedulerCalendarKind, obj["calendar_kind"]),
        timezone_key=_text(obj["timezone_key"]),
        local_date=_date(obj["local_date"]),
        coverage=_enum(PrivateSchedulerCalendarCoverage, obj["coverage"]),
        exchange_sessions=tuple(sessions),
        source_releases=tuple(releases),
        source_key=_text(obj["source_key"]),
        published_at=_nullable_instant(obj["published_at"]),
        observed_at=_instant(obj["observed_at"]),
        source_content_sha256=_text(obj["source_content_sha256"]),
    )


# --- serialization -----------------------------------------------------------------------------------------------

def serialize_private_scheduler_run_admission(*, admission: PrivateSchedulerRunAdmission) -> dict[str, object]:
    """Serialize a closed run admission into the canonical JSON-native `admission_payload` object."""
    if type(admission) is not PrivateSchedulerRunAdmission:
        raise TypeError(_ERR_ADMISSION_TYPE)
    if admission.source is PrivateSchedulerRunAdmissionSource.SCHEDULED_DIRECT:
        occurrence = admission.scheduled_occurrence
        trigger = occurrence.trigger
        provenance: dict[str, object] = {
            "scheduled_occurrence": {
                "schedule_key": occurrence.schedule_key,
                "schedule_revision": occurrence.schedule_revision,
                "timezone_key": occurrence.timezone_key,
                "local_date": occurrence.local_date.isoformat(),
                "local_time": occurrence.local_time.isoformat(timespec="microseconds"),
                "ambiguous_time_policy": occurrence.ambiguous_time_policy.value,
                "work_kind": trigger.work_kind.value,
                "scope": trigger.scope.value,
                "owner_id": _uuid_text(trigger.owner_id),
                "portfolio_id": _uuid_text(trigger.portfolio_id),
                "policy_key": trigger.policy_key,
                "policy_revision": trigger.policy_revision,
                "expected_scheduled_for": _instant_text(trigger.scheduled_for),
                "expected_idempotency_sha256": trigger.idempotency_sha256,
            }
        }
    elif admission.source is PrivateSchedulerRunAdmissionSource.EVENT_DRIVEN:
        trigger = admission.event_occurrence.trigger
        provenance = {
            "event_occurrence": {
                "work_kind": trigger.work_kind.value,
                "scope": trigger.scope.value,
                "owner_id": _uuid_text(trigger.owner_id),
                "portfolio_id": _uuid_text(trigger.portfolio_id),
                "event_cause_kind": trigger.event_cause_kind.value,
                "cause_key": trigger.cause_key,
                "cause_available_at": _instant_text(trigger.cause_available_at),
                "policy_key": trigger.policy_key,
                "policy_revision": trigger.policy_revision,
                "expected_idempotency_sha256": trigger.idempotency_sha256,
            }
        }
    else:
        applicability = admission.calendar_applicability
        decision = applicability.decision
        binding = applicability.calendar_binding
        constraint = applicability.constraint
        provenance = {
            "civil_decision": {
                "recurrence": _serialize_recurrence(decision.recurrence),
                "local_date": decision.local_date.isoformat(),
                "expected_eligibility": decision.eligibility.value,
            },
            "calendar_binding": {
                "evidence": _serialize_evidence(binding.evidence),
                "knowledge_cutoff": _instant_text(binding.knowledge_cutoff),
            },
            "calendar_constraint": {
                "calendar_key": constraint.calendar_key,
                "calendar_revision": constraint.calendar_revision,
                "calendar_kind": constraint.calendar_kind.value,
                "applicability_mode": constraint.applicability_mode.value,
                "release_key": constraint.release_key,
            },
            "expected_applicability_status": applicability.status.value,
            "expected_applicability_reason": applicability.reason.value,
            "expected_candidate_idempotency_sha256": applicability.candidate_occurrence.trigger.idempotency_sha256,
        }
    return {
        "protocol": _PROTOCOL,
        "source": admission.source.value,
        "run_idempotency_sha256": admission.run_idempotency_sha256,
        "provenance": provenance,
    }


# --- hydration ---------------------------------------------------------------------------------------------------

def _hydrate_scheduled(raw: object) -> PrivateSchedulerRunAdmission:
    wrapper = _object(raw, ("scheduled_occurrence",))
    obj = _object(wrapper["scheduled_occurrence"], _SCHEDULED_KEYS)
    occurrence = build_private_scheduler_scheduled_occurrence(
        schedule_key=_text(obj["schedule_key"]),
        schedule_revision=_integer(obj["schedule_revision"]),
        timezone_key=_text(obj["timezone_key"]),
        local_date=_date(obj["local_date"]),
        local_time=_time(obj["local_time"]),
        ambiguous_time_policy=_enum(PrivateSchedulerAmbiguousTimePolicy, obj["ambiguous_time_policy"]),
        work_kind=_enum(PrivateSchedulerWorkKind, obj["work_kind"]),
        scope=_enum(PrivateSchedulerScope, obj["scope"]),
        owner_id=_uuid(obj["owner_id"]),
        portfolio_id=_uuid(obj["portfolio_id"]),
        policy_key=_text(obj["policy_key"]),
        policy_revision=_integer(obj["policy_revision"]),
    )
    _instant(obj["expected_scheduled_for"])
    _expect(_instant_text(occurrence.trigger.scheduled_for), obj["expected_scheduled_for"])
    _expect(occurrence.trigger.idempotency_sha256, _text(obj["expected_idempotency_sha256"]))
    return admit_private_scheduler_scheduled_occurrence(occurrence=occurrence)


def _hydrate_event(raw: object) -> PrivateSchedulerRunAdmission:
    wrapper = _object(raw, ("event_occurrence",))
    obj = _object(wrapper["event_occurrence"], _EVENT_KEYS)
    occurrence = build_private_scheduler_event_occurrence(
        work_kind=_enum(PrivateSchedulerWorkKind, obj["work_kind"]),
        scope=_enum(PrivateSchedulerScope, obj["scope"]),
        owner_id=_uuid(obj["owner_id"]),
        portfolio_id=_uuid(obj["portfolio_id"]),
        event_cause_kind=_enum(PrivateSchedulerEventCauseKind, obj["event_cause_kind"]),
        cause_key=_text(obj["cause_key"]),
        cause_available_at=_instant(obj["cause_available_at"]),
        policy_key=_text(obj["policy_key"]),
        policy_revision=_integer(obj["policy_revision"]),
    )
    _expect(_instant_text(occurrence.trigger.cause_available_at), obj["cause_available_at"])      # the audit offset representation is exact
    _expect(occurrence.trigger.idempotency_sha256, _text(obj["expected_idempotency_sha256"]))
    return admit_private_scheduler_event_occurrence(occurrence=occurrence)


def _hydrate_calendar(raw: object) -> PrivateSchedulerRunAdmission:
    obj = _object(raw, _CALENDAR_KEYS)
    decision_obj = _object(obj["civil_decision"], _DECISION_KEYS)
    decision = evaluate_private_scheduler_civil_date(
        recurrence=_hydrate_recurrence(decision_obj["recurrence"]),
        local_date=_date(decision_obj["local_date"]),
    )
    _expect(decision.eligibility.value, _text(decision_obj["expected_eligibility"]))
    binding_obj = _object(obj["calendar_binding"], _BINDING_KEYS)
    binding = bind_private_scheduler_calendar_pit(
        evidence=_hydrate_evidence(binding_obj["evidence"]),
        knowledge_cutoff=_instant(binding_obj["knowledge_cutoff"]),
    )
    _expect(_instant_text(binding.knowledge_cutoff), binding_obj["knowledge_cutoff"])
    constraint_obj = _object(obj["calendar_constraint"], _CONSTRAINT_KEYS)
    constraint = build_private_scheduler_calendar_constraint(
        calendar_key=_text(constraint_obj["calendar_key"]),
        calendar_revision=_integer(constraint_obj["calendar_revision"]),
        calendar_kind=_enum(PrivateSchedulerCalendarKind, constraint_obj["calendar_kind"]),
        applicability_mode=_enum(PrivateSchedulerCalendarApplicabilityMode, constraint_obj["applicability_mode"]),
        release_key=_nullable_text(constraint_obj["release_key"]),
    )
    applicability = evaluate_private_scheduler_calendar_applicability(decision=decision, calendar_binding=binding, constraint=constraint)
    _expect(applicability.status.value, _text(obj["expected_applicability_status"]))
    _expect(applicability.reason.value, _text(obj["expected_applicability_reason"]))
    _expect(applicability.candidate_occurrence.trigger.idempotency_sha256, _text(obj["expected_candidate_idempotency_sha256"]))
    if applicability.status is not PrivateSchedulerCalendarApplicabilityStatus.APPLICABLE:
        raise ValueError(_ERR_NOT_APPLICABLE)
    return admit_private_scheduler_calendar_applicability(applicability=applicability)


def hydrate_private_scheduler_run_admission(*, payload: dict[str, object]) -> PrivateSchedulerRunAdmission:
    """Rebuild the closed run admission from a persisted payload through the closed public builders only, failing closed on any mismatch."""
    if type(payload) is not dict:
        raise TypeError(_ERR_PAYLOAD_TYPE)
    obj = _object(payload, _TOP_KEYS)
    if _text(obj["protocol"]) != _PROTOCOL:
        raise ValueError(_ERR_PROTOCOL)
    source = _enum(PrivateSchedulerRunAdmissionSource, obj["source"])
    stored_hash = _text(obj["run_idempotency_sha256"])
    if source is PrivateSchedulerRunAdmissionSource.SCHEDULED_DIRECT:
        admission = _hydrate_scheduled(obj["provenance"])
    elif source is PrivateSchedulerRunAdmissionSource.EVENT_DRIVEN:
        admission = _hydrate_event(obj["provenance"])
    else:
        admission = _hydrate_calendar(obj["provenance"])
    if admission.run_idempotency_sha256 != stored_hash:
        raise ValueError(_ERR_RUN_HASH)
    return admission
