"""
backend/engine/private/scheduler_scheduled_occurrence.py
========================================================
Timezone-safe materialization of one explicit scheduled occurrence into the closed Phase 24A trigger (Phase 24B1).

Phase 24A accepts an exact aware `scheduled_for` but does not calculate it. This module turns ONE explicitly supplied IANA timezone,
local calendar date and local wall-clock time into one deterministic UTC instant, then materializes the closed Phase 24A
`PrivateSchedulerTrigger` (always SCHEDULED, with no event cause) carrying a deterministic scheduled-occurrence idempotency identity.
The UTC instant lives only at `occurrence.trigger.scheduled_for` (canonical UTC); the zone, date and wall time stay on the occurrence
for provenance and reproducibility. Which date should run, whether it is a trading day or holiday, which recurrence produced it and
whether it is due or should dispatch are not decided here (24B2 owns recurrence and calendar eligibility; 24C and 24D own dispatch,
run authority and persistence).

Timezone authority: `zoneinfo.ZoneInfo(timezone_key)` is the only time-rule authority (the repository already ships the `tzdata` package
as the platform fallback); there is no private offset table, no alias normalization, no manual UTC offset, no UTC fallback and no
network. `timezone_key` is an exact, trimmed, printable str of 1..128 characters preserved as supplied. `local_date` must be an exact
`date` (a `datetime` is rejected, nothing is parsed) and `local_time` an exact naive `time` with fold 0: ambiguity is controlled only by
the explicit policy, never by hidden state inside the time value.

Wall-time resolution never trusts `replace(tzinfo=...)` alone. For fold 0 and fold 1 the naive wall time is attached to the zone,
converted to UTC and round-tripped back into the zone; a candidate is accepted only if the round trip reproduces the requested wall
fields exactly (date, hour, minute, second, microsecond). Then:

    no valid candidate        NONEXISTENT (a nonexistent time in the spring-forward gap): always rejected, no silent shift forward or backward
    one UTC instant           UNAMBIGUOUS: that exact instant, under every policy
    two different UTC instants  AMBIGUOUS (repeated hour): REJECT fails closed; EARLIER / LATER pick the chronologically earlier / later
                              UTC instant (by UTC ordering, not by fold number)

`PrivateSchedulerAmbiguousTimePolicy` is part of the versioned schedule configuration and of the idempotency identity even when the
wall time happens to be unambiguous.

Deterministic idempotency: `idempotency_sha256` is derived here, never caller supplied. It is SHA-256 over one canonical UTF-8 JSON
document (sort_keys=True, compact separators, explicit nulls, UUID canonical strings, enum values, isoformat microsecond timestamps; no
repr, object hash, binary object serialization, random salt or ambient value) that starts with the fixed protocol marker
`sentinax.private.scheduler.scheduled-occurrence.v1` (domain separation from any future event-driven hashing) and includes schedule key
and revision, timezone key, local date and time, ambiguity policy, the resolved UTC instant, work kind, scope, owner, portfolio, policy
key and policy revision. Two schedules that resolve to the same UTC instant but differ in schedule or zone configuration therefore have
different identities: occurrence identity is not merely a timestamp hash.

Phase 24A stays the semantic authority for work kind, scope, owner, portfolio, policy key and revision: the materializer delegates to
`build_private_scheduler_trigger`. `PrivateSchedulerScheduledOccurrence` recomputes the expected UTC instant and idempotency identity on
direct construction and rejects any forged trigger (wrong instant, non-UTC representation, wrong hash, EVENT_DRIVEN kind, other
authority).

Exclusions (each stated plainly):
    - no recurrence of any kind (daily, weekly, monthly, cron, RRULE, next / previous occurrence, date iteration)
    - no market calendar, trading-day, holiday or source-release logic and no business-day adjustment
    - no hardcoded methodology clock times
    - no ambient clock and no past or future validation (historical and future occurrences may be materialized, which replay needs)
    - no network or provider call
    - no dispatch, execution state, retry, lease or persistence
    - no use of the legacy `backend/infrastructure/scheduler.py`

Architectural Invariants:
    - Pure domain module: standard library (including `zoneinfo`, `hashlib`, `json`) plus the closed Phase 24A surface. No other Sentinax
      import, no async, no network, no database. Exact concrete types are required and subclasses are rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerScope,
    PrivateSchedulerTrigger,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_PROTOCOL = "sentinax.private.scheduler.scheduled-occurrence.v1"
_PLACEHOLDER_HASH = "0" * 64

_ERR_SCHEDULE_KEY_TYPE = "schedule_key must be an exact str instance"
_ERR_SCHEDULE_KEY_VALUE = "schedule_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_REVISION_TYPE = "schedule_revision must be an exact int instance"
_ERR_REVISION_VALUE = "schedule_revision must be at least 1"
_ERR_TIMEZONE_TYPE = "timezone_key must be an exact str instance"
_ERR_TIMEZONE_VALUE = "timezone_key must be a printable, already trimmed IANA key of 1 to 128 characters"
_ERR_TIMEZONE_UNKNOWN = "timezone_key is not an available IANA timezone"
_ERR_DATE = "local_date must be an exact date instance"
_ERR_TIME_TYPE = "local_time must be an exact naive time instance"
_ERR_TIME_FOLD = "local_time must have fold 0; ambiguity is controlled by ambiguous_time_policy only"
_ERR_POLICY = "ambiguous_time_policy must be an exact PrivateSchedulerAmbiguousTimePolicy instance"
_ERR_NONEXISTENT = "local wall time does not exist in the timezone (nonexistent or out of range): it is never shifted"
_ERR_AMBIGUOUS = "local wall time is ambiguous in the timezone and the policy is REJECT"
_ERR_TRIGGER_TYPE = "trigger must be an exact PrivateSchedulerTrigger instance"
_ERR_TRIGGER_KIND = "trigger must be a SCHEDULED trigger"
_ERR_TRIGGER_INSTANT = "trigger.scheduled_for must be the exact canonical UTC instant of the stored schedule"
_ERR_TRIGGER_IDEMPOTENCY = "trigger idempotency identity must match the canonical scheduled-occurrence identity"


class PrivateSchedulerAmbiguousTimePolicy(Enum):
    """How an ambiguous (repeated) local wall time is resolved; part of the versioned schedule configuration."""
    REJECT = "reject"
    EARLIER = "earlier"
    LATER = "later"


def _validate_schedule(
    schedule_key: object,
    schedule_revision: object,
    timezone_key: object,
    local_date: object,
    local_time: object,
    ambiguous_time_policy: object,
) -> ZoneInfo:
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
        zone = ZoneInfo(timezone_key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(_ERR_TIMEZONE_UNKNOWN) from None
    if type(local_date) is not date:
        raise TypeError(_ERR_DATE)
    if type(local_time) is not time or local_time.tzinfo is not None:
        raise TypeError(_ERR_TIME_TYPE)
    if local_time.fold != 0:
        raise ValueError(_ERR_TIME_FOLD)
    if type(ambiguous_time_policy) is not PrivateSchedulerAmbiguousTimePolicy:
        raise TypeError(_ERR_POLICY)
    return zone


def _resolve_utc(
    zone: ZoneInfo,
    local_date: date,
    local_time: time,
    policy: PrivateSchedulerAmbiguousTimePolicy,
) -> datetime:
    """Round-trip checked UTC instant of a local wall time; nonexistent times are rejected and ambiguity needs an explicit policy."""
    wall = datetime.combine(local_date, local_time)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        try:
            instant = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            returned = instant.astimezone(zone).replace(tzinfo=None, fold=0)
        except (OverflowError, ValueError, OSError):
            continue
        if returned == wall:  # exact date / hour / minute / second / microsecond round trip
            candidates.add(instant)
    if not candidates:
        raise ValueError(_ERR_NONEXISTENT)
    if len(candidates) == 1:
        return next(iter(candidates))
    if policy is PrivateSchedulerAmbiguousTimePolicy.REJECT:
        raise ValueError(_ERR_AMBIGUOUS)
    return min(candidates) if policy is PrivateSchedulerAmbiguousTimePolicy.EARLIER else max(candidates)


def _idempotency(
    schedule_key: str,
    schedule_revision: int,
    timezone_key: str,
    local_date: date,
    local_time: time,
    policy: PrivateSchedulerAmbiguousTimePolicy,
    scheduled_for: datetime,
    work_kind: PrivateSchedulerWorkKind,
    scope: PrivateSchedulerScope,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    policy_key: str,
    policy_revision: int,
) -> str:
    payload = {
        "protocol": _PROTOCOL,
        "schedule_key": schedule_key,
        "schedule_revision": schedule_revision,
        "timezone_key": timezone_key,
        "local_date": local_date.isoformat(),
        "local_time": local_time.isoformat(timespec="microseconds"),
        "ambiguous_time_policy": policy.value,
        "scheduled_for": scheduled_for.isoformat(timespec="microseconds"),
        "work_kind": work_kind.value,
        "scope": scope.value,
        "owner_id": None if owner_id is None else str(owner_id),
        "portfolio_id": None if portfolio_id is None else str(portfolio_id),
        "policy_key": policy_key,
        "policy_revision": policy_revision,
    }
    document = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PrivateSchedulerScheduledOccurrence:
    """One explicit scheduled occurrence: the stored schedule configuration plus the closed Phase 24A trigger it materializes."""
    schedule_key: str
    schedule_revision: int
    timezone_key: str
    local_date: date
    local_time: time
    ambiguous_time_policy: PrivateSchedulerAmbiguousTimePolicy
    trigger: PrivateSchedulerTrigger

    def __post_init__(self) -> None:
        zone = _validate_schedule(self.schedule_key, self.schedule_revision, self.timezone_key, self.local_date, self.local_time,
                                  self.ambiguous_time_policy)
        if type(self.trigger) is not PrivateSchedulerTrigger:
            raise TypeError(_ERR_TRIGGER_TYPE)
        trigger = self.trigger
        if trigger.trigger_kind is not PrivateSchedulerTriggerKind.SCHEDULED:
            raise ValueError(_ERR_TRIGGER_KIND)
        expected = _resolve_utc(zone, self.local_date, self.local_time, self.ambiguous_time_policy)
        if type(trigger.scheduled_for) is not datetime or trigger.scheduled_for.tzinfo is not timezone.utc or trigger.scheduled_for != expected:
            raise ValueError(_ERR_TRIGGER_INSTANT)
        expected_hash = _idempotency(
            self.schedule_key, self.schedule_revision, self.timezone_key, self.local_date, self.local_time, self.ambiguous_time_policy,
            expected, trigger.work_kind, trigger.scope, trigger.owner_id, trigger.portfolio_id, trigger.policy_key, trigger.policy_revision,
        )
        if trigger.idempotency_sha256 != expected_hash:
            raise ValueError(_ERR_TRIGGER_IDEMPOTENCY)


def build_private_scheduler_scheduled_occurrence(
    *,
    schedule_key: str,
    schedule_revision: int,
    timezone_key: str,
    local_date: date,
    local_time: time,
    ambiguous_time_policy: PrivateSchedulerAmbiguousTimePolicy,
    work_kind: PrivateSchedulerWorkKind,
    scope: PrivateSchedulerScope,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    policy_key: str,
    policy_revision: int,
) -> PrivateSchedulerScheduledOccurrence:
    """Materialize one explicit local wall-time occurrence; the UTC instant and idempotency identity are derived, never supplied."""
    zone = _validate_schedule(schedule_key, schedule_revision, timezone_key, local_date, local_time, ambiguous_time_policy)
    scheduled_for = _resolve_utc(zone, local_date, local_time, ambiguous_time_policy)
    probe = build_private_scheduler_trigger(  # the closed Phase 24A contract validates work, scope, owner and policy
        trigger_kind=PrivateSchedulerTriggerKind.SCHEDULED, work_kind=work_kind, scope=scope, owner_id=owner_id, portfolio_id=portfolio_id,
        scheduled_for=scheduled_for, event_cause_kind=None, cause_key=None, cause_available_at=None, policy_key=policy_key,
        policy_revision=policy_revision, idempotency_sha256=_PLACEHOLDER_HASH,
    )
    identity = _idempotency(
        schedule_key, schedule_revision, timezone_key, local_date, local_time, ambiguous_time_policy, scheduled_for,
        probe.work_kind, probe.scope, probe.owner_id, probe.portfolio_id, probe.policy_key, probe.policy_revision,
    )
    trigger = build_private_scheduler_trigger(
        trigger_kind=PrivateSchedulerTriggerKind.SCHEDULED, work_kind=probe.work_kind, scope=probe.scope, owner_id=probe.owner_id,
        portfolio_id=probe.portfolio_id, scheduled_for=scheduled_for, event_cause_kind=None, cause_key=None, cause_available_at=None,
        policy_key=probe.policy_key, policy_revision=probe.policy_revision, idempotency_sha256=identity,
    )
    return PrivateSchedulerScheduledOccurrence(
        schedule_key=schedule_key, schedule_revision=schedule_revision, timezone_key=timezone_key, local_date=local_date,
        local_time=local_time, ambiguous_time_policy=ambiguous_time_policy, trigger=trigger,
    )
