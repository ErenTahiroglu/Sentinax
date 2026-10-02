"""
backend/engine/private/scheduler_event_occurrence.py
====================================================
Deterministic EVENT_DRIVEN trigger materialization (Phase 24C1).

Phase 24A accepts `idempotency_sha256` as opaque caller authority, which suffices for the primitive envelope but not for the production
event path. Scheduled triggers already get a derived identity from Phase 24B1; this module is the symmetric event-driven path: an explicit
event cause plus work, scope, owner and policy authority becomes one closed Phase 24A EVENT_DRIVEN trigger whose idempotency identity is
derived here and can never be supplied or overridden by a caller. The builder has no argument for the trigger kind, `scheduled_for` or the hash.

Phase 24A stays the semantic authority for the work / scope matrix, owner and portfolio UUIDs, cause key, aware instants, policy key and
policy revision: the builder delegates to `build_private_scheduler_trigger` (first with a fixed syntactically valid placeholder hash purely
to validate; that probe trigger is never returned, stored or used as identity, then with the derived hash).

Identity: SHA-256 over one canonical JSON document (UTF-8, sort_keys=True, compact separators, explicit nulls, enum values, canonical UUID
strings; no repr, object hash, pickle, random salt, uuid4 or clock) that starts with the single fixed, non-configurable protocol marker
`sentinax.private.scheduler.event-occurrence.v1` (a domain-separated hash, so an event identity can never collide with the scheduled-occurrence
domain) and contains exactly: trigger kind "event_driven", work kind, scope, owner, portfolio, scheduled_for null, event cause kind, cause key,
cause_available_at, policy key and policy revision. `cause_available_at` is canonicalized to UTC with microsecond precision for the hash only, so
the same instant written with different offsets gives the same identity, while the stored trigger keeps the caller's original aware datetime
exactly as Phase 24A permits. The cause key is hashed exactly as stored (no case folding, trimming or normalization).

Consequences: identical inputs always give an equal occurrence and hash; changing the work kind, scope, owner or portfolio, cause kind, cause key,
the cause instant by even one microsecond, the policy key or the policy revision changes the hash. The same external event for two portfolios or
owners, or for two work kinds, is distinct (owner isolation, no global event-only deduplication), and the same cause key under two cause kinds is
distinct.

`PrivateSchedulerEventOccurrence` stores exactly one field, the closed trigger. Direct construction requires an exact `PrivateSchedulerTrigger`
of kind EVENT_DRIVEN (a scheduled trigger is rejected with ValueError) and recomputes the canonical hash from the trigger's own fields, rejecting
any trigger whose hash is merely a syntactically valid 64-hex string: that is the reason the wrapper exists.

Out of scope: no event-occurrence proof (the explicit envelope is canonicalized, never verified, and `cause_available_at` is not compared with a
clock), no previous-run lookup, duplicate search, history or revision resolution (uniqueness belongs to persistence), no dispatch, enqueue or
worker, no run state, lifecycle, concurrency claim, lease or retry (Phase 24C2 defines run admission and lifecycle semantics, Phase 24D implements
storage and runtime), no persistence, and no use of the legacy `backend/infrastructure/job_queue.py` or scheduler.

Architectural Invariants:
    - Pure domain module: standard library (`hashlib`, `json`) plus the closed Phase 24A public surface. No other Sentinax import, no clock, no
      randomness, no network, no database, no async.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerTrigger,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)

_PROTOCOL = "sentinax.private.scheduler.event-occurrence.v1"
_PROBE_HASH = "0000000000000000000000000000000000000000000000000000000000000000"

_ERR_TRIGGER_TYPE = "trigger must be an exact PrivateSchedulerTrigger instance"
_ERR_TRIGGER_KIND = "trigger must be an EVENT_DRIVEN trigger"
_ERR_IDEMPOTENCY = "trigger idempotency identity must match the canonical event-occurrence identity"


def _canonical_payload(
    work_kind: PrivateSchedulerWorkKind,
    scope: PrivateSchedulerScope,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    event_cause_kind: PrivateSchedulerEventCauseKind,
    cause_key: str,
    cause_available_at: datetime,
    policy_key: str,
    policy_revision: int,
) -> dict[str, object]:
    return {
        "protocol": _PROTOCOL,
        "trigger_kind": PrivateSchedulerTriggerKind.EVENT_DRIVEN.value,
        "work_kind": work_kind.value,
        "scope": scope.value,
        "owner_id": None if owner_id is None else str(owner_id),
        "portfolio_id": None if portfolio_id is None else str(portfolio_id),
        "scheduled_for": None,
        "event_cause_kind": event_cause_kind.value,
        "cause_key": cause_key,
        "cause_available_at": cause_available_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "policy_key": policy_key,
        "policy_revision": policy_revision,
    }


def _event_idempotency(trigger: PrivateSchedulerTrigger) -> str:
    payload = _canonical_payload(
        trigger.work_kind, trigger.scope, trigger.owner_id, trigger.portfolio_id, trigger.event_cause_kind, trigger.cause_key,
        trigger.cause_available_at, trigger.policy_key, trigger.policy_revision,
    )
    document = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PrivateSchedulerEventOccurrence:
    """One closed Phase 24A EVENT_DRIVEN trigger whose idempotency identity is the canonical derived event hash."""
    trigger: PrivateSchedulerTrigger

    def __post_init__(self) -> None:
        if type(self.trigger) is not PrivateSchedulerTrigger:
            raise TypeError(_ERR_TRIGGER_TYPE)
        if self.trigger.trigger_kind is not PrivateSchedulerTriggerKind.EVENT_DRIVEN:
            raise ValueError(_ERR_TRIGGER_KIND)
        if self.trigger.idempotency_sha256 != _event_idempotency(self.trigger):
            raise ValueError(_ERR_IDEMPOTENCY)


def build_private_scheduler_event_occurrence(
    *,
    work_kind: PrivateSchedulerWorkKind,
    scope: PrivateSchedulerScope,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    event_cause_kind: PrivateSchedulerEventCauseKind,
    cause_key: str,
    cause_available_at: datetime,
    policy_key: str,
    policy_revision: int,
) -> PrivateSchedulerEventOccurrence:
    """Materialize an explicit event cause as a closed EVENT_DRIVEN trigger with a derived, domain-separated idempotency identity."""
    probe = build_private_scheduler_trigger(  # Phase 24A validates every field; the probe is discarded and never becomes identity
        trigger_kind=PrivateSchedulerTriggerKind.EVENT_DRIVEN,
        work_kind=work_kind,
        scope=scope,
        owner_id=owner_id,
        portfolio_id=portfolio_id,
        scheduled_for=None,
        event_cause_kind=event_cause_kind,
        cause_key=cause_key,
        cause_available_at=cause_available_at,
        policy_key=policy_key,
        policy_revision=policy_revision,
        idempotency_sha256=_PROBE_HASH,
    )
    trigger = build_private_scheduler_trigger(
        trigger_kind=PrivateSchedulerTriggerKind.EVENT_DRIVEN,
        work_kind=probe.work_kind,
        scope=probe.scope,
        owner_id=probe.owner_id,
        portfolio_id=probe.portfolio_id,
        scheduled_for=None,
        event_cause_kind=probe.event_cause_kind,
        cause_key=probe.cause_key,
        cause_available_at=probe.cause_available_at,
        policy_key=probe.policy_key,
        policy_revision=probe.policy_revision,
        idempotency_sha256=_event_idempotency(probe),
    )
    return PrivateSchedulerEventOccurrence(trigger=trigger)
