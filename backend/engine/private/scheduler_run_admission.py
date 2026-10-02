"""
backend/engine/private/scheduler_run_admission.py
=================================================
Unified scheduler run admission (Phase 24C2A).

This module defines which canonical occurrence (a closed wrapper, never a raw trigger) may become a scheduler RUN CANDIDATE, through exactly three explicit provenance
paths, and preserves which path authorized it:

    SCHEDULED_DIRECT               a closed Phase 24B1 `PrivateSchedulerScheduledOccurrence`
    SCHEDULED_CALENDAR_APPLICABLE  a closed Phase 24B3B `PrivateSchedulerCalendarApplicability` whose status is APPLICABLE
    EVENT_DRIVEN                   a closed Phase 24C1 `PrivateSchedulerEventOccurrence`

A canonical occurrence is not a raw Phase 24A trigger, and a raw trigger is never an admission source. It may carry a syntactically valid caller hash with no proof that it came through 24B1
or 24C1, and a bare scheduled occurrence loses the fact that 24B3B evaluated the calendar. Run admission therefore consumes only the closed
wrapper authorities; there are three explicit builders, one per path, each with a single keyword-only input, no generic caller-facing
constructor and no builder that accepts a trigger. Direct dataclass construction is forge-resistant: one private canonical derivation, shared by
the builders and `__post_init__`, recomputes the discriminated provenance shape, the stored trigger and the run identity.

The provenance fields are a strict discriminated union: exactly the field matching the source is present and the other two are None; mixed and
all-None shapes fail. The stored `trigger` is the source-derived trigger object itself (identity, not merely equality): the occurrence trigger,
the calendar result's candidate occurrence trigger, or the event occurrence trigger. For the calendar path the APPLICABLE result itself is
retained as the audit proof that external-calendar applicability was actually evaluated; NOT_APPLICABLE and UNAVAILABLE results raise ValueError
and produce no run object (there is no skipped, blocked or no-op run).

Run identity: `run_idempotency_sha256` is exactly the occurrence trigger's `idempotency_sha256`. There is no second hash layer and no admission
source, calendar proof, clock, worker, attempt, UUID or salt in it (no second hash is generated here): one logical occurrence is at most one
logical scheduler run, and persistence will later enforce uniqueness. The admission path (the proof path) is audit provenance and not identity: the same
scheduled candidate admitted directly and admitted through an APPLICABLE calendar proof has the same run hash and a different admission source.
Which scheduled path a configured schedule may use (calendar-free schedule: direct; calendar-constrained schedule: calendar applicable) is a
configuration decision for Phase 24D; this module never silently converts one path into the other.

Exclusions: no admission clock or created / received time, no run id or UUID, no lifecycle state, no claim, lease, lock, heartbeat or
compare-and-swap (no claim and no concurrency here: Phase 24C2B defines transition and concurrency semantics, Phase 24D enforces them), no attempt
or retry, no dispatch, enqueue or worker, no persistence or uniqueness query, and no use of the legacy `backend/infrastructure/job_queue.py`
(uuid4 job ids, BackgroundTasks, Redis TTL).

Architectural Invariants:
    - Pure domain module: standard library plus the closed 24A / 24B1 / 24B3B / 24C1 public surface (no private helper). It neither hashes nor
      rebuilds a trigger. No clock, randomness, network, database or async.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from backend.engine.private.scheduler_calendar_applicability import (
    PrivateSchedulerCalendarApplicability,
    PrivateSchedulerCalendarApplicabilityStatus,
)
from backend.engine.private.scheduler_event_occurrence import PrivateSchedulerEventOccurrence
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerScheduledOccurrence
from backend.engine.private.scheduler_trigger import PrivateSchedulerTrigger, PrivateSchedulerTriggerKind

_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_SOURCE = "source must be an exact PrivateSchedulerRunAdmissionSource instance"
_ERR_SCHEDULED_TYPE = "scheduled_occurrence must be None or an exact PrivateSchedulerScheduledOccurrence instance"
_ERR_CALENDAR_TYPE = "calendar_applicability must be None or an exact PrivateSchedulerCalendarApplicability instance"
_ERR_EVENT_TYPE = "event_occurrence must be None or an exact PrivateSchedulerEventOccurrence instance"
_ERR_TRIGGER_TYPE = "trigger must be an exact PrivateSchedulerTrigger instance"
_ERR_HASH_TYPE = "run_idempotency_sha256 must be an exact str instance"
_ERR_HASH_VALUE = "run_idempotency_sha256 must be exactly 64 lowercase hexadecimal characters"
_ERR_SHAPE = "exactly the provenance field matching the admission source must be present and the other two must be None"
_ERR_TRIGGER_KIND = "the source occurrence trigger has the wrong trigger kind for this admission source"
_ERR_NOT_APPLICABLE = "only an APPLICABLE calendar result can be admitted"
_ERR_TRIGGER_MATCH = "trigger must be the source-derived trigger object"
_ERR_HASH_MATCH = "run_idempotency_sha256 must equal the occurrence trigger idempotency_sha256"


class PrivateSchedulerRunAdmissionSource(Enum):
    SCHEDULED_DIRECT = "scheduled_direct"
    SCHEDULED_CALENDAR_APPLICABLE = "scheduled_calendar_applicable"
    EVENT_DRIVEN = "event_driven"


def _derive(
    source: object,
    scheduled_occurrence: object,
    calendar_applicability: object,
    event_occurrence: object,
) -> PrivateSchedulerTrigger:
    """Single canonical derivation of the admitted trigger shared by the builders and the constructor."""
    if type(source) is not PrivateSchedulerRunAdmissionSource:
        raise TypeError(_ERR_SOURCE)
    if scheduled_occurrence is not None and type(scheduled_occurrence) is not PrivateSchedulerScheduledOccurrence:
        raise TypeError(_ERR_SCHEDULED_TYPE)
    if calendar_applicability is not None and type(calendar_applicability) is not PrivateSchedulerCalendarApplicability:
        raise TypeError(_ERR_CALENDAR_TYPE)
    if event_occurrence is not None and type(event_occurrence) is not PrivateSchedulerEventOccurrence:
        raise TypeError(_ERR_EVENT_TYPE)
    present = (scheduled_occurrence is not None, calendar_applicability is not None, event_occurrence is not None)
    if source is PrivateSchedulerRunAdmissionSource.SCHEDULED_DIRECT:
        if present != (True, False, False):
            raise ValueError(_ERR_SHAPE)
        trigger = scheduled_occurrence.trigger
        expected_kind = PrivateSchedulerTriggerKind.SCHEDULED
    elif source is PrivateSchedulerRunAdmissionSource.SCHEDULED_CALENDAR_APPLICABLE:
        if present != (False, True, False):
            raise ValueError(_ERR_SHAPE)
        if calendar_applicability.status is not PrivateSchedulerCalendarApplicabilityStatus.APPLICABLE:
            raise ValueError(_ERR_NOT_APPLICABLE)
        trigger = calendar_applicability.candidate_occurrence.trigger
        expected_kind = PrivateSchedulerTriggerKind.SCHEDULED
    else:
        if present != (False, False, True):
            raise ValueError(_ERR_SHAPE)
        trigger = event_occurrence.trigger
        expected_kind = PrivateSchedulerTriggerKind.EVENT_DRIVEN
    if trigger.trigger_kind is not expected_kind:
        raise ValueError(_ERR_TRIGGER_KIND)
    return trigger


@dataclass(frozen=True)
class PrivateSchedulerRunAdmission:
    """Immutable run candidate: the admission path (audit provenance), the source-derived trigger and the logical run identity."""
    source: PrivateSchedulerRunAdmissionSource
    scheduled_occurrence: PrivateSchedulerScheduledOccurrence | None
    calendar_applicability: PrivateSchedulerCalendarApplicability | None
    event_occurrence: PrivateSchedulerEventOccurrence | None
    trigger: PrivateSchedulerTrigger
    run_idempotency_sha256: str

    def __post_init__(self) -> None:
        if type(self.trigger) is not PrivateSchedulerTrigger:
            raise TypeError(_ERR_TRIGGER_TYPE)
        if type(self.run_idempotency_sha256) is not str:
            raise TypeError(_ERR_HASH_TYPE)
        if _SHA256.fullmatch(self.run_idempotency_sha256) is None:
            raise ValueError(_ERR_HASH_VALUE)
        expected = _derive(self.source, self.scheduled_occurrence, self.calendar_applicability, self.event_occurrence)
        if self.trigger is not expected:
            raise ValueError(_ERR_TRIGGER_MATCH)
        if self.run_idempotency_sha256 != expected.idempotency_sha256:
            raise ValueError(_ERR_HASH_MATCH)


def admit_private_scheduler_scheduled_occurrence(
    *,
    occurrence: PrivateSchedulerScheduledOccurrence,
) -> PrivateSchedulerRunAdmission:
    """Admit a closed 24B1 scheduled occurrence directly (a calendar-free schedule path)."""
    if type(occurrence) is not PrivateSchedulerScheduledOccurrence:
        raise TypeError(_ERR_SCHEDULED_TYPE)
    trigger = _derive(PrivateSchedulerRunAdmissionSource.SCHEDULED_DIRECT, occurrence, None, None)
    return PrivateSchedulerRunAdmission(
        source=PrivateSchedulerRunAdmissionSource.SCHEDULED_DIRECT,
        scheduled_occurrence=occurrence,
        calendar_applicability=None,
        event_occurrence=None,
        trigger=trigger,
        run_idempotency_sha256=trigger.idempotency_sha256,
    )


def admit_private_scheduler_calendar_applicability(
    *,
    applicability: PrivateSchedulerCalendarApplicability,
) -> PrivateSchedulerRunAdmission:
    """Admit an APPLICABLE 24B3B result, retaining it as the calendar proof; any other status raises."""
    if type(applicability) is not PrivateSchedulerCalendarApplicability:
        raise TypeError(_ERR_CALENDAR_TYPE)
    trigger = _derive(PrivateSchedulerRunAdmissionSource.SCHEDULED_CALENDAR_APPLICABLE, None, applicability, None)
    return PrivateSchedulerRunAdmission(
        source=PrivateSchedulerRunAdmissionSource.SCHEDULED_CALENDAR_APPLICABLE,
        scheduled_occurrence=None,
        calendar_applicability=applicability,
        event_occurrence=None,
        trigger=trigger,
        run_idempotency_sha256=trigger.idempotency_sha256,
    )


def admit_private_scheduler_event_occurrence(
    *,
    occurrence: PrivateSchedulerEventOccurrence,
) -> PrivateSchedulerRunAdmission:
    """Admit a closed 24C1 event occurrence."""
    if type(occurrence) is not PrivateSchedulerEventOccurrence:
        raise TypeError(_ERR_EVENT_TYPE)
    trigger = _derive(PrivateSchedulerRunAdmissionSource.EVENT_DRIVEN, None, None, occurrence)
    return PrivateSchedulerRunAdmission(
        source=PrivateSchedulerRunAdmissionSource.EVENT_DRIVEN,
        scheduled_occurrence=None,
        calendar_applicability=None,
        event_occurrence=occurrence,
        trigger=trigger,
        run_idempotency_sha256=trigger.idempotency_sha256,
    )
