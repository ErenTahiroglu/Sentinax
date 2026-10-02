"""
backend/engine/private/scheduler_run_persistence_transport.py
=============================================================
Raw PostgREST row transport for scheduler run persistence (Phase 24D2B1). Pure: no database, no RPC, no repository, no client, no clock, no randomness,
no hash. Phase 24D2B2 (the repository) will fetch rows with the explicit projections below and hand them to these functions; this module only decides
what a fetched row means.

Current-run row: `admission_payload` is hydrated through the CLOSED 24D2A codec (never a forged admission), then every immutable denormalized column of
the row (run hash, source, trigger kind, work kind, scope, owner, portfolio, scheduled_for, event cause kind, cause key, cause_available_at, policy key,
policy revision) is reconciled against the reconstructed admission. The payload and the columns are two independent representations; any disagreement
fails closed. There is no payload hash, so audit-only fields that influence neither the derivation nor the run identity are not authenticated here
(the codec's documented limit); this module adds no second identity.

Two timestamp policies coexist. The payload keeps exact audit offset strings (24D2A). PostgreSQL TIMESTAMPTZ columns come back through PostgREST as the
equivalent instant in the session offset (Z, +00:00, any other offset, variable fractional precision). Every column timestamp is therefore parsed as an
aware ISO-8601 instant and canonicalized to UTC and compared by instant, never by lexical form; naive, malformed and non-string values are rejected.
The audit offset of an event `cause_available_at` survives in the hydrated admission and is not replaced by the column's canonical UTC form.

The lifecycle columns (state, state_version, claim_key, claimed_at, lease_expires_at, terminal_at, failure_code) are exact-typed, converted to canonical
UTC and handed straight to the closed `PrivateSchedulerRunLifecycle`, whose own shape rules are the only authority. `created_at` and `updated_at` are
database metadata clocks: UTC-canonical, ordered `created_at <= updated_at`, and never identity.

History row: reconstructed against a caller-supplied closed admission (its run hash must equal the row's). A transition keeps `transition_at` separately
from the closed lifecycle snapshot because a renewal's instant (`renewed_at`) is not representable in a lifecycle (claimed_at is unchanged by a renew).
INITIALIZE has no transition_at and before_state_version None; every other kind needs before_state_version + 1 == after.state_version and a transition_at:
CLAIM and TAKE_OVER equal after.claimed_at, SUCCEED and FAIL equal after.terminal_at, and a renew satisfies after.claimed_at <= transition_at <
after.lease_expires_at. Only CLAIM may be a version-2 step; renew, takeover, succeed and fail need before_state_version >= 2 (a CLAIMED predecessor).
Without the prior snapshot the renewal cannot be proved to precede the prior lease expiry, nor the full before/after relation (for example that a renew only extends the lease); that limitation is explicit and the
per-row shape and the migration's append-only history triggers bound it. `recorded_at` is database metadata and is never inferred or compared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_run_lifecycle import (
    PrivateSchedulerRunLifecycle,
    PrivateSchedulerRunState,
    PrivateSchedulerRunTransitionKind,
)
from backend.engine.private.scheduler_run_persistence_codec import hydrate_private_scheduler_run_admission

_RUN_COLUMNS = (
    "run_idempotency_sha256", "admission_source", "trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for", "event_cause_kind",
    "cause_key", "cause_available_at", "policy_key", "policy_revision", "admission_payload", "state", "state_version", "claim_key", "claimed_at",
    "lease_expires_at", "terminal_at", "failure_code", "created_at", "updated_at",
)
_HISTORY_COLUMNS = (
    "run_idempotency_sha256", "after_state_version", "transition_kind", "before_state_version", "transition_at", "after_state", "claim_key", "claimed_at",
    "lease_expires_at", "terminal_at", "failure_code", "recorded_at",
)
PRIVATE_SCHEDULER_RUN_SELECT = ",".join(_RUN_COLUMNS)
PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT = ",".join(_HISTORY_COLUMNS)

_ERR_ROW_TYPE = "row must be a mapping"
_ERR_ROW_KEYS = "row keys must be exactly the selected projection"
_ERR_ADMISSION = "admission must be an exact PrivateSchedulerRunAdmission instance"
_ERR_TEXT = "column value must be an exact str"
_ERR_OPT_TEXT = "column value must be an exact str or None"
_ERR_INT = "column value must be an exact int"
_ERR_TIMESTAMP = "column timestamp must be an ISO-8601 string with an explicit UTC offset"
_ERR_UUID = "column value must be a canonical lowercase UUID string"
_ERR_ENUM = "column value is not a known enum value"
_ERR_MISMATCH = "denormalized column disagrees with the admission payload"
_ERR_METADATA = "created_at must not be after updated_at"
_ERR_UTC = "persisted instants must be canonical UTC datetimes"
_ERR_PERSISTED_RUN = "lifecycle must be an exact PrivateSchedulerRunLifecycle instance"
_ERR_TRANSITION_SHAPE = "transition row shape is inconsistent"
_ERR_TRANSITION_AT = "transition_at is inconsistent with the transition kind and snapshot"

_STATE = PrivateSchedulerRunState
_KIND = PrivateSchedulerRunTransitionKind
_AFTER_STATE = {_KIND.INITIALIZE: _STATE.READY, _KIND.CLAIM: _STATE.CLAIMED, _KIND.RENEW_CLAIM: _STATE.CLAIMED,
                _KIND.TAKE_OVER_EXPIRED_CLAIM: _STATE.CLAIMED, _KIND.SUCCEED: _STATE.SUCCEEDED, _KIND.FAIL: _STATE.FAILED}


def _check_utc(value: object) -> None:
    if type(value) is not datetime:
        raise TypeError(_ERR_UTC)
    if value.tzinfo is not timezone.utc:
        raise ValueError(_ERR_UTC)


@dataclass(frozen=True)
class PrivateSchedulerPersistedRun:
    """Current persisted run: the closed lifecycle snapshot plus database metadata clocks (never identity)."""
    lifecycle: PrivateSchedulerRunLifecycle
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.lifecycle) is not PrivateSchedulerRunLifecycle:
            raise TypeError(_ERR_PERSISTED_RUN)
        _check_utc(self.created_at)
        _check_utc(self.updated_at)
        if self.created_at > self.updated_at:
            raise ValueError(_ERR_METADATA)


@dataclass(frozen=True)
class PrivateSchedulerPersistedTransition:
    """One durable history row: kind, version step, the separately retained transition_at, the closed after-snapshot and the recorded_at metadata."""
    kind: PrivateSchedulerRunTransitionKind
    before_state_version: int | None
    transition_at: datetime | None
    after: PrivateSchedulerRunLifecycle
    recorded_at: datetime

    def __post_init__(self) -> None:
        if type(self.kind) is not _KIND:
            raise TypeError(_ERR_TRANSITION_SHAPE)
        if type(self.after) is not PrivateSchedulerRunLifecycle:
            raise TypeError(_ERR_PERSISTED_RUN)
        if self.before_state_version is not None and type(self.before_state_version) is not int:
            raise TypeError(_ERR_INT)
        if self.transition_at is not None:
            _check_utc(self.transition_at)
        _check_utc(self.recorded_at)
        after = self.after
        if after.state is not _AFTER_STATE[self.kind]:
            raise ValueError(_ERR_TRANSITION_SHAPE)
        if self.kind is _KIND.INITIALIZE:
            if self.before_state_version is not None or self.transition_at is not None or after.state_version != 1:
                raise ValueError(_ERR_TRANSITION_SHAPE)
            return
        if self.before_state_version is None or self.before_state_version < 1 or after.state_version != self.before_state_version + 1:
            raise ValueError(_ERR_TRANSITION_SHAPE)
        if self.transition_at is None:
            raise ValueError(_ERR_TRANSITION_AT)
        if self.kind is _KIND.CLAIM:
            if self.before_state_version != 1:
                raise ValueError(_ERR_TRANSITION_SHAPE)
        elif self.before_state_version < 2:
            raise ValueError(_ERR_TRANSITION_SHAPE)                                                 # every other kind needs a CLAIMED predecessor
        if self.kind in (_KIND.CLAIM, _KIND.TAKE_OVER_EXPIRED_CLAIM):
            valid = self.transition_at == after.claimed_at
        elif self.kind is _KIND.RENEW_CLAIM:
            valid = after.claimed_at <= self.transition_at < after.lease_expires_at
        else:
            valid = self.transition_at == after.terminal_at
        if not valid:
            raise ValueError(_ERR_TRANSITION_AT)


def _row(row: object, columns: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(row, Mapping):
        raise TypeError(_ERR_ROW_TYPE)
    keys = list(row.keys())
    if any(type(key) is not str for key in keys) or len(keys) != len(columns) or set(keys) != set(columns):
        raise ValueError(_ERR_ROW_KEYS)
    return row


def _text(value: object) -> str:
    if type(value) is not str:
        raise TypeError(_ERR_TEXT)
    return value


def _optional_text(value: object) -> str | None:
    if value is not None and type(value) is not str:
        raise TypeError(_ERR_OPT_TEXT)
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError(_ERR_INT)
    return value


def _instant(value: object) -> datetime:
    """Parse a SQL TIMESTAMPTZ transport string (Z / any offset / variable fractional precision) into a canonical UTC datetime."""
    if type(value) is not str:
        raise TypeError(_ERR_TIMESTAMP)
    if len(value) < 20 or value[10] != "T":
        raise ValueError(_ERR_TIMESTAMP)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(_ERR_TIMESTAMP) from error
    if parsed.utcoffset() is None:
        raise ValueError(_ERR_TIMESTAMP)
    return parsed.astimezone(timezone.utc)


def _optional_instant(value: object) -> datetime | None:
    return None if value is None else _instant(value)


def _enum(enum_type: type, value: object):
    if type(value) is not str:
        raise TypeError(_ERR_TEXT)
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(_ERR_ENUM) from error


def _uuid_text(value: object) -> str | None:
    if value is None:
        return None
    text = _text(value)
    try:
        canonical = str(UUID(text))
    except ValueError as error:
        raise ValueError(_ERR_UUID) from error
    if canonical != text:
        raise ValueError(_ERR_UUID)
    return text


def _agree(column: object, expected: object) -> None:
    if column != expected or type(column) is not type(expected):
        raise ValueError(_ERR_MISMATCH)


def _agree_instant(column: object, expected: datetime | None) -> None:
    parsed = _optional_instant(column)
    if parsed != expected:
        raise ValueError(_ERR_MISMATCH)


def _lifecycle(admission: PrivateSchedulerRunAdmission, state: object, state_version: object, claim_key: object, claimed_at: object, lease_expires_at: object,
               terminal_at: object, failure_code: object) -> PrivateSchedulerRunLifecycle:
    return PrivateSchedulerRunLifecycle(
        admission=admission, state=_enum(_STATE, state), state_version=_integer(state_version), claim_key=_optional_text(claim_key),
        claimed_at=_optional_instant(claimed_at), lease_expires_at=_optional_instant(lease_expires_at), terminal_at=_optional_instant(terminal_at),
        failure_code=_optional_text(failure_code))


def hydrate_private_scheduler_persisted_run(*, row: Mapping[str, object]) -> PrivateSchedulerPersistedRun:
    """Hydrate one current-run row: closed payload codec first, then every immutable column must agree, then the closed lifecycle shape rules."""
    data = _row(row, _RUN_COLUMNS)
    payload = data["admission_payload"]
    if type(payload) is not dict:
        raise TypeError(_ERR_ROW_TYPE)
    admission = hydrate_private_scheduler_run_admission(payload=payload)
    trigger = admission.trigger
    _agree(_text(data["run_idempotency_sha256"]), admission.run_idempotency_sha256)
    _agree(_text(data["admission_source"]), admission.source.value)
    _agree(_text(data["trigger_kind"]), trigger.trigger_kind.value)
    _agree(_text(data["work_kind"]), trigger.work_kind.value)
    _agree(_text(data["scope"]), trigger.scope.value)
    _agree(_uuid_text(data["owner_id"]), None if trigger.owner_id is None else str(trigger.owner_id))
    _agree(_uuid_text(data["portfolio_id"]), None if trigger.portfolio_id is None else str(trigger.portfolio_id))
    _agree_instant(data["scheduled_for"], trigger.scheduled_for)
    _agree(_optional_text(data["event_cause_kind"]), None if trigger.event_cause_kind is None else trigger.event_cause_kind.value)
    _agree(_optional_text(data["cause_key"]), trigger.cause_key)
    _agree_instant(data["cause_available_at"], None if trigger.cause_available_at is None else trigger.cause_available_at.astimezone(timezone.utc))
    _agree(_text(data["policy_key"]), trigger.policy_key)
    _agree(_integer(data["policy_revision"]), trigger.policy_revision)
    lifecycle = _lifecycle(admission, data["state"], data["state_version"], data["claim_key"], data["claimed_at"], data["lease_expires_at"],
                           data["terminal_at"], data["failure_code"])
    if data["created_at"] is None or data["updated_at"] is None:
        raise TypeError(_ERR_TIMESTAMP)
    return PrivateSchedulerPersistedRun(lifecycle=lifecycle, created_at=_instant(data["created_at"]), updated_at=_instant(data["updated_at"]))


def hydrate_private_scheduler_persisted_transition(*, row: Mapping[str, object], admission: PrivateSchedulerRunAdmission) -> PrivateSchedulerPersistedTransition:
    """Hydrate one history row against its closed admission; transition_at is kept separately and relation-checked per kind."""
    if type(admission) is not PrivateSchedulerRunAdmission:
        raise TypeError(_ERR_ADMISSION)
    data = _row(row, _HISTORY_COLUMNS)
    _agree(_text(data["run_idempotency_sha256"]), admission.run_idempotency_sha256)
    kind = _enum(_KIND, data["transition_kind"])
    after_version = _integer(data["after_state_version"])
    before_raw = data["before_state_version"]
    before_version = None if before_raw is None else _integer(before_raw)
    after = _lifecycle(admission, data["after_state"], after_version, data["claim_key"], data["claimed_at"], data["lease_expires_at"], data["terminal_at"],
                       data["failure_code"])
    if data["recorded_at"] is None:
        raise TypeError(_ERR_TIMESTAMP)
    return PrivateSchedulerPersistedTransition(kind=kind, before_state_version=before_version, transition_at=_optional_instant(data["transition_at"]),
                                               after=after, recorded_at=_instant(data["recorded_at"]))
