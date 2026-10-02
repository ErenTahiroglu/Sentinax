"""
backend/engine/private/scheduler_calendar_applicability.py
===========================================================
Calendar applicability policy for scheduled candidates (Phase 24B3B).

Chain: 24B2 civil eligibility -> (materialized through) 24B1 candidate scheduled occurrence -> constrained by 24B3A external evidence (a
PIT-bound calendar artifact) -> 24B3B calendar applicability -> (later) 24C dispatch and run authority. This module combines one ELIGIBLE
24B2 civil-date decision, one exact 24B3A PIT calendar binding and one explicit calendar constraint, and answers whether the candidate is
applicable under the declared external-calendar policy: APPLICABLE, NOT_APPLICABLE or UNAVAILABLE. It is scheduling metadata: no dispatch,
no execution, no run state; there is deliberately no calendar-evidence-to-dispatch shortcut.

Preconditions (anything else raises ValueError, never a status):

    - the decision must be civil ELIGIBLE (24B2 owns civil ineligibility; it is never turned into an external-calendar status)
    - the candidate occurrence is derived only through the closed 24B1 materializer (no duplicated DST, UTC or hashing logic; a
      nonexistent or ambiguous-under-REJECT wall time propagates as a failure, never as a calendar status)
    - exact matches: calendar_key, calendar_revision, calendar_kind, evidence local_date == decision local_date and evidence timezone_key ==
      recurrence timezone_key (exact string identity, no alias or offset equivalence, no nearest-date fallback, no search for another calendar)
    - anti-lookahead frontier: calendar PIT cutoff <= scheduled occurrence, by exact UTC instant (equality allowed; a cutoff earlier than
      the occurrence is valid because a scheduler may prepare ahead). With the closed 24B3A guarantee observed_at <= cutoff the chain is
      observed_at <= cutoff <= scheduled_for, so future calendar knowledge cannot leak backward into an earlier occurrence

Policy (one private derivation shared by the evaluator and the result constructor):

    coverage UNAVAILABLE      -> UNAVAILABLE / CALENDAR_COVERAGE_UNAVAILABLE, first and without inspecting empty tuples (an empty tuple is
                                 never read as closure or "no release" unless coverage is COMPLETE_FOR_DATE)
    EXCHANGE_SESSION_EXISTS   -> date-level: at least one session on the date is APPLICABLE / EXCHANGE_SESSION_PRESENT, none is
                                 NOT_APPLICABLE / EXCHANGE_NO_SESSION. The occurrence is NOT required to fall inside a session (pre-open
                                 analysis and post-close refresh are legitimately applicable on a session date) and a half-day or shortened
                                 session is still a session (no duration or normal-hours requirement)
    SOURCE_RELEASE_EXISTS     -> exact key lookup (case-sensitive, no normalization, no fuzzy match): present (DATE_ONLY or EXACT_TIME) is
                                 APPLICABLE / SOURCE_RELEASE_PRESENT, absent is NOT_APPLICABLE / SOURCE_RELEASE_NOT_PRESENT; date presence
                                 only, no timing statement
    SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME -> absent: NOT_APPLICABLE / SOURCE_RELEASE_NOT_PRESENT; DATE_ONLY: UNAVAILABLE /
                                 SOURCE_RELEASE_DATE_ONLY (the not-before rule cannot be proven without a planned instant and no time is
                                 fabricated); EXACT_TIME: candidate >= planned_for is APPLICABLE / SOURCE_RELEASE_TIME_REACHED (equality
                                 allowed), earlier is NOT_APPLICABLE / SOURCE_RELEASE_BEFORE_PLANNED_TIME, with no tolerance and no offset

A planned release is not actual availability: SOURCE_RELEASE_TIME_REACHED means only that the configured occurrence is not earlier than the
calendar's planned release instant, not that data was published, available, fetched or ingested (not actual availability; no such field
exists). The +1 or +5 minute delay of a methodology is the recurrence's wall-clock choice, never an arithmetic offset here.
`require_private_scheduler_applicable_occurrence` returns the already materialized candidate by identity for an APPLICABLE result and raises for
the other statuses; it has no dispatch effect.

Architectural Invariants:
    - Pure domain module: standard library plus the closed 24B2 / 24B1 / 24B3A public surface (no private helper). No provider, network,
      ambient clock, weekend or holiday inference, session-containment check, dispatch, queue, run state or persistence.
    - The result retains the decision, binding and constraint by identity and recomputes the candidate, status and reason on direct
      construction, rejecting any forged value. The legacy backend/infrastructure/scheduler.py is not used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timezone
from enum import Enum

from backend.engine.private.scheduler_calendar_evidence import (
    PrivateSchedulerCalendarCoverage,
    PrivateSchedulerCalendarKind,
    PrivateSchedulerCalendarPITBinding,
    PrivateSchedulerReleaseTimePrecision,
)
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerCivilDateDecision,
    PrivateSchedulerCivilDateEligibility,
    materialize_private_scheduler_civil_occurrence,
)
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerScheduledOccurrence

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")

_ERR_KEY_TYPE = "calendar_key must be an exact str instance"
_ERR_KEY_VALUE = "calendar_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_REVISION_TYPE = "calendar_revision must be an exact int instance"
_ERR_REVISION_VALUE = "calendar_revision must be at least 1"
_ERR_KIND = "calendar_kind must be an exact PrivateSchedulerCalendarKind instance"
_ERR_MODE = "applicability_mode must be an exact PrivateSchedulerCalendarApplicabilityMode instance"
_ERR_RELEASE_KEY_TYPE = "release_key must be an exact str instance for a source-release mode"
_ERR_RELEASE_KEY_VALUE = "release_key must be a printable, already trimmed string of 1 to 128 characters"
_ERR_EXCHANGE_SHAPE = "EXCHANGE_SESSION_EXISTS requires an EXCHANGE_SESSION calendar kind and release_key None"
_ERR_RELEASE_SHAPE = "source-release modes require a SOURCE_RELEASE_PLAN calendar kind"
_ERR_DECISION_TYPE = "decision must be an exact PrivateSchedulerCivilDateDecision instance"
_ERR_BINDING_TYPE = "calendar_binding must be an exact PrivateSchedulerCalendarPITBinding instance"
_ERR_CONSTRAINT_TYPE = "constraint must be an exact PrivateSchedulerCalendarConstraint instance"
_ERR_CANDIDATE_TYPE = "candidate_occurrence must be an exact PrivateSchedulerScheduledOccurrence instance"
_ERR_STATUS_TYPE = "status must be an exact PrivateSchedulerCalendarApplicabilityStatus instance"
_ERR_REASON_TYPE = "reason must be an exact PrivateSchedulerCalendarApplicabilityReason instance"
_ERR_INELIGIBLE = "a civil-ineligible decision is rejected before calendar applicability"
_ERR_IDENTITY = "calendar evidence must match the constraint calendar_key, calendar_revision and calendar_kind exactly"
_ERR_DATE = "calendar evidence local_date must equal the decision local_date"
_ERR_TIMEZONE = "calendar evidence timezone_key must equal the recurrence timezone_key exactly"
_ERR_FRONTIER = "the calendar knowledge cutoff must not be later than the scheduled occurrence"
_ERR_MATCH = "applicability must match the canonical derivation exactly"
_ERR_NOT_APPLICABLE = "only an APPLICABLE result provides an occurrence"
_ERR_APPLICABILITY_TYPE = "applicability must be an exact PrivateSchedulerCalendarApplicability instance"


class PrivateSchedulerCalendarApplicabilityMode(Enum):
    EXCHANGE_SESSION_EXISTS = "exchange_session_exists"
    SOURCE_RELEASE_EXISTS = "source_release_exists"
    SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME = "source_release_not_before_planned_time"


class PrivateSchedulerCalendarApplicabilityStatus(Enum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class PrivateSchedulerCalendarApplicabilityReason(Enum):
    EXCHANGE_SESSION_PRESENT = "exchange_session_present"
    EXCHANGE_NO_SESSION = "exchange_no_session"
    SOURCE_RELEASE_PRESENT = "source_release_present"
    SOURCE_RELEASE_NOT_PRESENT = "source_release_not_present"
    SOURCE_RELEASE_TIME_REACHED = "source_release_time_reached"
    SOURCE_RELEASE_BEFORE_PLANNED_TIME = "source_release_before_planned_time"
    SOURCE_RELEASE_DATE_ONLY = "source_release_date_only"
    CALENDAR_COVERAGE_UNAVAILABLE = "calendar_coverage_unavailable"


_STATUS = PrivateSchedulerCalendarApplicabilityStatus
_REASON = PrivateSchedulerCalendarApplicabilityReason


@dataclass(frozen=True)
class PrivateSchedulerCalendarConstraint:
    """Explicit declaration of which exact calendar contract a schedule depends on and which applicability rule applies."""
    calendar_key: str
    calendar_revision: int
    calendar_kind: PrivateSchedulerCalendarKind
    applicability_mode: PrivateSchedulerCalendarApplicabilityMode
    release_key: str | None

    def __post_init__(self) -> None:
        if type(self.calendar_key) is not str:
            raise TypeError(_ERR_KEY_TYPE)
        if _IDENTIFIER.fullmatch(self.calendar_key) is None:
            raise ValueError(_ERR_KEY_VALUE)
        if type(self.calendar_revision) is not int:
            raise TypeError(_ERR_REVISION_TYPE)
        if self.calendar_revision < 1:
            raise ValueError(_ERR_REVISION_VALUE)
        if type(self.calendar_kind) is not PrivateSchedulerCalendarKind:
            raise TypeError(_ERR_KIND)
        if type(self.applicability_mode) is not PrivateSchedulerCalendarApplicabilityMode:
            raise TypeError(_ERR_MODE)
        if self.applicability_mode is PrivateSchedulerCalendarApplicabilityMode.EXCHANGE_SESSION_EXISTS:
            if self.calendar_kind is not PrivateSchedulerCalendarKind.EXCHANGE_SESSION or self.release_key is not None:
                raise ValueError(_ERR_EXCHANGE_SHAPE)
        else:
            if self.calendar_kind is not PrivateSchedulerCalendarKind.SOURCE_RELEASE_PLAN:
                raise ValueError(_ERR_RELEASE_SHAPE)
            if type(self.release_key) is not str:
                raise TypeError(_ERR_RELEASE_KEY_TYPE)
            if not 1 <= len(self.release_key) <= 128 or self.release_key != self.release_key.strip() or not self.release_key.isprintable():
                raise ValueError(_ERR_RELEASE_KEY_VALUE)


def build_private_scheduler_calendar_constraint(
    *,
    calendar_key: str,
    calendar_revision: int,
    calendar_kind: PrivateSchedulerCalendarKind,
    applicability_mode: PrivateSchedulerCalendarApplicabilityMode,
    release_key: str | None,
) -> PrivateSchedulerCalendarConstraint:
    """Validate an explicit calendar constraint; nothing is defaulted or inferred."""
    return PrivateSchedulerCalendarConstraint(
        calendar_key=calendar_key,
        calendar_revision=calendar_revision,
        calendar_kind=calendar_kind,
        applicability_mode=applicability_mode,
        release_key=release_key,
    )


def _derive(
    decision: PrivateSchedulerCivilDateDecision,
    calendar_binding: PrivateSchedulerCalendarPITBinding,
    constraint: PrivateSchedulerCalendarConstraint,
) -> tuple[PrivateSchedulerScheduledOccurrence, _STATUS, _REASON]:
    """Single canonical derivation shared by the evaluator and the result constructor."""
    if type(decision) is not PrivateSchedulerCivilDateDecision:
        raise TypeError(_ERR_DECISION_TYPE)
    if type(calendar_binding) is not PrivateSchedulerCalendarPITBinding:
        raise TypeError(_ERR_BINDING_TYPE)
    if type(constraint) is not PrivateSchedulerCalendarConstraint:
        raise TypeError(_ERR_CONSTRAINT_TYPE)
    if decision.eligibility is not PrivateSchedulerCivilDateEligibility.ELIGIBLE:
        raise ValueError(_ERR_INELIGIBLE)
    candidate = materialize_private_scheduler_civil_occurrence(decision=decision)
    evidence = calendar_binding.evidence
    if (
        evidence.calendar_key != constraint.calendar_key
        or evidence.calendar_revision != constraint.calendar_revision
        or evidence.calendar_kind is not constraint.calendar_kind
    ):
        raise ValueError(_ERR_IDENTITY)
    if evidence.local_date != decision.local_date:
        raise ValueError(_ERR_DATE)
    if evidence.timezone_key != decision.recurrence.timezone_key:
        raise ValueError(_ERR_TIMEZONE)
    occurrence_time = candidate.trigger.scheduled_for
    if calendar_binding.knowledge_cutoff.astimezone(timezone.utc) > occurrence_time:
        raise ValueError(_ERR_FRONTIER)
    if evidence.coverage is PrivateSchedulerCalendarCoverage.UNAVAILABLE:
        return (candidate, _STATUS.UNAVAILABLE, _REASON.CALENDAR_COVERAGE_UNAVAILABLE)
    mode = constraint.applicability_mode
    if mode is PrivateSchedulerCalendarApplicabilityMode.EXCHANGE_SESSION_EXISTS:
        if evidence.exchange_sessions:
            return (candidate, _STATUS.APPLICABLE, _REASON.EXCHANGE_SESSION_PRESENT)
        return (candidate, _STATUS.NOT_APPLICABLE, _REASON.EXCHANGE_NO_SESSION)
    match = None
    for release in evidence.source_releases:
        if release.release_key == constraint.release_key:
            match = release
    if match is None:
        return (candidate, _STATUS.NOT_APPLICABLE, _REASON.SOURCE_RELEASE_NOT_PRESENT)
    if mode is PrivateSchedulerCalendarApplicabilityMode.SOURCE_RELEASE_EXISTS:
        return (candidate, _STATUS.APPLICABLE, _REASON.SOURCE_RELEASE_PRESENT)
    if match.time_precision is PrivateSchedulerReleaseTimePrecision.DATE_ONLY:
        return (candidate, _STATUS.UNAVAILABLE, _REASON.SOURCE_RELEASE_DATE_ONLY)
    if occurrence_time >= match.planned_for:
        return (candidate, _STATUS.APPLICABLE, _REASON.SOURCE_RELEASE_TIME_REACHED)
    return (candidate, _STATUS.NOT_APPLICABLE, _REASON.SOURCE_RELEASE_BEFORE_PLANNED_TIME)


@dataclass(frozen=True)
class PrivateSchedulerCalendarApplicability:
    """Calendar applicability of one candidate occurrence; retains its inputs by identity and rejects any forged outcome."""
    decision: PrivateSchedulerCivilDateDecision
    calendar_binding: PrivateSchedulerCalendarPITBinding
    constraint: PrivateSchedulerCalendarConstraint
    candidate_occurrence: PrivateSchedulerScheduledOccurrence
    status: PrivateSchedulerCalendarApplicabilityStatus
    reason: PrivateSchedulerCalendarApplicabilityReason

    def __post_init__(self) -> None:
        if type(self.decision) is not PrivateSchedulerCivilDateDecision:
            raise TypeError(_ERR_DECISION_TYPE)
        if type(self.calendar_binding) is not PrivateSchedulerCalendarPITBinding:
            raise TypeError(_ERR_BINDING_TYPE)
        if type(self.constraint) is not PrivateSchedulerCalendarConstraint:
            raise TypeError(_ERR_CONSTRAINT_TYPE)
        if type(self.candidate_occurrence) is not PrivateSchedulerScheduledOccurrence:
            raise TypeError(_ERR_CANDIDATE_TYPE)
        if type(self.status) is not PrivateSchedulerCalendarApplicabilityStatus:
            raise TypeError(_ERR_STATUS_TYPE)
        if type(self.reason) is not PrivateSchedulerCalendarApplicabilityReason:
            raise TypeError(_ERR_REASON_TYPE)
        candidate, status, reason = _derive(self.decision, self.calendar_binding, self.constraint)
        if self.candidate_occurrence != candidate or self.status is not status or self.reason is not reason:
            raise ValueError(_ERR_MATCH)


def evaluate_private_scheduler_calendar_applicability(
    *,
    decision: PrivateSchedulerCivilDateDecision,
    calendar_binding: PrivateSchedulerCalendarPITBinding,
    constraint: PrivateSchedulerCalendarConstraint,
) -> PrivateSchedulerCalendarApplicability:
    """Evaluate the declared external-calendar rule for one eligible civil date; evidence only, nothing is dispatched."""
    candidate, status, reason = _derive(decision, calendar_binding, constraint)
    return PrivateSchedulerCalendarApplicability(
        decision=decision,
        calendar_binding=calendar_binding,
        constraint=constraint,
        candidate_occurrence=candidate,
        status=status,
        reason=reason,
    )


def require_private_scheduler_applicable_occurrence(
    *,
    applicability: PrivateSchedulerCalendarApplicability,
) -> PrivateSchedulerScheduledOccurrence:
    """Return the materialized candidate (by identity) for an APPLICABLE result; any other status raises. No dispatch."""
    if type(applicability) is not PrivateSchedulerCalendarApplicability:
        raise TypeError(_ERR_APPLICABILITY_TYPE)
    if applicability.status is not PrivateSchedulerCalendarApplicabilityStatus.APPLICABLE:
        raise ValueError(_ERR_NOT_APPLICABLE)
    return applicability.candidate_occurrence
