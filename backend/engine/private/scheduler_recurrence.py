"""
backend/engine/private/scheduler_recurrence.py
==============================================
Versioned civil recurrence definition and explicit-date eligibility (Phase 24B2).

Chain: 24A trigger envelope -> 24B1 local wall time to canonical UTC occurrence -> 24B2 civil recurrence eligibility -> (later) 24B3
market / source calendar authority, 24C dispatch and run authority, 24D runtime and persistence. This module answers only: given ONE
explicit local calendar date, does this versioned CIVIL recurrence consider that date eligible, and if so materialize the exact
scheduled occurrence through the closed Phase 24B1.

Civil calendar, not a market calendar: eligibility uses only the Gregorian civil calendar of Python `date`. An eligible date does not
mean a market is open, a source is available, trading is allowed or a publication is guaranteed. A weekend is an ordinary civil date:
DAILY is eligible on a Sunday and WEEKLY(SUNDAY) is eligible on a Sunday; there is no weekend-means-closed inference (24B3 owns
exchange, TEFAS, release and holiday semantics). There is no holiday logic and no business-day adjustment.

Kinds (no cron, no RRULE, no business-day or market recurrence):

    DAILY                  weekdays == () and day_of_month is None; every civil date is eligible
    WEEKLY                 weekdays: exact non-empty tuple of unique PrivateSchedulerWeekday in Monday -> Sunday order (validated, never
                           sorted), day_of_month None; eligible iff the date's weekday is listed
    MONTHLY_DAY_OF_MONTH   day_of_month: exact int in [1, 31] (bool rejected), weekdays == (); eligible iff local_date.day equals it

The short month semantics: a month that lacks the configured day (day 29, 30 or 31) simply has no eligible date; it is never moved to month
end, to the next month, backward or to a business day. February 2023 has no 29th to evaluate and April has no 31st.

Evaluation scope: this is one explicit local date per decision. `evaluate_private_scheduler_civil_date` evaluates exactly one supplied date.
There is no next-run search and no previous-run calculation, no date iteration or range scan, no search horizon, no active date range
(start / end) and no ambient clock (historical and future dates are equally valid, as replay needs). `PrivateSchedulerCivilDateDecision`
retains the recurrence by object identity and recomputes eligibility on direct construction, rejecting any forged value.

Materialization delegates to the closed Phase 24B1 `build_private_scheduler_scheduled_occurrence` with the recurrence fields and the
decision date, so there is no duplicated UTC, DST or idempotency logic. An INELIGIBLE decision raises `ValueError` (no `None`, no skipped
occurrence, no alternate date). DST stays the 24B1 authority: a civil-eligible date whose wall time is nonexistent, or ambiguous under
REJECT, fails at materialization and this module never alters the date, time or ambiguity policy to rescue it. Civil eligibility is not
a valid timezone occurrence. Also: no ambient clock, no dispatch.

Construction-time validation (the chosen approach): a recurrence must not defer obviously invalid closed-contract fields to the first
occurrence, but it also has no real date, and a date-specific check would wrongly reject valid schedules whose wall time is DST-sensitive
on some other date. Therefore: (1) work kind, scope, owner, portfolio, policy key and policy revision are validated by delegating to the
closed Phase 24A `build_private_scheduler_trigger` with a fixed aware UTC instant that is used only as a validation probe (it involves no
timezone or DST, is never stored, never affects identity and its trigger is discarded); (2) the few date-independent 24B1 fields
(schedule key and revision grammar, timezone key availability through `ZoneInfo`, local time exactness and the policy enum) get small
local checks that a parity test pins to the 24B1 behaviour; (3) 24B1 re-validates everything again on every actual date at
materialization. Constructing a recurrence therefore does not prove that every future wall occurrence is valid.

Architectural Invariants:
    - Pure domain module: standard library (including `zoneinfo` for key availability) plus the closed Phase 24A / 24B1 public surface.
      No other Sentinax import, no loops over dates, no async, no network, no database, no persistence.
    - No dispatch, execution state, retry or queue; no use of the legacy `backend/infrastructure/scheduler.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.engine.private.scheduler_scheduled_occurrence import (
    PrivateSchedulerAmbiguousTimePolicy,
    PrivateSchedulerScheduledOccurrence,
    build_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerScope,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_PROBE_HASH = "0000000000000000000000000000000000000000000000000000000000000000"

_ERR_SCHEDULE_KEY_TYPE = "schedule_key must be an exact str instance"
_ERR_SCHEDULE_KEY_VALUE = "schedule_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_REVISION_TYPE = "schedule_revision must be an exact int instance"
_ERR_REVISION_VALUE = "schedule_revision must be at least 1"
_ERR_TIMEZONE_TYPE = "timezone_key must be an exact str instance"
_ERR_TIMEZONE_VALUE = "timezone_key must be a printable, already trimmed IANA key of 1 to 128 characters"
_ERR_TIMEZONE_UNKNOWN = "timezone_key is not an available IANA timezone"
_ERR_TIME_TYPE = "local_time must be an exact naive time instance"
_ERR_TIME_FOLD = "local_time must have fold 0; ambiguity is controlled by ambiguous_time_policy only"
_ERR_POLICY = "ambiguous_time_policy must be an exact PrivateSchedulerAmbiguousTimePolicy instance"
_ERR_KIND = "recurrence_kind must be an exact PrivateSchedulerRecurrenceKind instance"
_ERR_WEEKDAYS_TYPE = "weekdays must be an exact tuple of exact PrivateSchedulerWeekday instances"
_ERR_DAILY_SHAPE = "a DAILY recurrence requires weekdays == () and day_of_month is None"
_ERR_WEEKLY_SHAPE = "a WEEKLY recurrence requires unique, non-empty weekdays in Monday-to-Sunday order and day_of_month None"
_ERR_MONTHLY_TYPE = "day_of_month must be an exact int for a MONTHLY_DAY_OF_MONTH recurrence"
_ERR_MONTHLY_SHAPE = "a MONTHLY_DAY_OF_MONTH recurrence requires weekdays == () and 1 <= day_of_month <= 31"
_ERR_RECURRENCE_TYPE = "recurrence must be an exact PrivateSchedulerRecurrence instance"
_ERR_DATE_TYPE = "local_date must be an exact date instance"
_ERR_ELIGIBILITY_TYPE = "eligibility must be an exact PrivateSchedulerCivilDateEligibility instance"
_ERR_ELIGIBILITY_MATCH = "eligibility must match the canonical civil recurrence evaluation exactly"
_ERR_DECISION_TYPE = "decision must be an exact PrivateSchedulerCivilDateDecision instance"
_ERR_INELIGIBLE = "an ineligible civil date cannot be materialized into an occurrence"


class PrivateSchedulerRecurrenceKind(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY_DAY_OF_MONTH = "monthly_day_of_month"


class PrivateSchedulerWeekday(Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class PrivateSchedulerCivilDateEligibility(Enum):
    """Civil-calendar eligibility only; there is no unknown, market-closed or holiday state."""
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


# Representation mapping from Python's date.weekday() index (Monday == 0) to the stored enum; not schedule authority.
_WEEKDAY_BY_INDEX = (
    PrivateSchedulerWeekday.MONDAY,
    PrivateSchedulerWeekday.TUESDAY,
    PrivateSchedulerWeekday.WEDNESDAY,
    PrivateSchedulerWeekday.THURSDAY,
    PrivateSchedulerWeekday.FRIDAY,
    PrivateSchedulerWeekday.SATURDAY,
    PrivateSchedulerWeekday.SUNDAY,
)
_WEEKDAY_POSITION = {
    PrivateSchedulerWeekday.MONDAY: 0,
    PrivateSchedulerWeekday.TUESDAY: 1,
    PrivateSchedulerWeekday.WEDNESDAY: 2,
    PrivateSchedulerWeekday.THURSDAY: 3,
    PrivateSchedulerWeekday.FRIDAY: 4,
    PrivateSchedulerWeekday.SATURDAY: 5,
    PrivateSchedulerWeekday.SUNDAY: 6,
}


def _validate_local_schedule_fields(
    schedule_key: object,
    schedule_revision: object,
    timezone_key: object,
    local_time: object,
    ambiguous_time_policy: object,
) -> None:
    """Small date-independent checks pinned to the closed 24B1 behaviour by a parity test; 24B1 re-validates at materialization."""
    if type(schedule_key) is not str:
        raise TypeError(_ERR_SCHEDULE_KEY_TYPE)
    if _IDENTIFIER.fullmatch(schedule_key) is None:
        raise ValueError(_ERR_SCHEDULE_KEY_VALUE)
    if type(schedule_revision) is not int:
        raise TypeError(_ERR_REVISION_TYPE)
    if schedule_revision < 1:
        raise ValueError(_ERR_REVISION_VALUE)
    if type(timezone_key) is not str:
        raise TypeError(_ERR_TIMEZONE_TYPE)
    if not 1 <= len(timezone_key) <= 128 or timezone_key != timezone_key.strip() or not timezone_key.isprintable():
        raise ValueError(_ERR_TIMEZONE_VALUE)
    try:
        ZoneInfo(timezone_key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(_ERR_TIMEZONE_UNKNOWN) from None
    if type(local_time) is not time or local_time.tzinfo is not None:
        raise TypeError(_ERR_TIME_TYPE)
    if local_time.fold != 0:
        raise ValueError(_ERR_TIME_FOLD)
    if type(ambiguous_time_policy) is not PrivateSchedulerAmbiguousTimePolicy:
        raise TypeError(_ERR_POLICY)


def _validate_shape(recurrence_kind: object, weekdays: object, day_of_month: object) -> None:
    if type(recurrence_kind) is not PrivateSchedulerRecurrenceKind:
        raise TypeError(_ERR_KIND)
    if type(weekdays) is not tuple:
        raise TypeError(_ERR_WEEKDAYS_TYPE)
    previous = -1
    for weekday in weekdays:
        if type(weekday) is not PrivateSchedulerWeekday:
            raise TypeError(_ERR_WEEKDAYS_TYPE)
    if recurrence_kind is PrivateSchedulerRecurrenceKind.DAILY:
        if weekdays or day_of_month is not None:
            raise ValueError(_ERR_DAILY_SHAPE)
    elif recurrence_kind is PrivateSchedulerRecurrenceKind.WEEKLY:
        if not weekdays or day_of_month is not None:
            raise ValueError(_ERR_WEEKLY_SHAPE)
        for weekday in weekdays:
            position = _WEEKDAY_POSITION[weekday]
            if position <= previous:
                raise ValueError(_ERR_WEEKLY_SHAPE)
            previous = position
    else:
        if type(day_of_month) is not int:
            raise TypeError(_ERR_MONTHLY_TYPE)
        if weekdays or not 1 <= day_of_month <= 31:
            raise ValueError(_ERR_MONTHLY_SHAPE)


@dataclass(frozen=True)
class PrivateSchedulerRecurrence:
    """Versioned civil recurrence definition: schedule configuration plus the closed 24A authority; carries no date."""
    schedule_key: str
    schedule_revision: int
    timezone_key: str
    local_time: time
    ambiguous_time_policy: PrivateSchedulerAmbiguousTimePolicy
    recurrence_kind: PrivateSchedulerRecurrenceKind
    weekdays: tuple[PrivateSchedulerWeekday, ...]
    day_of_month: int | None
    work_kind: PrivateSchedulerWorkKind
    scope: PrivateSchedulerScope
    owner_id: UUID | None
    portfolio_id: UUID | None
    policy_key: str
    policy_revision: int

    def __post_init__(self) -> None:
        _validate_local_schedule_fields(self.schedule_key, self.schedule_revision, self.timezone_key, self.local_time,
                                        self.ambiguous_time_policy)
        _validate_shape(self.recurrence_kind, self.weekdays, self.day_of_month)
        build_private_scheduler_trigger(  # validation probe only: fixed UTC instant, no timezone or DST, discarded, never stored
            trigger_kind=PrivateSchedulerTriggerKind.SCHEDULED,
            work_kind=self.work_kind,
            scope=self.scope,
            owner_id=self.owner_id,
            portfolio_id=self.portfolio_id,
            scheduled_for=datetime(1, 1, 1, tzinfo=timezone.utc),
            event_cause_kind=None,
            cause_key=None,
            cause_available_at=None,
            policy_key=self.policy_key,
            policy_revision=self.policy_revision,
            idempotency_sha256=_PROBE_HASH,
        )


def build_private_scheduler_recurrence(
    *,
    schedule_key: str,
    schedule_revision: int,
    timezone_key: str,
    local_time: time,
    ambiguous_time_policy: PrivateSchedulerAmbiguousTimePolicy,
    recurrence_kind: PrivateSchedulerRecurrenceKind,
    weekdays: tuple[PrivateSchedulerWeekday, ...],
    day_of_month: int | None,
    work_kind: PrivateSchedulerWorkKind,
    scope: PrivateSchedulerScope,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    policy_key: str,
    policy_revision: int,
) -> PrivateSchedulerRecurrence:
    """Validate an explicit civil recurrence definition; nothing is defaulted, searched or dispatched."""
    return PrivateSchedulerRecurrence(
        schedule_key=schedule_key,
        schedule_revision=schedule_revision,
        timezone_key=timezone_key,
        local_time=local_time,
        ambiguous_time_policy=ambiguous_time_policy,
        recurrence_kind=recurrence_kind,
        weekdays=weekdays,
        day_of_month=day_of_month,
        work_kind=work_kind,
        scope=scope,
        owner_id=owner_id,
        portfolio_id=portfolio_id,
        policy_key=policy_key,
        policy_revision=policy_revision,
    )


def _canonical_eligibility(recurrence: PrivateSchedulerRecurrence, local_date: date) -> PrivateSchedulerCivilDateEligibility:
    eligible = PrivateSchedulerCivilDateEligibility.ELIGIBLE
    ineligible = PrivateSchedulerCivilDateEligibility.INELIGIBLE
    if recurrence.recurrence_kind is PrivateSchedulerRecurrenceKind.DAILY:
        return eligible
    if recurrence.recurrence_kind is PrivateSchedulerRecurrenceKind.WEEKLY:
        return eligible if _WEEKDAY_BY_INDEX[local_date.weekday()] in recurrence.weekdays else ineligible
    return eligible if local_date.day == recurrence.day_of_month else ineligible


@dataclass(frozen=True)
class PrivateSchedulerCivilDateDecision:
    """Civil-date eligibility of one explicit local date; retains the recurrence by identity and rejects forged eligibility."""
    recurrence: PrivateSchedulerRecurrence
    local_date: date
    eligibility: PrivateSchedulerCivilDateEligibility

    def __post_init__(self) -> None:
        if type(self.recurrence) is not PrivateSchedulerRecurrence:
            raise TypeError(_ERR_RECURRENCE_TYPE)
        if type(self.local_date) is not date:
            raise TypeError(_ERR_DATE_TYPE)
        if type(self.eligibility) is not PrivateSchedulerCivilDateEligibility:
            raise TypeError(_ERR_ELIGIBILITY_TYPE)
        if self.eligibility is not _canonical_eligibility(self.recurrence, self.local_date):
            raise ValueError(_ERR_ELIGIBILITY_MATCH)


def evaluate_private_scheduler_civil_date(
    *,
    recurrence: PrivateSchedulerRecurrence,
    local_date: date,
) -> PrivateSchedulerCivilDateDecision:
    """Evaluate exactly one explicit civil date against one recurrence; no search, no clock, no market knowledge."""
    if type(recurrence) is not PrivateSchedulerRecurrence:
        raise TypeError(_ERR_RECURRENCE_TYPE)
    if type(local_date) is not date:
        raise TypeError(_ERR_DATE_TYPE)
    return PrivateSchedulerCivilDateDecision(
        recurrence=recurrence, local_date=local_date, eligibility=_canonical_eligibility(recurrence, local_date),
    )


def materialize_private_scheduler_civil_occurrence(
    *,
    decision: PrivateSchedulerCivilDateDecision,
) -> PrivateSchedulerScheduledOccurrence:
    """Materialize an ELIGIBLE decision through the closed 24B1 authority; an ineligible date raises and is never adjusted."""
    if type(decision) is not PrivateSchedulerCivilDateDecision:
        raise TypeError(_ERR_DECISION_TYPE)
    if decision.eligibility is not PrivateSchedulerCivilDateEligibility.ELIGIBLE:
        raise ValueError(_ERR_INELIGIBLE)
    recurrence = decision.recurrence
    return build_private_scheduler_scheduled_occurrence(
        schedule_key=recurrence.schedule_key,
        schedule_revision=recurrence.schedule_revision,
        timezone_key=recurrence.timezone_key,
        local_date=decision.local_date,
        local_time=recurrence.local_time,
        ambiguous_time_policy=recurrence.ambiguous_time_policy,
        work_kind=recurrence.work_kind,
        scope=recurrence.scope,
        owner_id=recurrence.owner_id,
        portfolio_id=recurrence.portfolio_id,
        policy_key=recurrence.policy_key,
        policy_revision=recurrence.policy_revision,
    )
