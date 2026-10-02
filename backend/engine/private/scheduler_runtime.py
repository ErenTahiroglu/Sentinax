"""
backend/engine/private/scheduler_runtime.py
===========================================
Lease-safe runtime executor for exactly ONE scheduler run (Phase 24D3A). The repository and a typed work dispatcher are injected; the runtime owns only the
lifecycle procedure: claim before dispatch, then at most one dispatch, then one terminal transition.

Flow for a persisted READY run: clock() once, `repository.claim_run` once; the database CAS (migration 024 through the repository) picks the winner and only
an APPLIED claim may dispatch. A claim conflict (not found, version conflict, transition conflict) returns immediately: no dispatch, no terminal call and
no automatic retry. After an applied claim the dispatcher is called exactly once with the persisted claimed admission and the claim key (no work-kind branching: the
dispatcher owns the mapping). Then clock() once more and one terminal call: `succeed_run` after a SUCCEEDED dispatch, `fail_run` otherwise. A dispatcher
exception is mapped to the canonical code `dispatcher_exception` and a non-exact return value to `dispatcher_contract_error`; no exception class, message,
repr or stack trace is persisted. Only `Exception` is caught (never KeyboardInterrupt, SystemExit or other base exceptions). A terminal non-applied result (not found / version conflict /
transition conflict) is reported as its own runtime status and is never converted into a success or failure of the work: the work may have run.

Authorities are explicit: the claim key, the lease expiry and the (injected, deterministic) clock are caller inputs; the runtime generates no identity,
invents no lease duration and reads no ambient clock. Clock output must be an exact aware datetime and is canonicalized to UTC.

Limits: at most one dispatch per invocation after winning the persisted claim; exactly-once external side effects are NOT guaranteed (a dispatched run whose
terminal persistence conflicts is never dispatched again automatically; side-effect idempotency belongs to the concrete handlers). There is no renewal, no takeover,
no heartbeat: this is for work expected to fit inside one lease; if the terminal clock is at or after the lease expiry the closed domain rejects locally
and that error propagates (it signals an insufficient lease policy, it is not turned into FAILED). Repository exceptions propagate with no hidden recovery.
GAME_CHANGER_REVIEW is a review intent only, never an automatic trade, sell, rebalance or quarantine; concrete handlers (24D3B) own that. No queue, no loop,
no async, no legacy scheduler.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_run_lifecycle import PrivateSchedulerRunState, PrivateSchedulerRunTransitionKind
from backend.engine.private.scheduler_run_persistence_transport import PrivateSchedulerPersistedRun
from backend.engine.private.scheduler_run_repository import (
    PrivateSchedulerRunApplyResult,
    PrivateSchedulerRunApplyStatus,
    PrivateSchedulerRunRepository,
)

DISPATCHER_EXCEPTION_FAILURE_CODE = "dispatcher_exception"
DISPATCHER_CONTRACT_ERROR_FAILURE_CODE = "dispatcher_contract_error"

_FAILURE_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_AS = PrivateSchedulerRunApplyStatus
_KIND = PrivateSchedulerRunTransitionKind

_ERR_STATUS = "status must be an exact enum member"
_ERR_FAILURE_TYPE = "failure_code must be an exact str or None"
_ERR_FAILURE_VALUE = "failure_code must match the closed failure-code grammar and only a FAILED dispatch carries one"
_ERR_ADMISSION = "admission must be an exact PrivateSchedulerRunAdmission instance"
_ERR_CLAIM_KEY_TYPE = "claim_key must be an exact str"
_ERR_CLAIM_KEY_VALUE = "claim_key must be 1..128 printable characters without surrounding whitespace"
_ERR_DEPENDENCY = "runtime dependency is missing a required callable"
_ERR_PERSISTED_RUN = "persisted_run must be an exact PrivateSchedulerPersistedRun instance"
_ERR_NOT_READY = "only READY runs can be executed"
_ERR_LEASE_TYPE = "lease_expires_at must be an exact datetime"
_ERR_LEASE_VALUE = "lease_expires_at must be canonical UTC"
_ERR_CLOCK_TYPE = "clock must return an exact datetime"
_ERR_CLOCK_VALUE = "clock must return an aware datetime"
_ERR_RESULT = "runtime result fields are inconsistent with the status"
_ERR_REPOSITORY_RESULT = "repository returned an unexpected result"
_ERR_CONTINUITY = "repository result does not continue the supplied run"


class PrivateSchedulerDispatchStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PrivateSchedulerRuntimeStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CLAIM_NOT_FOUND = "claim_not_found"
    CLAIM_VERSION_CONFLICT = "claim_version_conflict"
    CLAIM_TRANSITION_CONFLICT = "claim_transition_conflict"
    TERMINAL_NOT_FOUND = "terminal_not_found"
    TERMINAL_VERSION_CONFLICT = "terminal_version_conflict"
    TERMINAL_TRANSITION_CONFLICT = "terminal_transition_conflict"


_DS = PrivateSchedulerDispatchStatus
_RS = PrivateSchedulerRuntimeStatus
_CLAIM_STATUS = {_AS.NOT_FOUND: _RS.CLAIM_NOT_FOUND, _AS.VERSION_CONFLICT: _RS.CLAIM_VERSION_CONFLICT, _AS.TRANSITION_CONFLICT: _RS.CLAIM_TRANSITION_CONFLICT}
_TERMINAL_STATUS = {_AS.NOT_FOUND: _RS.TERMINAL_NOT_FOUND, _AS.VERSION_CONFLICT: _RS.TERMINAL_VERSION_CONFLICT,
                    _AS.TRANSITION_CONFLICT: _RS.TERMINAL_TRANSITION_CONFLICT}


def _check_failure_code(value: object) -> None:
    if type(value) is not str:
        raise TypeError(_ERR_FAILURE_TYPE)
    if _FAILURE_CODE.fullmatch(value) is None:
        raise ValueError(_ERR_FAILURE_VALUE)


def _check_claim_key(value: object) -> None:
    if type(value) is not str:
        raise TypeError(_ERR_CLAIM_KEY_TYPE)
    if not 1 <= len(value) <= 128 or value != value.strip() or not value.isprintable():
        raise ValueError(_ERR_CLAIM_KEY_VALUE)


@dataclass(frozen=True)
class PrivateSchedulerDispatchResult:
    """Outcome reported by a work dispatcher: a status and, only for FAILED, a canonical failure code (no payload, no exception text)."""
    status: PrivateSchedulerDispatchStatus
    failure_code: str | None

    def __post_init__(self) -> None:
        if type(self.status) is not _DS:
            raise TypeError(_ERR_STATUS)
        if self.status is _DS.SUCCEEDED:
            if self.failure_code is not None:
                raise ValueError(_ERR_FAILURE_VALUE)
        else:
            _check_failure_code(self.failure_code)


@dataclass(frozen=True)
class PrivateSchedulerDispatchRequest:
    """What a dispatcher receives: the claimed admission (owner, portfolio and work kind are reachable through its trigger) and the claim key."""
    admission: PrivateSchedulerRunAdmission
    claim_key: str

    def __post_init__(self) -> None:
        if type(self.admission) is not PrivateSchedulerRunAdmission:
            raise TypeError(_ERR_ADMISSION)
        _check_claim_key(self.claim_key)


class PrivateSchedulerWorkDispatcher(Protocol):
    """Synchronous typed work-dispatch port; concrete handlers (24D3B) implement it."""

    def dispatch(self, *, request: PrivateSchedulerDispatchRequest) -> PrivateSchedulerDispatchResult:
        ...


@dataclass(frozen=True)
class PrivateSchedulerRuntimeResult:
    """One run attempt: the runtime status, the claim result, the dispatch outcome (if any) and the terminal repository result (if any)."""
    status: PrivateSchedulerRuntimeStatus
    run_idempotency_sha256: str
    claim_result: PrivateSchedulerRunApplyResult
    dispatch_result: PrivateSchedulerDispatchResult | None
    terminal_result: PrivateSchedulerRunApplyResult | None

    def __post_init__(self) -> None:
        if type(self.status) is not _RS:
            raise TypeError(_ERR_STATUS)
        if type(self.run_idempotency_sha256) is not str or _HASH.fullmatch(self.run_idempotency_sha256) is None:
            raise ValueError(_ERR_RESULT)
        claim, dispatch, terminal = self.claim_result, self.dispatch_result, self.terminal_result
        if type(claim) is not PrivateSchedulerRunApplyResult or (terminal is not None and type(terminal) is not PrivateSchedulerRunApplyResult):
            raise TypeError(_ERR_RESULT)
        if dispatch is not None and type(dispatch) is not PrivateSchedulerDispatchResult:
            raise TypeError(_ERR_RESULT)
        if claim.run_idempotency_sha256 != self.run_idempotency_sha256 or (terminal is not None and terminal.run_idempotency_sha256 != self.run_idempotency_sha256):
            raise ValueError(_ERR_RESULT)
        status = self.status
        claim_conflicts = {value: key for key, value in _CLAIM_STATUS.items()}
        terminal_conflicts = {value: key for key, value in _TERMINAL_STATUS.items()}
        if status in claim_conflicts:
            if claim.status is not claim_conflicts[status] or dispatch is not None or terminal is not None:
                raise ValueError(_ERR_RESULT)
            return
        if claim.status is not _AS.APPLIED or dispatch is None or terminal is None:
            raise ValueError(_ERR_RESULT)
        if status in terminal_conflicts:
            if terminal.status is not terminal_conflicts[status]:
                raise ValueError(_ERR_RESULT)
            return
        succeeded = status is _RS.SUCCEEDED
        if terminal.status is not _AS.APPLIED or dispatch.status is not (_DS.SUCCEEDED if succeeded else _DS.FAILED):
            raise ValueError(_ERR_RESULT)
        if terminal.predicted_transition.kind is not (_KIND.SUCCEED if succeeded else _KIND.FAIL):
            raise ValueError(_ERR_RESULT)
        if not succeeded and terminal.persisted_transition.after.failure_code != dispatch.failure_code:
            raise ValueError(_ERR_RESULT)


class PrivateSchedulerRuntime:
    """Executes one READY run through an injected repository, dispatcher and clock."""

    def __init__(self, *, repository: PrivateSchedulerRunRepository, dispatcher: PrivateSchedulerWorkDispatcher, clock: Callable[[], datetime]) -> None:
        if not all(callable(getattr(repository, name, None)) for name in ("claim_run", "succeed_run", "fail_run")):
            raise TypeError(_ERR_DEPENDENCY)
        if not callable(getattr(dispatcher, "dispatch", None)) or not callable(clock):
            raise TypeError(_ERR_DEPENDENCY)
        self._repository = repository
        self._dispatcher = dispatcher
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime:
            raise TypeError(_ERR_CLOCK_TYPE)
        if value.utcoffset() is None:
            raise ValueError(_ERR_CLOCK_VALUE)
        return value.astimezone(timezone.utc)

    def execute_ready_run(self, *, persisted_run: PrivateSchedulerPersistedRun, claim_key: str, lease_expires_at: datetime) -> PrivateSchedulerRuntimeResult:
        """Claim, dispatch at most once, then persist exactly one terminal transition for a READY run."""
        if type(persisted_run) is not PrivateSchedulerPersistedRun:
            raise TypeError(_ERR_PERSISTED_RUN)
        lifecycle = persisted_run.lifecycle
        if lifecycle.state is not PrivateSchedulerRunState.READY:
            raise ValueError(_ERR_NOT_READY)
        _check_claim_key(claim_key)
        if type(lease_expires_at) is not datetime:
            raise TypeError(_ERR_LEASE_TYPE)
        if lease_expires_at.tzinfo is not timezone.utc:
            raise ValueError(_ERR_LEASE_VALUE)
        run_hash = lifecycle.admission.run_idempotency_sha256

        claim_result = self._repository.claim_run(lifecycle=lifecycle, expected_version=lifecycle.state_version, claim_key=claim_key, claimed_at=self._now(),
                                                  lease_expires_at=lease_expires_at)
        if type(claim_result) is not PrivateSchedulerRunApplyResult or claim_result.run_idempotency_sha256 != run_hash:
            raise RuntimeError(_ERR_REPOSITORY_RESULT)
        if claim_result.status is not _AS.APPLIED:
            return PrivateSchedulerRuntimeResult(_CLAIM_STATUS[claim_result.status], run_hash, claim_result, None, None)
        claimed = claim_result.persisted_transition.after
        if claimed.admission is not lifecycle.admission or claim_result.persisted_transition.kind is not _KIND.CLAIM:
            raise RuntimeError(_ERR_CONTINUITY)

        request = PrivateSchedulerDispatchRequest(admission=claimed.admission, claim_key=claim_key)
        try:
            outcome = self._dispatcher.dispatch(request=request)
        except Exception:
            outcome = PrivateSchedulerDispatchResult(_DS.FAILED, DISPATCHER_EXCEPTION_FAILURE_CODE)
        if type(outcome) is not PrivateSchedulerDispatchResult:
            outcome = PrivateSchedulerDispatchResult(_DS.FAILED, DISPATCHER_CONTRACT_ERROR_FAILURE_CODE)

        terminal_at = self._now()
        if outcome.status is _DS.SUCCEEDED:
            terminal = self._repository.succeed_run(lifecycle=claimed, expected_version=claimed.state_version, claim_key=claim_key, terminal_at=terminal_at)
        else:
            terminal = self._repository.fail_run(lifecycle=claimed, expected_version=claimed.state_version, claim_key=claim_key, terminal_at=terminal_at,
                                                 failure_code=outcome.failure_code)
        if type(terminal) is not PrivateSchedulerRunApplyResult or terminal.run_idempotency_sha256 != run_hash:
            raise RuntimeError(_ERR_REPOSITORY_RESULT)
        if terminal.status is not _AS.APPLIED:
            return PrivateSchedulerRuntimeResult(_TERMINAL_STATUS[terminal.status], run_hash, claim_result, outcome, terminal)
        if terminal.persisted_transition.after.admission is not claimed.admission:
            raise RuntimeError(_ERR_CONTINUITY)
        status = _RS.SUCCEEDED if outcome.status is _DS.SUCCEEDED else _RS.FAILED
        return PrivateSchedulerRuntimeResult(status, run_hash, claim_result, outcome, terminal)
