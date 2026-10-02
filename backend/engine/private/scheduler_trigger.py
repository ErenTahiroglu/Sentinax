"""
backend/engine/private/scheduler_trigger.py
===========================================
Immutable private scheduler trigger envelope (Phase 24A).

This module does not run a scheduler. It is the pure domain boundary that records what work is requested, why it was triggered
(scheduled or event-driven), which system or portfolio-owner scope it belongs to, which scheduler-policy revision authorized it and
which logical occurrence / idempotency identity it carries. It never decides whether a trigger is due, what wall-clock time it should
fire, whether a market is open, whether work should execute or whether execution succeeded.

Sentinax scheduling is hybrid: SCHEDULED occurrences and EVENT_DRIVEN causes. The two are mutually exclusive discriminated shapes:

    SCHEDULED      scheduled_for is an exact aware datetime; event_cause_kind, cause_key and cause_available_at are all None
    EVENT_DRIVEN   scheduled_for is None; event_cause_kind, cause_key and cause_available_at are all present

`scheduled_for` is the caller-supplied occurrence instant, preserved exactly and not checked against any named market slot.
`cause_available_at` is the caller-supplied instant at which the event cause was available (not the occurrence time of any slot). The
event cause kind is caller-supplied causal provenance: it does not prove the underlying event occurred and implies no work kind, scope
or investment action. `cause_key` is an exact printable string of 1..128 characters, already trimmed, case preserved and never
interpreted, normalized or derived.

Scope and owner isolation: SYSTEM scope (global / source-level work) has no owner and no portfolio; PORTFOLIO scope requires an exact
owner UUID and an exact portfolio UUID (no subclass, no owner inference, no storage lookup, no anonymous or cross-owner portfolio).
SOURCE_DATA_REFRESH is SYSTEM work only; PORTFOLIO_ANALYSIS_REFRESH, PORTFOLIO_HEALTH_CHECK and GAME_CHANGER_REVIEW are PORTFOLIO work
only, so a portfolio analysis can never become an unscoped global task. The work kind is declarative orchestration intent and invokes
no engine.

Identity: `policy_key` is a strict lowercase identifier (no trimming or normalization, no value hardcoded) and `policy_revision` an
exact int >= 1 (bool, float, Decimal and strings rejected). `idempotency_sha256` is an opaque caller-supplied 64-lowercase-hex logical
occurrence identity: nothing is hashed or generated, no previous run is queried, uniqueness is not claimed, and it is not
authentication.

There is no ambient clock: no `now`, no `today`, no past or future validation against the current time (any valid aware instant is
accepted; later due / dispatch logic receives explicit caller time). Exact concrete types are required and subclasses are rejected.

Explicitly out of scope: no schedule calculation (no cron, next / previous run, weekday, business-day, holiday, exchange calendar or DST
recurrence; Phase 24B), no hardcoded clock times or polling interval, no dispatch of any engine, no execution state (pending, running,
success, failure), no retry, backoff, lease or lock, no persistence, no notification, no trade consequence (Phase 24C dispatch and run
authority, Phase 24D runtime and persistence). The legacy `backend/infrastructure/scheduler.py` is non-authoritative: its polling loop,
watchlist, scoring, indicator and notification behaviour are not imported, referenced or reproduced.

Architectural Invariants:
    - Pure domain module: standard library only and no other Sentinax import. No loops, arithmetic, clock, network, database, or async.
    - `PrivateSchedulerTrigger` re-validates its complete contract on direct construction; the builder is keyword-only with no defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_KIND = "trigger_kind must be an exact PrivateSchedulerTriggerKind instance"
_ERR_WORK = "work_kind must be an exact PrivateSchedulerWorkKind instance"
_ERR_SCOPE = "scope must be an exact PrivateSchedulerScope instance"
_ERR_SYSTEM_SCOPE = "SYSTEM scope requires owner_id and portfolio_id to be None"
_ERR_PORTFOLIO_SCOPE = "PORTFOLIO scope requires exact UUID owner_id and portfolio_id"
_ERR_WORK_SCOPE = "SOURCE_DATA_REFRESH requires SYSTEM scope and every portfolio work kind requires PORTFOLIO scope"
_ERR_SCHEDULED_SHAPE = "a SCHEDULED trigger must not carry an event cause, cause key or cause availability"
_ERR_EVENT_SHAPE = "an EVENT_DRIVEN trigger must not carry scheduled_for"
_ERR_DATETIME = "scheduled_for and cause_available_at must be exact timezone-aware datetime instances"
_ERR_CAUSE_KIND = "event_cause_kind must be an exact PrivateSchedulerEventCauseKind instance"
_ERR_CAUSE_KEY_TYPE = "cause_key must be an exact str instance"
_ERR_CAUSE_KEY_VALUE = "cause_key must be a printable, already trimmed string of 1 to 128 characters"
_ERR_POLICY_KEY_TYPE = "policy_key must be an exact str instance"
_ERR_POLICY_KEY_VALUE = "policy_key must be a canonical lowercase identifier of at most 128 characters"
_ERR_REVISION_TYPE = "policy_revision must be an exact int instance"
_ERR_REVISION_VALUE = "policy_revision must be at least 1"
_ERR_HASH_TYPE = "idempotency_sha256 must be an exact str instance"
_ERR_HASH_VALUE = "idempotency_sha256 must be exactly 64 lowercase hexadecimal characters"


class PrivateSchedulerTriggerKind(Enum):
    SCHEDULED = "scheduled"
    EVENT_DRIVEN = "event_driven"


class PrivateSchedulerScope(Enum):
    SYSTEM = "system"
    PORTFOLIO = "portfolio"


class PrivateSchedulerWorkKind(Enum):
    """Declarative orchestration intent only; invokes no engine."""
    SOURCE_DATA_REFRESH = "source_data_refresh"
    PORTFOLIO_ANALYSIS_REFRESH = "portfolio_analysis_refresh"
    PORTFOLIO_HEALTH_CHECK = "portfolio_health_check"
    GAME_CHANGER_REVIEW = "game_changer_review"


class PrivateSchedulerEventCauseKind(Enum):
    """Caller-supplied causal provenance for an EVENT_DRIVEN trigger; does not prove the event occurred."""
    DISCLOSURE_INGESTED = "disclosure_ingested"
    MACRO_RELEASE_INGESTED = "macro_release_ingested"
    POLICY_CONFIGURATION_CHANGED = "policy_configuration_changed"
    PORTFOLIO_CHANGED = "portfolio_changed"
    NEW_CASH_CONFIRMED = "new_cash_confirmed"
    USER_VIEW_CHANGED = "user_view_changed"
    RISK_LIMIT_BREACH = "risk_limit_breach"


def _check_aware(value: object) -> None:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(_ERR_DATETIME)
    try:
        if value.utcoffset() is None:
            raise TypeError(_ERR_DATETIME)
        value.astimezone(timezone.utc)
    except TypeError:
        raise TypeError(_ERR_DATETIME) from None
    except Exception:
        raise TypeError(_ERR_DATETIME) from None


@dataclass(frozen=True)
class PrivateSchedulerTrigger:
    """Immutable occurrence envelope: what, why, for whom, under which policy revision and which idempotency identity."""
    trigger_kind: PrivateSchedulerTriggerKind
    work_kind: PrivateSchedulerWorkKind
    scope: PrivateSchedulerScope
    owner_id: UUID | None
    portfolio_id: UUID | None
    scheduled_for: datetime | None
    event_cause_kind: PrivateSchedulerEventCauseKind | None
    cause_key: str | None
    cause_available_at: datetime | None
    policy_key: str
    policy_revision: int
    idempotency_sha256: str

    def __post_init__(self) -> None:
        if type(self.trigger_kind) is not PrivateSchedulerTriggerKind:
            raise TypeError(_ERR_KIND)
        if type(self.work_kind) is not PrivateSchedulerWorkKind:
            raise TypeError(_ERR_WORK)
        if type(self.scope) is not PrivateSchedulerScope:
            raise TypeError(_ERR_SCOPE)
        if self.scope is PrivateSchedulerScope.SYSTEM:
            if self.owner_id is not None or self.portfolio_id is not None:
                raise ValueError(_ERR_SYSTEM_SCOPE)
        elif type(self.owner_id) is not UUID or type(self.portfolio_id) is not UUID:
            raise TypeError(_ERR_PORTFOLIO_SCOPE)
        if (self.work_kind is PrivateSchedulerWorkKind.SOURCE_DATA_REFRESH) != (self.scope is PrivateSchedulerScope.SYSTEM):
            raise ValueError(_ERR_WORK_SCOPE)
        if self.trigger_kind is PrivateSchedulerTriggerKind.SCHEDULED:
            if self.event_cause_kind is not None or self.cause_key is not None or self.cause_available_at is not None:
                raise ValueError(_ERR_SCHEDULED_SHAPE)
            _check_aware(self.scheduled_for)
        else:
            if self.scheduled_for is not None:
                raise ValueError(_ERR_EVENT_SHAPE)
            if type(self.event_cause_kind) is not PrivateSchedulerEventCauseKind:
                raise TypeError(_ERR_CAUSE_KIND)
            if type(self.cause_key) is not str:
                raise TypeError(_ERR_CAUSE_KEY_TYPE)
            if not 1 <= len(self.cause_key) <= 128 or self.cause_key != self.cause_key.strip() or not self.cause_key.isprintable():
                raise ValueError(_ERR_CAUSE_KEY_VALUE)
            _check_aware(self.cause_available_at)
        if type(self.policy_key) is not str:
            raise TypeError(_ERR_POLICY_KEY_TYPE)
        if _IDENTIFIER.fullmatch(self.policy_key) is None:
            raise ValueError(_ERR_POLICY_KEY_VALUE)
        if type(self.policy_revision) is not int:
            raise TypeError(_ERR_REVISION_TYPE)
        if self.policy_revision < 1:
            raise ValueError(_ERR_REVISION_VALUE)
        if type(self.idempotency_sha256) is not str:
            raise TypeError(_ERR_HASH_TYPE)
        if _SHA256.fullmatch(self.idempotency_sha256) is None:
            raise ValueError(_ERR_HASH_VALUE)


def build_private_scheduler_trigger(
    *,
    trigger_kind: PrivateSchedulerTriggerKind,
    work_kind: PrivateSchedulerWorkKind,
    scope: PrivateSchedulerScope,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    scheduled_for: datetime | None,
    event_cause_kind: PrivateSchedulerEventCauseKind | None,
    cause_key: str | None,
    cause_available_at: datetime | None,
    policy_key: str,
    policy_revision: int,
    idempotency_sha256: str,
) -> PrivateSchedulerTrigger:
    """Validate an explicit trigger envelope; every input is caller authority and nothing is defaulted, derived or dispatched."""
    return PrivateSchedulerTrigger(
        trigger_kind=trigger_kind,
        work_kind=work_kind,
        scope=scope,
        owner_id=owner_id,
        portfolio_id=portfolio_id,
        scheduled_for=scheduled_for,
        event_cause_kind=event_cause_kind,
        cause_key=cause_key,
        cause_available_at=cause_available_at,
        policy_key=policy_key,
        policy_revision=policy_revision,
        idempotency_sha256=idempotency_sha256,
    )
