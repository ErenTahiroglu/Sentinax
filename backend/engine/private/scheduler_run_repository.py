"""
backend/engine/private/scheduler_run_repository.py
==================================================
RPC-only scheduler run repository (Phase 24D2B2). It connects the CLOSED scheduler domain (24C2A admission, 24C2B lifecycle) to migration 024 through the
24D2A admission codec and the 24D2B1 row transport. The repository is not a semantic authority: it predicts through the closed domain, performs one RPC
and reconciles the database answer.

Trust boundary: this is trusted backend / service-role infrastructure that handles SYSTEM and PORTFOLIO runs alike (owner isolation lives in the immutable
admission). It takes an injected PostgREST-compatible client, never builds one, never reads the environment, and is never exposed directly to
untrusted, user-supplied run hashes; any future API boundary applies its own authorization.

Write discipline (stronger than the grants of migration 024, which let service_role write tables directly): the two scheduler tables are only READ, through
`.table(...).select(<transport projection>).eq(...).limit(2).execute()`. Every write goes through exactly two RPCs, `initialize_private_scheduler_run` and
`apply_private_scheduler_run_transition`; there is no insert, update, upsert or delete and no dynamic RPC name.

Transitions: the closed C2B function predicts (and validates) first, so an invalid command never reaches SQL; then exactly one apply RPC is made
(no automatic retry, no reload-and-retry, no repaired expected_version, no conflict turned into success; the caller owns any retry policy, a 24D3 concern). The
RPC status is the authority: applied, not_found, version_conflict and transition_conflict are returned as they are, with no predicted or persisted
transition on the three non-applied statuses. An APPLIED answer is reconciled against the immutable exact-version history row (never the current row,
which may legitimately have advanced again): kind, before version, the closed after-snapshot (same admission object) and transition_at must equal the
prediction and the explicit command instant. That closes the renewal limitation of the 24D2B1 transport for rows applied here: prior lifecycle plus
renewed_at plus history are all available together.

Initialization: INITIALIZED must be READY / version 1; IDEMPOTENT_DUPLICATE may report an advanced state; both must have the durable INITIALIZE history row
at version 1 (verified against the supplied admission), otherwise RuntimeError. CONFLICT returns no transition and adopts nothing. Any malformed RPC
response is a RuntimeError. No clock, no randomness, no worker, no dispatch and no legacy queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_run_lifecycle import (
    PrivateSchedulerRunLifecycle,
    PrivateSchedulerRunState,
    PrivateSchedulerRunTransition,
    PrivateSchedulerRunTransitionKind,
    claim_private_scheduler_run,
    fail_private_scheduler_run,
    initialize_private_scheduler_run_lifecycle,
    renew_private_scheduler_run_claim,
    succeed_private_scheduler_run,
    take_over_expired_private_scheduler_run_claim,
)
from backend.engine.private.scheduler_run_persistence_codec import serialize_private_scheduler_run_admission
from backend.engine.private.scheduler_run_persistence_transport import (
    PRIVATE_SCHEDULER_RUN_SELECT,
    PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT,
    PrivateSchedulerPersistedRun,
    PrivateSchedulerPersistedTransition,
    hydrate_private_scheduler_persisted_run,
    hydrate_private_scheduler_persisted_transition,
)

_HASH = re.compile(r"[0-9a-f]{64}")
_RPC_KEYS = frozenset({"status", "run_idempotency_sha256", "state", "state_version"})
_STATE = PrivateSchedulerRunState
_KIND = PrivateSchedulerRunTransitionKind

_STATE_VERSION_FLOOR = {_STATE.READY: 1, _STATE.CLAIMED: 2, _STATE.SUCCEEDED: 3, _STATE.FAILED: 3}

_ERR_CLIENT = "client must not be None"
_ERR_ADMISSION = "admission must be an exact PrivateSchedulerRunAdmission instance"
_ERR_LIFECYCLE = "lifecycle must be an exact PrivateSchedulerRunLifecycle instance"
_ERR_HASH_TYPE = "run_idempotency_sha256 must be a str"
_ERR_HASH_VALUE = "run_idempotency_sha256 must be 64 lowercase hex characters"
_ERR_VERSION = "after_state_version must be an exact int >= 1"
_ERR_RESULT = "result fields are inconsistent with the status"
_ERR_RPC = "malformed RPC response"
_ERR_ROWS = "malformed table response"
_ERR_DUPLICATE_ROWS = "more than one row returned for a unique key"
_ERR_RECONCILE = "database result disagrees with the closed domain"
_ERR_HISTORY = "durable history row is missing or inconsistent"


class PrivateSchedulerRunInitializationStatus(Enum):
    INITIALIZED = "initialized"
    IDEMPOTENT_DUPLICATE = "idempotent_duplicate"
    CONFLICT = "conflict"


class PrivateSchedulerRunApplyStatus(Enum):
    APPLIED = "applied"
    NOT_FOUND = "not_found"
    VERSION_CONFLICT = "version_conflict"
    TRANSITION_CONFLICT = "transition_conflict"


_INIT_STATUS = PrivateSchedulerRunInitializationStatus
_APPLY_STATUS = PrivateSchedulerRunApplyStatus


@dataclass(frozen=True)
class PrivateSchedulerRunInitializationResult:
    status: PrivateSchedulerRunInitializationStatus
    run_idempotency_sha256: str
    state: PrivateSchedulerRunState
    state_version: int
    initialize_transition: PrivateSchedulerPersistedTransition | None

    def __post_init__(self) -> None:
        transition = self.initialize_transition
        if type(self.status) is not _INIT_STATUS or type(self.state) is not _STATE or type(self.state_version) is not int:
            raise TypeError(_ERR_RESULT)
        _check_hash(self.run_idempotency_sha256)
        if not _pair_valid(self.state, self.state_version):
            raise ValueError(_ERR_RESULT)
        if self.status is _INIT_STATUS.CONFLICT:
            if transition is not None:
                raise ValueError(_ERR_RESULT)
            return
        if (type(transition) is not PrivateSchedulerPersistedTransition or transition.kind is not _KIND.INITIALIZE or transition.after.state is not _STATE.READY
                or transition.after.state_version != 1 or transition.after.admission.run_idempotency_sha256 != self.run_idempotency_sha256):
            raise ValueError(_ERR_RESULT)


@dataclass(frozen=True)
class PrivateSchedulerRunApplyResult:
    status: PrivateSchedulerRunApplyStatus
    run_idempotency_sha256: str
    state: PrivateSchedulerRunState | None
    state_version: int | None
    predicted_transition: PrivateSchedulerRunTransition | None
    persisted_transition: PrivateSchedulerPersistedTransition | None

    def __post_init__(self) -> None:
        if type(self.status) is not _APPLY_STATUS:
            raise TypeError(_ERR_RESULT)
        _check_hash(self.run_idempotency_sha256)
        if self.state is not None or self.state_version is not None:
            if type(self.state) is not _STATE or type(self.state_version) is not int:
                raise TypeError(_ERR_RESULT)
            if not _pair_valid(self.state, self.state_version):
                raise ValueError(_ERR_RESULT)
        predicted, persisted = self.predicted_transition, self.persisted_transition
        if self.status is _APPLY_STATUS.APPLIED:
            if (self.state is None or self.state_version is None or type(predicted) is not PrivateSchedulerRunTransition
                    or type(persisted) is not PrivateSchedulerPersistedTransition):
                raise ValueError(_ERR_RESULT)
            if (persisted.kind is not predicted.kind or persisted.before_state_version != predicted.before.state_version or persisted.after != predicted.after
                    or persisted.after.admission is not predicted.after.admission):
                raise ValueError(_ERR_RESULT)
            return
        if predicted is not None or persisted is not None:
            raise ValueError(_ERR_RESULT)
        if (self.state is None) != (self.status is _APPLY_STATUS.NOT_FOUND) or (self.state_version is None) != (self.status is _APPLY_STATUS.NOT_FOUND):
            raise ValueError(_ERR_RESULT)


def _pair_valid(state: object, version: object) -> bool:
    """Closed lifecycle scalar relation: READY is v1, CLAIMED >= v2, SUCCEEDED and FAILED >= v3."""
    if type(state) is not _STATE or type(version) is not int:
        return False
    floor = _STATE_VERSION_FLOOR[state]
    return version == 1 if state is _STATE.READY else version >= floor


def _validate_state_version_pair(state: PrivateSchedulerRunState, state_version: int) -> None:
    if not _pair_valid(state, state_version):
        raise RuntimeError(_ERR_RPC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _check_hash(value: object) -> str:
    if type(value) is not str:
        raise TypeError(_ERR_HASH_TYPE)
    if _HASH.fullmatch(value) is None:
        raise ValueError(_ERR_HASH_VALUE)
    return value


def _rpc_item(response: Any) -> dict:
    data = response.data
    if type(data) is not list or len(data) != 1 or type(data[0]) is not dict or set(data[0]) != _RPC_KEYS:
        raise RuntimeError(_ERR_RPC)
    return data[0]


def _rpc_fields(item: dict, expected_hash: str) -> tuple[str, PrivateSchedulerRunState | None, int | None]:
    """Exact-typed (status text, state, version); the run hash must be canonical and equal the expected one."""
    status, run_hash, state, version = item["status"], item["run_idempotency_sha256"], item["state"], item["state_version"]
    if type(status) is not str or type(run_hash) is not str or _HASH.fullmatch(run_hash) is None or run_hash != expected_hash:
        raise RuntimeError(_ERR_RPC)
    if state is not None:
        if type(state) is not str:
            raise RuntimeError(_ERR_RPC)
        try:
            state = _STATE(state)
        except ValueError as error:
            raise RuntimeError(_ERR_RPC) from error
    if version is not None and (type(version) is not int or version < 1):
        raise RuntimeError(_ERR_RPC)
    if state is not None and version is not None:
        _validate_state_version_pair(state, version)
    return status, state, version


def _status(enum_type: type, text: str):
    try:
        return enum_type(text)
    except ValueError as error:
        raise RuntimeError(_ERR_RPC) from error


class PrivateSchedulerRunRepository:
    """Trusted backend scheduler repository: reads two tables, writes only through two RPCs, makes at most one write RPC per call."""

    def __init__(self, client: Any) -> None:
        if client is None:
            raise ValueError(_ERR_CLIENT)
        self._client = client

    # -- reads ----------------------------------------------------------------------------------------------------

    def _rows(self, query: Any) -> list[dict]:
        data = query.execute().data
        if type(data) is not list or any(type(row) is not dict for row in data):
            raise RuntimeError(_ERR_ROWS)
        if len(data) > 1:
            raise RuntimeError(_ERR_DUPLICATE_ROWS)
        return data

    def get_run(self, *, run_idempotency_sha256: str) -> PrivateSchedulerPersistedRun | None:
        """Read the current run by its canonical run hash; no row is None, more than one row fails closed."""
        run_hash = _check_hash(run_idempotency_sha256)
        rows = self._rows(self._client.table("private_scheduler_runs").select(PRIVATE_SCHEDULER_RUN_SELECT)
                          .eq("run_idempotency_sha256", run_hash).limit(2))
        return hydrate_private_scheduler_persisted_run(row=rows[0]) if rows else None

    def get_transition(self, *, admission: PrivateSchedulerRunAdmission, after_state_version: int) -> PrivateSchedulerPersistedTransition | None:
        """Read one immutable history version for the supplied admission."""
        if type(admission) is not PrivateSchedulerRunAdmission:
            raise TypeError(_ERR_ADMISSION)
        if type(after_state_version) is not int or after_state_version < 1:
            raise ValueError(_ERR_VERSION)
        rows = self._rows(self._client.table("private_scheduler_run_transitions").select(PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT)
                          .eq("run_idempotency_sha256", admission.run_idempotency_sha256).eq("after_state_version", after_state_version).limit(2))
        return hydrate_private_scheduler_persisted_transition(row=rows[0], admission=admission) if rows else None

    def _durable_history(self, admission: PrivateSchedulerRunAdmission, after_state_version: int) -> PrivateSchedulerPersistedTransition:
        try:
            persisted = self.get_transition(admission=admission, after_state_version=after_state_version)
        except (ValueError, TypeError) as error:
            raise RuntimeError(_ERR_HISTORY) from error
        if persisted is None:
            raise RuntimeError(_ERR_HISTORY)
        return persisted

    # -- initialization -------------------------------------------------------------------------------------------

    def initialize_run(self, *, admission: PrivateSchedulerRunAdmission) -> PrivateSchedulerRunInitializationResult:
        """Initialize the run through the atomic RPC and verify the durable INITIALIZE history unless the database reports a conflict."""
        if type(admission) is not PrivateSchedulerRunAdmission:
            raise TypeError(_ERR_ADMISSION)
        trigger = admission.trigger
        params = {
            "p_run_idempotency_sha256": admission.run_idempotency_sha256,
            "p_admission_source": admission.source.value,
            "p_trigger_kind": trigger.trigger_kind.value,
            "p_work_kind": trigger.work_kind.value,
            "p_scope": trigger.scope.value,
            "p_owner_id": None if trigger.owner_id is None else str(trigger.owner_id),
            "p_portfolio_id": None if trigger.portfolio_id is None else str(trigger.portfolio_id),
            "p_scheduled_for": _iso(trigger.scheduled_for),
            "p_event_cause_kind": None if trigger.event_cause_kind is None else trigger.event_cause_kind.value,
            "p_cause_key": trigger.cause_key,
            "p_cause_available_at": _iso(trigger.cause_available_at),
            "p_policy_key": trigger.policy_key,
            "p_policy_revision": trigger.policy_revision,
            "p_admission_payload": serialize_private_scheduler_run_admission(admission=admission),
        }
        item = _rpc_item(self._client.rpc("initialize_private_scheduler_run", params).execute())
        status_text, state, version = _rpc_fields(item, admission.run_idempotency_sha256)
        status = _status(_INIT_STATUS, status_text)
        if state is None or version is None:
            raise RuntimeError(_ERR_RPC)
        if status is _INIT_STATUS.CONFLICT:
            return PrivateSchedulerRunInitializationResult(status, admission.run_idempotency_sha256, state, version, None)
        if status is _INIT_STATUS.INITIALIZED and (state is not _STATE.READY or version != 1):
            raise RuntimeError(_ERR_RECONCILE)
        persisted = self._durable_history(admission, 1)
        expected = initialize_private_scheduler_run_lifecycle(admission=admission)
        if (persisted.kind is not _KIND.INITIALIZE or persisted.before_state_version is not None or persisted.transition_at is not None
                or persisted.after != expected.after):
            raise RuntimeError(_ERR_HISTORY)
        return PrivateSchedulerRunInitializationResult(status, admission.run_idempotency_sha256, state, version, persisted)

    # -- transitions ----------------------------------------------------------------------------------------------

    def claim_run(self, *, lifecycle: PrivateSchedulerRunLifecycle, expected_version: int, claim_key: str, claimed_at: datetime,
                  lease_expires_at: datetime) -> PrivateSchedulerRunApplyResult:
        _require_lifecycle(lifecycle)
        predicted = claim_private_scheduler_run(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, claimed_at=claimed_at,
                                                lease_expires_at=lease_expires_at)
        return self._apply(predicted, expected_version, claim_key, claimed_at, lease_expires_at, None)

    def renew_claim(self, *, lifecycle: PrivateSchedulerRunLifecycle, expected_version: int, claim_key: str, renewed_at: datetime,
                    lease_expires_at: datetime) -> PrivateSchedulerRunApplyResult:
        _require_lifecycle(lifecycle)
        predicted = renew_private_scheduler_run_claim(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, renewed_at=renewed_at,
                                                      lease_expires_at=lease_expires_at)
        return self._apply(predicted, expected_version, claim_key, renewed_at, lease_expires_at, None)

    def take_over_expired_claim(self, *, lifecycle: PrivateSchedulerRunLifecycle, expected_version: int, claim_key: str, claimed_at: datetime,
                                lease_expires_at: datetime) -> PrivateSchedulerRunApplyResult:
        _require_lifecycle(lifecycle)
        predicted = take_over_expired_private_scheduler_run_claim(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key,
                                                                  claimed_at=claimed_at, lease_expires_at=lease_expires_at)
        return self._apply(predicted, expected_version, claim_key, claimed_at, lease_expires_at, None)

    def succeed_run(self, *, lifecycle: PrivateSchedulerRunLifecycle, expected_version: int, claim_key: str,
                    terminal_at: datetime) -> PrivateSchedulerRunApplyResult:
        _require_lifecycle(lifecycle)
        predicted = succeed_private_scheduler_run(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, terminal_at=terminal_at)
        return self._apply(predicted, expected_version, claim_key, terminal_at, None, None)

    def fail_run(self, *, lifecycle: PrivateSchedulerRunLifecycle, expected_version: int, claim_key: str, terminal_at: datetime,
                 failure_code: str) -> PrivateSchedulerRunApplyResult:
        _require_lifecycle(lifecycle)
        predicted = fail_private_scheduler_run(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, terminal_at=terminal_at,
                                               failure_code=failure_code)
        return self._apply(predicted, expected_version, claim_key, terminal_at, None, failure_code)

    def _apply(self, predicted: PrivateSchedulerRunTransition, expected_version: int, claim_key: str, transition_at: datetime,
               lease_expires_at: datetime | None, failure_code: str | None) -> PrivateSchedulerRunApplyResult:
        """One apply RPC for an already domain-validated prediction, then reconcile; never retries."""
        admission = predicted.after.admission
        run_hash = admission.run_idempotency_sha256
        params = {
            "p_run_idempotency_sha256": run_hash,
            "p_expected_version": expected_version,
            "p_transition_kind": predicted.kind.value,
            "p_claim_key": claim_key,
            "p_transition_at": _iso(transition_at),
            "p_lease_expires_at": _iso(lease_expires_at),
            "p_failure_code": failure_code,
        }
        item = _rpc_item(self._client.rpc("apply_private_scheduler_run_transition", params).execute())
        status_text, state, version = _rpc_fields(item, run_hash)
        status = _status(_APPLY_STATUS, status_text)
        if status is _APPLY_STATUS.NOT_FOUND:
            if state is not None or version is not None:
                raise RuntimeError(_ERR_RPC)
            return PrivateSchedulerRunApplyResult(status, run_hash, None, None, None, None)
        if state is None or version is None:
            raise RuntimeError(_ERR_RPC)
        if status is _APPLY_STATUS.VERSION_CONFLICT or status is _APPLY_STATUS.TRANSITION_CONFLICT:
            if (version == expected_version) != (status is _APPLY_STATUS.TRANSITION_CONFLICT):
                raise RuntimeError(_ERR_RECONCILE)
            return PrivateSchedulerRunApplyResult(status, run_hash, state, version, None, None)
        after = predicted.after
        if state is not after.state or version != after.state_version:
            raise RuntimeError(_ERR_RECONCILE)
        persisted = self._durable_history(admission, after.state_version)
        if (persisted.kind is not predicted.kind or persisted.before_state_version != predicted.before.state_version or persisted.after != after
                or persisted.after.admission is not admission or persisted.transition_at != transition_at.astimezone(timezone.utc)):
            raise RuntimeError(_ERR_RECONCILE)
        return PrivateSchedulerRunApplyResult(status, run_hash, state, version, predicted, persisted)


def _require_lifecycle(lifecycle: object) -> None:
    if type(lifecycle) is not PrivateSchedulerRunLifecycle:
        raise TypeError(_ERR_LIFECYCLE)
