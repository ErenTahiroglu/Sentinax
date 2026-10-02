"""
backend/engine/private/scheduler_run_lifecycle.py
=================================================
Run lifecycle and claim-lease authority for one admitted scheduler run (Phase 24C2B).

This module defines the canonical lifecycle rules for ONE logical scheduler run admitted by Phase 24C2A: when it becomes claimable, which
state transitions are valid, what makes a claim valid, when a claim may be renewed or replaced, and which claim may mark the run succeeded or
failed. States are exactly READY, CLAIMED, SUCCEEDED and FAILED; the logical run identity stays `admission.run_idempotency_sha256` (no second
identity is stored) and the admission is retained by identity through every transition.

Optimistic version contract (a CAS contract, NOT distributed atomicity): pure object validation cannot prove that two workers did not read the same
state concurrently. The domain therefore defines `state_version` plus the caller's `expected_version` (which must equal the snapshot's version, no
hidden repair), the opaque `claim_key` and a half-open lease interval [claimed_at, lease_expires_at) (at exactly lease_expires_at the claim is
already expired). A valid transition only means: if storage applies it atomically against expected_version it is domain-valid. Phase 24D MUST
implement the atomic compare-and-swap (conceptually UPDATE ... WHERE run_idempotency_sha256 = ? AND state_version = expected_version, exactly one
row) and run-idempotency uniqueness; nothing here provides distributed locking and no valid CLAIM proves a worker won a race.

Snapshot shapes (`PrivateSchedulerRunLifecycle`, eight stored fields; stored times are canonical UTC only, no conversion):

    READY       state_version == 1 and no claim, lease, terminal time or failure code
    CLAIMED     state_version >= 2, claim key, claimed_at and lease_expires_at present, claimed_at < lease_expires_at, no terminal fields
    SUCCEEDED   state_version >= 3, claim fields present, terminal_at present, claimed_at <= terminal_at < lease_expires_at, no failure code
    FAILED      as SUCCEEDED plus a failure code (a strict lowercase identifier; never exception text, stack traces, PII or provider bodies)

Transitions (each returns a `PrivateSchedulerRunTransition` with kind, before and after; state_version increases by exactly one and no caller picks it):

    INITIALIZE                 admission -> READY version 1 (before None)
    CLAIM                      READY only; claimed_at must not precede the run not-before instant: scheduled_for for a scheduled trigger and
                               cause_available_at for an event-driven trigger (no tolerance, no clock), and claimed_at < lease_expires_at
    RENEW_CLAIM                CLAIMED only, same claim key, only while the old lease is active (claimed_at <= renewed_at < old lease expiry; at the
                               expiry instant it is too late), the new expiry strictly later; keeps the original claimed_at; renewed_at is not stored
    TAKE_OVER_EXPIRED_CLAIM    CLAIMED only, only at or after the old expiry (claimed_at >= old lease_expires_at, one microsecond early fails), with a
                               different claim key; this is lease recovery, not a retry of a FAILED run
    SUCCEED / FAIL             CLAIMED only, by the exact current claim key (case sensitive, no normalization), and only while the lease is active
                               (claimed_at <= terminal_at < lease_expires_at); a stale claimant cannot finish after a takeover

SUCCEEDED and FAILED are final: every transition function rejects them (no reclaim, renew, reset or retry; FAILED has no retry in v1 and there is no
attempt count, retry_after or backoff). The transition record preserves before / after provenance: its constructor re-validates the relation of
`after` to `before` through one relation check that the transition functions also use (admission identity by `is`, exactly one version step, state,
claim-key, lease and not-before rules); transient inputs such as renewed_at and expected_version are checked by the transition functions only.

Explicit non-goals: no ambient clock (every instant is an argument, so historical replay works), no generated claim key, UUID or randomness, no result
payload, no worker, dispatch, enqueue or execution, no persistence, uniqueness query or CAS implementation (no persistence here), no use of the legacy
`backend/infrastructure/job_queue.py` whose PENDING / RUNNING states are not authority.

Architectural Invariants:
    - Pure domain module: standard library plus the closed 24C2A admission type and the 24A trigger-kind enum. No clock, randomness, network, database
      or async. Exact concrete types: subclasses and raw strings are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_trigger import PrivateSchedulerTriggerKind

_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")

_ERR_ADMISSION = "admission must be an exact PrivateSchedulerRunAdmission instance"
_ERR_STATE = "state must be an exact PrivateSchedulerRunState instance"
_ERR_VERSION_TYPE = "state_version and expected_version must be exact int instances"
_ERR_VERSION_VALUE = "state_version must be at least 1"
_ERR_UTC_TYPE = "lifecycle instants must be exact datetime instances"
_ERR_UTC_VALUE = "lifecycle instants must be canonical UTC (tzinfo is timezone.utc)"
_ERR_CLAIM_KEY_TYPE = "claim_key must be an exact str instance"
_ERR_CLAIM_KEY_VALUE = "claim_key must be a printable, already trimmed string of 1 to 128 characters"
_ERR_FAILURE_TYPE = "failure_code must be an exact str instance"
_ERR_FAILURE_VALUE = "failure_code must be a canonical lowercase identifier of at most 128 characters"
_ERR_READY = "READY requires state_version 1 and no claim, lease, terminal time or failure code"
_ERR_CLAIMED = "CLAIMED requires state_version >= 2, claim key, claimed_at < lease_expires_at and no terminal fields"
_ERR_SUCCEEDED = "SUCCEEDED requires state_version >= 3, claim fields, claimed_at <= terminal_at < lease_expires_at and no failure code"
_ERR_FAILED = "FAILED requires state_version >= 3, claim fields, claimed_at <= terminal_at < lease_expires_at and a failure code"
_ERR_LIFECYCLE_TYPE = "lifecycle and before / after must be exact PrivateSchedulerRunLifecycle instances"
_ERR_KIND = "kind must be an exact PrivateSchedulerRunTransitionKind instance"
_ERR_EXPECTED_VERSION = "expected_version must equal the lifecycle state_version"
_ERR_STATE_PRECONDITION = "the lifecycle is not in the state this transition requires"
_ERR_CLAIM_MATCH = "claim_key must equal the current claim key exactly"
_ERR_NOT_BEFORE = "claimed_at must not precede the run not-before instant"
_ERR_LEASE_ACTIVE = "the instant must fall inside the active half-open lease [claimed_at, lease_expires_at)"
_ERR_LEASE_EXTEND = "a renewed lease must expire strictly later than the current lease"
_ERR_NOT_EXPIRED = "an expired-claim takeover requires claimed_at >= the current lease_expires_at"
_ERR_NEW_CLAIM = "a takeover requires a claim key different from the current claim key"
_ERR_RELATION = "the transition record does not follow the canonical lifecycle relation"


class PrivateSchedulerRunState(Enum):
    READY = "ready"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PrivateSchedulerRunTransitionKind(Enum):
    INITIALIZE = "initialize"
    CLAIM = "claim"
    RENEW_CLAIM = "renew_claim"
    TAKE_OVER_EXPIRED_CLAIM = "take_over_expired_claim"
    SUCCEED = "succeed"
    FAIL = "fail"


_STATE = PrivateSchedulerRunState
_KIND = PrivateSchedulerRunTransitionKind


def _check_utc(value: object) -> None:
    if type(value) is not datetime:
        raise TypeError(_ERR_UTC_TYPE)
    if value.tzinfo is not timezone.utc:
        raise ValueError(_ERR_UTC_VALUE)


def _check_claim_key(value: object) -> None:
    if type(value) is not str:
        raise TypeError(_ERR_CLAIM_KEY_TYPE)
    if not 1 <= len(value) <= 128 or value != value.strip() or not value.isprintable():
        raise ValueError(_ERR_CLAIM_KEY_VALUE)


def _not_before(admission: PrivateSchedulerRunAdmission) -> datetime:
    """Canonical not-before instant of the run, taken from the admitted trigger; pure, no clock."""
    trigger = admission.trigger
    if trigger.trigger_kind is PrivateSchedulerTriggerKind.SCHEDULED:
        return trigger.scheduled_for.astimezone(timezone.utc)
    return trigger.cause_available_at.astimezone(timezone.utc)


@dataclass(frozen=True)
class PrivateSchedulerRunLifecycle:
    """One immutable lifecycle snapshot of an admitted run: state, optimistic version, claim lease and terminal facts."""
    admission: PrivateSchedulerRunAdmission
    state: PrivateSchedulerRunState
    state_version: int
    claim_key: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    terminal_at: datetime | None
    failure_code: str | None

    def __post_init__(self) -> None:
        if type(self.admission) is not PrivateSchedulerRunAdmission:
            raise TypeError(_ERR_ADMISSION)
        if type(self.state) is not PrivateSchedulerRunState:
            raise TypeError(_ERR_STATE)
        if type(self.state_version) is not int:
            raise TypeError(_ERR_VERSION_TYPE)
        if self.state_version < 1:
            raise ValueError(_ERR_VERSION_VALUE)
        for instant in (self.claimed_at, self.lease_expires_at, self.terminal_at):
            if instant is not None:
                _check_utc(instant)
        if self.claim_key is not None:
            _check_claim_key(self.claim_key)
        if self.failure_code is not None:
            if type(self.failure_code) is not str:
                raise TypeError(_ERR_FAILURE_TYPE)
            if _FAILURE_CODE.fullmatch(self.failure_code) is None:
                raise ValueError(_ERR_FAILURE_VALUE)
        claim_fields = (self.claim_key is not None, self.claimed_at is not None, self.lease_expires_at is not None)
        if self.state is _STATE.READY:
            if self.state_version != 1 or any(claim_fields) or self.terminal_at is not None or self.failure_code is not None:
                raise ValueError(_ERR_READY)
        elif self.state is _STATE.CLAIMED:
            if (self.state_version < 2 or not all(claim_fields) or self.terminal_at is not None or self.failure_code is not None
                    or not self.claimed_at < self.lease_expires_at):
                raise ValueError(_ERR_CLAIMED)
        else:
            failed = self.state is _STATE.FAILED
            message = _ERR_FAILED if failed else _ERR_SUCCEEDED
            if self.state_version < 3 or not all(claim_fields) or self.terminal_at is None or (self.failure_code is not None) != failed:
                raise ValueError(message)
            if not self.claimed_at <= self.terminal_at < self.lease_expires_at:
                raise ValueError(message)


def _check_relation(kind: PrivateSchedulerRunTransitionKind, before: PrivateSchedulerRunLifecycle | None, after: PrivateSchedulerRunLifecycle) -> None:
    """The single relation policy between consecutive snapshots, shared by the transition functions and the record constructor."""
    if kind is _KIND.INITIALIZE:
        if before is not None or after.state is not _STATE.READY:
            raise ValueError(_ERR_RELATION)
        return
    if before is None or after.admission is not before.admission or after.state_version != before.state_version + 1:
        raise ValueError(_ERR_RELATION)
    if kind is _KIND.CLAIM:
        if before.state is not _STATE.READY or after.state is not _STATE.CLAIMED:
            raise ValueError(_ERR_RELATION)
        if after.claimed_at < _not_before(after.admission):
            raise ValueError(_ERR_NOT_BEFORE)
        return
    if before.state is not _STATE.CLAIMED:
        raise ValueError(_ERR_RELATION)
    if kind is _KIND.RENEW_CLAIM:
        if (after.state is not _STATE.CLAIMED or after.claim_key != before.claim_key or after.claimed_at != before.claimed_at
                or not after.lease_expires_at > before.lease_expires_at):
            raise ValueError(_ERR_RELATION)
    elif kind is _KIND.TAKE_OVER_EXPIRED_CLAIM:
        if after.state is not _STATE.CLAIMED or after.claim_key == before.claim_key or not after.claimed_at >= before.lease_expires_at:
            raise ValueError(_ERR_RELATION)
    else:
        wanted = _STATE.SUCCEEDED if kind is _KIND.SUCCEED else _STATE.FAILED
        if (after.state is not wanted or after.claim_key != before.claim_key or after.claimed_at != before.claimed_at
                or after.lease_expires_at != before.lease_expires_at):
            raise ValueError(_ERR_RELATION)


@dataclass(frozen=True)
class PrivateSchedulerRunTransition:
    """Immutable proof of which canonical transition produced a snapshot; retains before and after."""
    kind: PrivateSchedulerRunTransitionKind
    before: PrivateSchedulerRunLifecycle | None
    after: PrivateSchedulerRunLifecycle

    def __post_init__(self) -> None:
        if type(self.kind) is not PrivateSchedulerRunTransitionKind:
            raise TypeError(_ERR_KIND)
        if self.before is not None and type(self.before) is not PrivateSchedulerRunLifecycle:
            raise TypeError(_ERR_LIFECYCLE_TYPE)
        if type(self.after) is not PrivateSchedulerRunLifecycle:
            raise TypeError(_ERR_LIFECYCLE_TYPE)
        _check_relation(self.kind, self.before, self.after)


def initialize_private_scheduler_run_lifecycle(*, admission: PrivateSchedulerRunAdmission) -> PrivateSchedulerRunTransition:
    """INITIALIZE: the canonical READY snapshot at version 1; the admission is retained by identity."""
    after = PrivateSchedulerRunLifecycle(
        admission=admission, state=_STATE.READY, state_version=1, claim_key=None, claimed_at=None, lease_expires_at=None, terminal_at=None,
        failure_code=None,
    )
    return PrivateSchedulerRunTransition(kind=_KIND.INITIALIZE, before=None, after=after)


def _preconditions(lifecycle: object, expected_version: object, required: PrivateSchedulerRunState) -> None:
    if type(lifecycle) is not PrivateSchedulerRunLifecycle:
        raise TypeError(_ERR_LIFECYCLE_TYPE)
    if type(expected_version) is not int:
        raise TypeError(_ERR_VERSION_TYPE)
    if expected_version != lifecycle.state_version:
        raise ValueError(_ERR_EXPECTED_VERSION)
    if lifecycle.state is not required:
        raise ValueError(_ERR_STATE_PRECONDITION)


def _next(lifecycle: PrivateSchedulerRunLifecycle, state: PrivateSchedulerRunState, claim_key: str | None, claimed_at: datetime | None,
          lease_expires_at: datetime | None, terminal_at: datetime | None, failure_code: str | None) -> PrivateSchedulerRunLifecycle:
    return PrivateSchedulerRunLifecycle(
        admission=lifecycle.admission, state=state, state_version=lifecycle.state_version + 1, claim_key=claim_key, claimed_at=claimed_at,
        lease_expires_at=lease_expires_at, terminal_at=terminal_at, failure_code=failure_code,
    )


def claim_private_scheduler_run(
    *,
    lifecycle: PrivateSchedulerRunLifecycle,
    expected_version: int,
    claim_key: str,
    claimed_at: datetime,
    lease_expires_at: datetime,
) -> PrivateSchedulerRunTransition:
    """CLAIM a READY run no earlier than its not-before instant; the new snapshot carries version + 1."""
    _preconditions(lifecycle, expected_version, _STATE.READY)
    _check_claim_key(claim_key)
    _check_utc(claimed_at)
    _check_utc(lease_expires_at)
    after = _next(lifecycle, _STATE.CLAIMED, claim_key, claimed_at, lease_expires_at, None, None)
    return PrivateSchedulerRunTransition(kind=_KIND.CLAIM, before=lifecycle, after=after)


def renew_private_scheduler_run_claim(
    *,
    lifecycle: PrivateSchedulerRunLifecycle,
    expected_version: int,
    claim_key: str,
    renewed_at: datetime,
    lease_expires_at: datetime,
) -> PrivateSchedulerRunTransition:
    """RENEW the current claim while its lease is active; renewed_at is a transition input and is not stored."""
    _preconditions(lifecycle, expected_version, _STATE.CLAIMED)
    _check_claim_key(claim_key)
    _check_utc(renewed_at)
    _check_utc(lease_expires_at)
    if claim_key != lifecycle.claim_key:
        raise ValueError(_ERR_CLAIM_MATCH)
    if not lifecycle.claimed_at <= renewed_at < lifecycle.lease_expires_at:
        raise ValueError(_ERR_LEASE_ACTIVE)
    if not lease_expires_at > lifecycle.lease_expires_at:
        raise ValueError(_ERR_LEASE_EXTEND)
    after = _next(lifecycle, _STATE.CLAIMED, lifecycle.claim_key, lifecycle.claimed_at, lease_expires_at, None, None)
    return PrivateSchedulerRunTransition(kind=_KIND.RENEW_CLAIM, before=lifecycle, after=after)


def take_over_expired_private_scheduler_run_claim(
    *,
    lifecycle: PrivateSchedulerRunLifecycle,
    expected_version: int,
    claim_key: str,
    claimed_at: datetime,
    lease_expires_at: datetime,
) -> PrivateSchedulerRunTransition:
    """TAKE OVER a claim whose lease has expired (claimed_at >= old expiry) with a different claim key; not a retry of a terminal run."""
    _preconditions(lifecycle, expected_version, _STATE.CLAIMED)
    _check_claim_key(claim_key)
    _check_utc(claimed_at)
    _check_utc(lease_expires_at)
    if claim_key == lifecycle.claim_key:
        raise ValueError(_ERR_NEW_CLAIM)
    if claimed_at < lifecycle.lease_expires_at:
        raise ValueError(_ERR_NOT_EXPIRED)
    after = _next(lifecycle, _STATE.CLAIMED, claim_key, claimed_at, lease_expires_at, None, None)
    return PrivateSchedulerRunTransition(kind=_KIND.TAKE_OVER_EXPIRED_CLAIM, before=lifecycle, after=after)


def _terminal(
    kind: PrivateSchedulerRunTransitionKind,
    state: PrivateSchedulerRunState,
    lifecycle: object,
    expected_version: object,
    claim_key: object,
    terminal_at: object,
    failure_code: str | None,
) -> PrivateSchedulerRunTransition:
    _preconditions(lifecycle, expected_version, _STATE.CLAIMED)
    _check_claim_key(claim_key)
    _check_utc(terminal_at)
    if claim_key != lifecycle.claim_key:
        raise ValueError(_ERR_CLAIM_MATCH)
    if not lifecycle.claimed_at <= terminal_at < lifecycle.lease_expires_at:
        raise ValueError(_ERR_LEASE_ACTIVE)
    after = _next(lifecycle, state, lifecycle.claim_key, lifecycle.claimed_at, lifecycle.lease_expires_at, terminal_at, failure_code)
    return PrivateSchedulerRunTransition(kind=kind, before=lifecycle, after=after)


def succeed_private_scheduler_run(
    *,
    lifecycle: PrivateSchedulerRunLifecycle,
    expected_version: int,
    claim_key: str,
    terminal_at: datetime,
) -> PrivateSchedulerRunTransition:
    """SUCCEED the run by the exact current claim, only while its lease is active; SUCCEEDED is final."""
    return _terminal(_KIND.SUCCEED, _STATE.SUCCEEDED, lifecycle, expected_version, claim_key, terminal_at, None)


def fail_private_scheduler_run(
    *,
    lifecycle: PrivateSchedulerRunLifecycle,
    expected_version: int,
    claim_key: str,
    terminal_at: datetime,
    failure_code: str,
) -> PrivateSchedulerRunTransition:
    """FAIL the run by the exact current claim, only while its lease is active; FAILED is final and has no retry."""
    if type(failure_code) is not str:
        raise TypeError(_ERR_FAILURE_TYPE)
    return _terminal(_KIND.FAIL, _STATE.FAILED, lifecycle, expected_version, claim_key, terminal_at, failure_code)
