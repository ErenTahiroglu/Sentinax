"""
backend/tests/test_scheduler_runtime.py
=======================================
Phase 24D3A: lease-safe single-run scheduler runtime. Claim first (the database CAS chooses the winner), dispatch at most once through an injected port,
then one terminal transition. Deterministic doubles for the repository, dispatcher and clock; no database, no queue, no loop, no retry.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.engine.private import scheduler_runtime as runtime_module
from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_run_lifecycle import (
    PrivateSchedulerRunState,
    PrivateSchedulerRunTransitionKind,
    claim_private_scheduler_run,
    fail_private_scheduler_run,
    initialize_private_scheduler_run_lifecycle,
    succeed_private_scheduler_run,
)
from backend.engine.private.scheduler_run_persistence_transport import PrivateSchedulerPersistedRun, PrivateSchedulerPersistedTransition
from backend.engine.private.scheduler_run_repository import PrivateSchedulerRunApplyResult, PrivateSchedulerRunApplyStatus
from backend.engine.private.scheduler_runtime import (
    PrivateSchedulerDispatchRequest,
    PrivateSchedulerDispatchResult,
    PrivateSchedulerDispatchStatus,
    PrivateSchedulerRuntime,
    PrivateSchedulerRuntimeResult,
    PrivateSchedulerRuntimeStatus,
)
from backend.engine.private.scheduler_trigger import PrivateSchedulerScope, PrivateSchedulerWorkKind
from backend.tests.invariants import static_guards as sg
from backend.tests.test_scheduler_run_persistence_transport import ADMISSIONS, event_admission, not_before, scheduled_admission

UTC = timezone.utc
KIND, STATE = PrivateSchedulerRunTransitionKind, PrivateSchedulerRunState
RS, DS, AS = PrivateSchedulerRuntimeStatus, PrivateSchedulerDispatchStatus, PrivateSchedulerRunApplyStatus
CLAIM_KEY = "claim-runtime-1"
RECORDED = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)


def ready_run(admission) -> PrivateSchedulerPersistedRun:
    return PrivateSchedulerPersistedRun(lifecycle=initialize_private_scheduler_run_lifecycle(admission=admission).after, created_at=RECORDED, updated_at=RECORDED)


def applied(transition, at) -> PrivateSchedulerRunApplyResult:
    after = transition.after
    persisted = PrivateSchedulerPersistedTransition(kind=transition.kind, before_state_version=transition.before.state_version, transition_at=at, after=after,
                                                    recorded_at=RECORDED)
    return PrivateSchedulerRunApplyResult(AS.APPLIED, after.admission.run_idempotency_sha256, after.state, after.state_version, transition, persisted)


def non_applied(status: AS, admission) -> PrivateSchedulerRunApplyResult:
    state, version = {AS.NOT_FOUND: (None, None), AS.VERSION_CONFLICT: (STATE.CLAIMED, 5), AS.TRANSITION_CONFLICT: (STATE.READY, 1)}[status]
    return PrivateSchedulerRunApplyResult(status, admission.run_idempotency_sha256, state, version, None, None)


class FakeRepository:
    """Only claim_run / succeed_run / fail_run exist; each runs the CLOSED domain first (as the real repository does) and then 'one RPC'."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rpc_calls: list[str] = []
        self.claim_status = AS.APPLIED
        self.terminal_status = AS.APPLIED
        self.claim_override = None
        self.terminal_override = None
        self.error: Exception | None = None

    def claim_run(self, *, lifecycle, expected_version, claim_key, claimed_at, lease_expires_at):
        self.calls.append(("claim_run", dict(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, claimed_at=claimed_at,
                                             lease_expires_at=lease_expires_at)))
        predicted = claim_private_scheduler_run(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, claimed_at=claimed_at,
                                                lease_expires_at=lease_expires_at)
        self.rpc_calls.append("claim")
        if self.claim_override is not None:
            return self.claim_override(predicted, claimed_at)
        return applied(predicted, claimed_at) if self.claim_status is AS.APPLIED else non_applied(self.claim_status, lifecycle.admission)

    def succeed_run(self, *, lifecycle, expected_version, claim_key, terminal_at):
        self.calls.append(("succeed_run", dict(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, terminal_at=terminal_at)))
        predicted = succeed_private_scheduler_run(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, terminal_at=terminal_at)
        return self._terminal(predicted, terminal_at, lifecycle)

    def fail_run(self, *, lifecycle, expected_version, claim_key, terminal_at, failure_code):
        self.calls.append(("fail_run", dict(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, terminal_at=terminal_at,
                                            failure_code=failure_code)))
        predicted = fail_private_scheduler_run(lifecycle=lifecycle, expected_version=expected_version, claim_key=claim_key, terminal_at=terminal_at,
                                               failure_code=failure_code)
        return self._terminal(predicted, terminal_at, lifecycle)

    def _terminal(self, predicted, at, lifecycle):
        if self.error is not None:
            raise self.error
        self.rpc_calls.append("terminal")
        if self.terminal_override is not None:
            return self.terminal_override(predicted, at)
        return applied(predicted, at) if self.terminal_status is AS.APPLIED else non_applied(self.terminal_status, lifecycle.admission)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeDispatcher:
    def __init__(self, behavior="success") -> None:
        self.requests: list[PrivateSchedulerDispatchRequest] = []
        self.behavior = behavior

    def dispatch(self, *, request):
        self.requests.append(request)
        behavior = self.behavior
        if behavior == "success":
            return PrivateSchedulerDispatchResult(DS.SUCCEEDED, None)
        if behavior == "failed":
            return PrivateSchedulerDispatchResult(DS.FAILED, "provider_timeout")
        if behavior == "raise":
            raise RuntimeError("secret provider details")
        if callable(behavior):
            return behavior()
        raise AssertionError(behavior)


class SequenceClock:
    def __init__(self, *values) -> None:
        self.values = list(values)
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.values.pop(0)


def build(admission=None, behavior="success", minutes=(0, 5)):
    admission = admission or scheduled_admission()
    nb = not_before(admission)
    repo, dispatcher, clock = FakeRepository(), FakeDispatcher(behavior), SequenceClock(*[nb + timedelta(minutes=m) for m in minutes])
    runtime = PrivateSchedulerRuntime(repository=repo, dispatcher=dispatcher, clock=clock)
    return admission, nb, repo, dispatcher, clock, runtime


def run(runtime, admission, nb, lease_minutes=10, claim_key=CLAIM_KEY):
    return runtime.execute_ready_run(persisted_run=ready_run(admission), claim_key=claim_key, lease_expires_at=nb + timedelta(minutes=lease_minutes))


# --- contracts -----------------------------------------------------------------------------------------------------

def test_enums_and_dataclass_shapes_are_exact() -> None:
    assert {m.name: m.value for m in PrivateSchedulerDispatchStatus} == {"SUCCEEDED": "succeeded", "FAILED": "failed"}
    assert {m.name: m.value for m in PrivateSchedulerRuntimeStatus} == {
        "SUCCEEDED": "succeeded", "FAILED": "failed", "CLAIM_NOT_FOUND": "claim_not_found", "CLAIM_VERSION_CONFLICT": "claim_version_conflict",
        "CLAIM_TRANSITION_CONFLICT": "claim_transition_conflict", "TERMINAL_NOT_FOUND": "terminal_not_found",
        "TERMINAL_VERSION_CONFLICT": "terminal_version_conflict", "TERMINAL_TRANSITION_CONFLICT": "terminal_transition_conflict"}
    shapes = {PrivateSchedulerDispatchResult: ["status", "failure_code"], PrivateSchedulerDispatchRequest: ["admission", "claim_key"],
              PrivateSchedulerRuntimeResult: ["status", "run_idempotency_sha256", "claim_result", "dispatch_result", "terminal_result"]}
    for cls, fields in shapes.items():
        assert [f.name for f in dataclasses.fields(cls)] == fields
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(cls))
        assert cls.__dataclass_params__.frozen is True


def test_dispatch_result_and_request_validation() -> None:
    ok = PrivateSchedulerDispatchResult(DS.FAILED, "provider_timeout")
    assert ok.failure_code == "provider_timeout" and PrivateSchedulerDispatchResult(DS.SUCCEEDED, None).failure_code is None
    for args in ((DS.SUCCEEDED, "x"), (DS.FAILED, None), (DS.FAILED, ""), (DS.FAILED, "Bad Code"), (DS.FAILED, "-x"), (DS.FAILED, "a" * 129), (DS.FAILED, "Trace\nback"),
                 (DS.FAILED, 5), ("failed", "x"), (None, None)):
        with pytest.raises((TypeError, ValueError)):
            PrivateSchedulerDispatchResult(*args)
    assert PrivateSchedulerDispatchResult(DS.FAILED, "a" * 128).failure_code == "a" * 128
    admission = scheduled_admission()
    assert PrivateSchedulerDispatchRequest(admission, "claim-1").claim_key == "claim-1"
    for admission_arg, key in ((None, "k"), (object(), "k"), (admission.trigger, "k"), (admission, ""), (admission, " k"), (admission, "k" * 129), (admission, "k\n"),
                               (admission, None), (admission, 1)):
        with pytest.raises((TypeError, ValueError)):
            PrivateSchedulerDispatchRequest(admission_arg, key)  # type: ignore[arg-type]


def test_constructor_dependencies_and_signatures() -> None:
    parameters = list(inspect.signature(PrivateSchedulerRuntime.__init__).parameters.values())[1:]
    assert [p.name for p in parameters] == ["repository", "dispatcher", "clock"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    execute = list(inspect.signature(PrivateSchedulerRuntime.execute_ready_run).parameters.values())[1:]
    assert [p.name for p in execute] == ["persisted_run", "claim_key", "lease_expires_at"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in execute)
    good = dict(repository=FakeRepository(), dispatcher=FakeDispatcher(), clock=SequenceClock())
    PrivateSchedulerRuntime(**good)
    for name in good:
        for bad in (None, 5, "x"):
            with pytest.raises(TypeError):
                PrivateSchedulerRuntime(**{**good, name: bad})
    with pytest.raises(TypeError):
        PrivateSchedulerRuntime(**{**good, "repository": object()})
    with pytest.raises(TypeError):
        PrivateSchedulerRuntime(**{**good, "dispatcher": object()})


# --- success / failure flows ---------------------------------------------------------------------------------------

def test_success_flow_claims_dispatches_once_then_succeeds() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    result = run(runtime, admission, nb)
    assert result.status is RS.SUCCEEDED and result.run_idempotency_sha256 == admission.run_idempotency_sha256
    assert repo.names() == ["claim_run", "succeed_run"] and clock.count == 2
    assert len(dispatcher.requests) == 1
    assert result.claim_result.status is AS.APPLIED and result.terminal_result.status is AS.APPLIED
    assert result.dispatch_result == PrivateSchedulerDispatchResult(DS.SUCCEEDED, None)
    assert result.terminal_result.predicted_transition.kind is KIND.SUCCEED
    claim_args, succeed_args = repo.calls[0][1], repo.calls[1][1]
    assert claim_args["lifecycle"] is ready_run(admission).lifecycle or claim_args["lifecycle"] == ready_run(admission).lifecycle
    assert claim_args["expected_version"] == 1 and claim_args["claim_key"] == CLAIM_KEY and claim_args["claimed_at"] == nb
    assert claim_args["lease_expires_at"] == nb + timedelta(minutes=10)
    claimed = result.claim_result.persisted_transition.after
    assert succeed_args["lifecycle"] is claimed and succeed_args["expected_version"] == claimed.state_version == 2
    assert succeed_args["claim_key"] == CLAIM_KEY and succeed_args["terminal_at"] == nb + timedelta(minutes=5)
    request = dispatcher.requests[0]
    assert type(request) is PrivateSchedulerDispatchRequest and request.claim_key == CLAIM_KEY and request.admission is claimed.admission


def test_controlled_dispatcher_failure_uses_the_exact_failure_code() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build(behavior="failed")
    result = run(runtime, admission, nb)
    assert result.status is RS.FAILED and repo.names() == ["claim_run", "fail_run"] and clock.count == 2 and len(dispatcher.requests) == 1
    assert repo.calls[1][1]["failure_code"] == "provider_timeout" and result.terminal_result.predicted_transition.kind is KIND.FAIL
    assert result.dispatch_result == PrivateSchedulerDispatchResult(DS.FAILED, "provider_timeout")


def test_dispatcher_exception_is_sanitized() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build(behavior="raise")
    result = run(runtime, admission, nb)
    assert result.status is RS.FAILED and repo.names() == ["claim_run", "fail_run"] and clock.count == 2
    assert repo.calls[1][1]["failure_code"] == "dispatcher_exception"
    assert result.dispatch_result == PrivateSchedulerDispatchResult(DS.FAILED, "dispatcher_exception")
    assert "secret provider details" not in repr(repo.calls) and "secret" not in repr(result)


class _Sub(PrivateSchedulerDispatchResult):
    pass


@pytest.mark.parametrize("bad", [lambda: None, lambda: object(), lambda: {"status": "succeeded"}, lambda: "succeeded",
                                 lambda: _Sub(DS.SUCCEEDED, None)])
def test_dispatcher_contract_errors_fail_with_the_canonical_code(bad) -> None:
    admission, nb, repo, dispatcher, clock, runtime = build(behavior=bad)
    result = run(runtime, admission, nb)
    assert result.status is RS.FAILED and repo.names() == ["claim_run", "fail_run"] and repo.calls[1][1]["failure_code"] == "dispatcher_contract_error"
    assert result.dispatch_result == PrivateSchedulerDispatchResult(DS.FAILED, "dispatcher_contract_error")


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_base_exceptions_are_not_caught(exception) -> None:
    def boom():
        raise exception()

    admission, nb, repo, dispatcher, clock, runtime = build(behavior=boom)
    with pytest.raises(exception):
        run(runtime, admission, nb)
    assert repo.names() == ["claim_run"]


# --- claim conflicts -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [(AS.NOT_FOUND, RS.CLAIM_NOT_FOUND), (AS.VERSION_CONFLICT, RS.CLAIM_VERSION_CONFLICT),
                                             (AS.TRANSITION_CONFLICT, RS.CLAIM_TRANSITION_CONFLICT)])
def test_claim_conflicts_never_dispatch(status, expected) -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    repo.claim_status = status
    result = run(runtime, admission, nb)
    assert result.status is expected and result.dispatch_result is None and result.terminal_result is None and result.claim_result.status is status
    assert dispatcher.requests == [] and repo.names() == ["claim_run"] and clock.count == 1


@pytest.mark.parametrize("status,expected", [(AS.NOT_FOUND, RS.TERMINAL_NOT_FOUND), (AS.VERSION_CONFLICT, RS.TERMINAL_VERSION_CONFLICT),
                                             (AS.TRANSITION_CONFLICT, RS.TERMINAL_TRANSITION_CONFLICT)])
@pytest.mark.parametrize("behavior,terminal", [("success", "succeed_run"), ("failed", "fail_run")])
def test_terminal_conflicts_preserve_the_dispatch_outcome_and_do_not_retry(status, expected, behavior, terminal) -> None:
    admission, nb, repo, dispatcher, clock, runtime = build(behavior=behavior)
    repo.terminal_status = status
    result = run(runtime, admission, nb)
    assert result.status is expected and result.terminal_result.status is status and result.dispatch_result is not None
    assert result.dispatch_result.status is (DS.SUCCEEDED if behavior == "success" else DS.FAILED)
    assert repo.names() == ["claim_run", terminal] and len(dispatcher.requests) == 1 and clock.count == 2


def test_repository_exceptions_propagate_without_hidden_recovery() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    repo.error = ConnectionError("db down")
    with pytest.raises(ConnectionError):
        run(runtime, admission, nb)
    assert repo.names() == ["claim_run", "succeed_run"] and len(dispatcher.requests) == 1

    class ClaimBoom(FakeRepository):
        def claim_run(self, **kwargs):
            self.calls.append(("claim_run", kwargs))
            raise ConnectionError("down")

    claim_repo, dispatcher = ClaimBoom(), FakeDispatcher()
    runtime = PrivateSchedulerRuntime(repository=claim_repo, dispatcher=dispatcher, clock=SequenceClock(nb))
    with pytest.raises(ConnectionError):
        run(runtime, admission, nb)
    assert dispatcher.requests == [] and claim_repo.names() == ["claim_run"]


def test_terminal_clock_at_or_after_lease_expiry_propagates_the_domain_rejection() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build(minutes=(0, 10))
    with pytest.raises(ValueError):
        run(runtime, admission, nb, lease_minutes=10)
    assert len(dispatcher.requests) == 1 and repo.names() == ["claim_run", "succeed_run"] and repo.rpc_calls == ["claim"] and clock.count == 2
    admission, nb, repo, dispatcher, clock, runtime = build(behavior="failed", minutes=(0, 11))
    with pytest.raises(ValueError):
        run(runtime, admission, nb, lease_minutes=10)
    assert repo.rpc_calls == ["claim"] and len(dispatcher.requests) == 1


# --- input validation ----------------------------------------------------------------------------------------------

def test_only_ready_persisted_runs_are_accepted_before_any_side_effect() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    initial = ready_run(admission).lifecycle
    from backend.engine.private.scheduler_run_lifecycle import claim_private_scheduler_run as claim, succeed_private_scheduler_run as succeed
    claimed = claim(lifecycle=initial, expected_version=1, claim_key="c", claimed_at=nb, lease_expires_at=nb + timedelta(minutes=10)).after
    done = succeed(lifecycle=claimed, expected_version=2, claim_key="c", terminal_at=nb + timedelta(minutes=1)).after
    failed = fail_private_scheduler_run(lifecycle=claimed, expected_version=2, claim_key="c", terminal_at=nb + timedelta(minutes=1), failure_code="x").after
    for lifecycle in (claimed, done, failed):
        persisted = PrivateSchedulerPersistedRun(lifecycle=lifecycle, created_at=RECORDED, updated_at=RECORDED)
        with pytest.raises(ValueError):
            runtime.execute_ready_run(persisted_run=persisted, claim_key=CLAIM_KEY, lease_expires_at=nb + timedelta(minutes=10))

    class Sub(PrivateSchedulerPersistedRun):
        pass

    sub = Sub(lifecycle=initial, created_at=RECORDED, updated_at=RECORDED)
    for bad in (None, object(), initial, sub):
        with pytest.raises(TypeError):
            runtime.execute_ready_run(persisted_run=bad, claim_key=CLAIM_KEY, lease_expires_at=nb + timedelta(minutes=10))
    ready = ready_run(admission)
    for key in ("", " k", "k" * 129, "k\n"):
        with pytest.raises(ValueError):
            runtime.execute_ready_run(persisted_run=ready, claim_key=key, lease_expires_at=nb + timedelta(minutes=10))
    for key in (None, 1, b"k"):
        with pytest.raises(TypeError):
            runtime.execute_ready_run(persisted_run=ready, claim_key=key, lease_expires_at=nb + timedelta(minutes=10))  # type: ignore[arg-type]
    for lease in (None, "x", datetime(2026, 10, 2, 7, 5), nb.astimezone(timezone(timedelta(hours=3))) + timedelta(minutes=10)):
        with pytest.raises((TypeError, ValueError)):
            runtime.execute_ready_run(persisted_run=ready, claim_key=CLAIM_KEY, lease_expires_at=lease)  # type: ignore[arg-type]
    assert clock.count == 0 and repo.calls == [] and dispatcher.requests == []


# --- clock authority -----------------------------------------------------------------------------------------------

def test_clock_output_is_validated_and_canonicalized_to_utc() -> None:
    admission = scheduled_admission()
    nb = not_before(admission)
    plus3 = timezone(timedelta(hours=3))
    repo, dispatcher = FakeRepository(), FakeDispatcher()
    runtime = PrivateSchedulerRuntime(repository=repo, dispatcher=dispatcher, clock=SequenceClock(nb.astimezone(plus3), (nb + timedelta(minutes=5)).astimezone(plus3)))
    run(runtime, admission, nb)
    assert repo.calls[0][1]["claimed_at"] == nb and repo.calls[0][1]["claimed_at"].tzinfo is UTC
    assert repo.calls[1][1]["terminal_at"].tzinfo is UTC

    class Sub(datetime):
        pass

    for bad in (None, "2026-10-02T07:00:00Z", 1.0, datetime(2026, 10, 2, 7, 0), Sub(2026, 10, 2, 7, 0, tzinfo=UTC)):
        repo, dispatcher = FakeRepository(), FakeDispatcher()
        runtime = PrivateSchedulerRuntime(repository=repo, dispatcher=dispatcher, clock=SequenceClock(bad))
        with pytest.raises((TypeError, ValueError)):
            run(runtime, admission, nb)
        assert repo.calls == [] and dispatcher.requests == []
    repo, dispatcher = FakeRepository(), FakeDispatcher()
    runtime = PrivateSchedulerRuntime(repository=repo, dispatcher=dispatcher, clock=SequenceClock(nb, datetime(2026, 10, 2, 7, 5)))
    with pytest.raises((TypeError, ValueError)):
        run(runtime, admission, nb)
    assert repo.names() == ["claim_run"] and len(dispatcher.requests) == 1                          # invalid terminal clock: no terminal call


# --- scope / work-kind neutrality ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_every_work_kind_and_scope_reaches_the_dispatcher_unchanged(name: str) -> None:
    admission = ADMISSIONS[name]()
    _, nb, repo, dispatcher, clock, runtime = build(admission)
    result = run(runtime, admission, nb)
    request = dispatcher.requests[0]
    assert request.admission is result.claim_result.persisted_transition.after.admission and request.admission == admission
    trigger = request.admission.trigger
    assert trigger.work_kind is admission.trigger.work_kind and trigger.scope is admission.trigger.scope
    assert (trigger.owner_id, trigger.portfolio_id) == (admission.trigger.owner_id, admission.trigger.portfolio_id)
    if trigger.scope is PrivateSchedulerScope.SYSTEM:
        assert trigger.work_kind is PrivateSchedulerWorkKind.SOURCE_DATA_REFRESH and trigger.owner_id is None and trigger.portfolio_id is None
    else:
        assert trigger.owner_id is not None and trigger.portfolio_id is not None
    assert result.status is RS.SUCCEEDED and repo.names() == ["claim_run", "succeed_run"]


def test_all_four_work_kinds_are_covered() -> None:
    assert {ADMISSIONS[n]().trigger.work_kind for n in ADMISSIONS} == set(PrivateSchedulerWorkKind)


# --- continuity ----------------------------------------------------------------------------------------------------

def test_foreign_claim_admission_is_a_runtime_error_before_dispatch() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    other = event_admission()

    def foreign(predicted, at):
        other_predicted = claim_private_scheduler_run(lifecycle=initialize_private_scheduler_run_lifecycle(admission=other).after, expected_version=1,
                                                      claim_key=CLAIM_KEY, claimed_at=not_before(other), lease_expires_at=not_before(other) + timedelta(minutes=10))
        return applied(other_predicted, not_before(other))

    repo.claim_override = foreign
    with pytest.raises(RuntimeError):
        run(runtime, admission, nb)
    assert dispatcher.requests == [] and repo.names() == ["claim_run"]
    admission, nb, repo, dispatcher, clock, runtime = build()
    repo.claim_override = lambda predicted, at: "not-a-result"
    with pytest.raises(RuntimeError):
        run(runtime, admission, nb)
    assert dispatcher.requests == []


def test_foreign_terminal_admission_is_a_runtime_error() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    other = event_admission()

    def foreign(predicted, at):
        o_initial = initialize_private_scheduler_run_lifecycle(admission=other).after
        o_nb = not_before(other)
        o_claimed = claim_private_scheduler_run(lifecycle=o_initial, expected_version=1, claim_key=CLAIM_KEY, claimed_at=o_nb, lease_expires_at=o_nb + timedelta(minutes=10)).after
        return applied(succeed_private_scheduler_run(lifecycle=o_claimed, expected_version=2, claim_key=CLAIM_KEY, terminal_at=o_nb + timedelta(minutes=1)),
                       o_nb + timedelta(minutes=1))

    repo.terminal_override = foreign
    with pytest.raises(RuntimeError):
        run(runtime, admission, nb)
    assert len(dispatcher.requests) == 1 and repo.names() == ["claim_run", "succeed_run"]


# --- result forge resistance ---------------------------------------------------------------------------------------

def test_runtime_result_rejects_forged_combinations() -> None:
    admission, nb, repo, dispatcher, clock, runtime = build()
    good = run(runtime, admission, nb)
    h, claim, terminal = good.run_idempotency_sha256, good.claim_result, good.terminal_result
    ok_dispatch = PrivateSchedulerDispatchResult(DS.SUCCEEDED, None)
    bad_dispatch = PrivateSchedulerDispatchResult(DS.FAILED, "provider_timeout")
    conflict = non_applied(AS.VERSION_CONFLICT, admission)
    not_found = non_applied(AS.NOT_FOUND, admission)
    transition_conflict = non_applied(AS.TRANSITION_CONFLICT, admission)
    other = event_admission()
    foreign_claim = applied(claim_private_scheduler_run(lifecycle=initialize_private_scheduler_run_lifecycle(admission=other).after, expected_version=1,
                                                         claim_key=CLAIM_KEY, claimed_at=not_before(other), lease_expires_at=not_before(other) + timedelta(minutes=10)),
                            not_before(other))
    _, nb2, repo2, d2, c2, rt2 = build(behavior="failed")
    fail_run_result = run(rt2, admission, nb2)
    Result = PrivateSchedulerRuntimeResult
    assert Result(RS.SUCCEEDED, h, claim, ok_dispatch, terminal).status is RS.SUCCEEDED
    assert Result(RS.FAILED, h, fail_run_result.claim_result, bad_dispatch, fail_run_result.terminal_result).status is RS.FAILED
    assert Result(RS.CLAIM_VERSION_CONFLICT, h, conflict, None, None).dispatch_result is None
    assert Result(RS.TERMINAL_NOT_FOUND, h, claim, ok_dispatch, not_found).status is RS.TERMINAL_NOT_FOUND
    forged = [
        (RS.SUCCEEDED, h, conflict, ok_dispatch, terminal),                    # SUCCEEDED without an applied claim
        (RS.SUCCEEDED, h, claim, bad_dispatch, terminal),                      # SUCCEEDED with failed dispatch
        (RS.SUCCEEDED, h, claim, None, terminal),
        (RS.SUCCEEDED, h, claim, ok_dispatch, None),
        (RS.SUCCEEDED, h, claim, ok_dispatch, fail_run_result.terminal_result),  # wrong terminal kind
        (RS.SUCCEEDED, h, claim, ok_dispatch, not_found),
        (RS.FAILED, h, claim, ok_dispatch, fail_run_result.terminal_result),   # FAILED with success dispatch
        (RS.FAILED, h, claim, bad_dispatch, terminal),                         # FAILED with a SUCCEED terminal
        (RS.CLAIM_VERSION_CONFLICT, h, conflict, ok_dispatch, None),           # claim conflict with dispatch present
        (RS.CLAIM_VERSION_CONFLICT, h, conflict, None, terminal),
        (RS.CLAIM_VERSION_CONFLICT, h, not_found, None, None),                 # status/claim mismatch
        (RS.CLAIM_NOT_FOUND, h, claim, None, None),
        (RS.CLAIM_TRANSITION_CONFLICT, h, conflict, None, None),
        (RS.TERMINAL_NOT_FOUND, h, claim, None, not_found),                    # terminal conflict without dispatch
        (RS.TERMINAL_NOT_FOUND, h, claim, ok_dispatch, conflict),              # wrong mapped terminal status
        (RS.TERMINAL_VERSION_CONFLICT, h, claim, ok_dispatch, None),
        (RS.TERMINAL_VERSION_CONFLICT, h, conflict, ok_dispatch, conflict),    # claim not applied
        (RS.TERMINAL_TRANSITION_CONFLICT, h, claim, ok_dispatch, terminal),
        (RS.SUCCEEDED, "b" * 64, claim, ok_dispatch, terminal),                # wrong run hash
        (RS.SUCCEEDED, h.upper(), claim, ok_dispatch, terminal),
        (RS.SUCCEEDED, h[:63], claim, ok_dispatch, terminal),
        (RS.SUCCEEDED, None, claim, ok_dispatch, terminal),
        (RS.SUCCEEDED, h, foreign_claim, ok_dispatch, terminal),               # foreign claim result
        ("succeeded", h, claim, ok_dispatch, terminal),
        (RS.SUCCEEDED, h, "claim", ok_dispatch, terminal),
        (RS.SUCCEEDED, h, claim, "dispatch", terminal),
    ]
    for args in forged:
        with pytest.raises((TypeError, ValueError)):
            Result(*args)
    assert Result(RS.TERMINAL_TRANSITION_CONFLICT, h, claim, ok_dispatch, transition_conflict).terminal_result is transition_conflict


# --- source-level invariants ---------------------------------------------------------------------------------------

_SOURCE = Path(runtime_module.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_runtime.py"


def test_repository_is_used_only_through_claim_succeed_fail() -> None:
    used = {n.func.attr for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "_repository"}
    assert used == {"claim_run", "succeed_run", "fail_run"}
    attrs = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not attrs & {"renew_claim", "take_over_expired_claim", "get_run", "get_transition", "initialize_run", "rpc", "table"}


def test_no_loop_async_retry_clock_randomness_or_queue() -> None:
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.For, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.AsyncWith))]
    names = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not names & {"now", "utcnow", "today", "sleep", "monotonic", "uuid4", "uuid1", "random", "secrets", "environ", "getenv", "retry", "backoff"}
    imported: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for forbidden in ("scheduler", "job_queue", "redis", "fastapi", "httpx", "requests", "asyncio", "random", "uuid", "secrets", "os", "supabase", "celery",
                      "orchestrator", "game_changer_gate", "portfolio", "providers", "allocation"):
        assert not any(forbidden == part or forbidden in part.split(".") for part in imported), forbidden
    assert not [m for m in imported if m.startswith("backend.infrastructure")]
    excepts = [n for n in ast.walk(_TREE) if isinstance(n, ast.ExceptHandler)]
    assert len(excepts) == 1 and isinstance(excepts[0].type, ast.Name) and excepts[0].type.id == "Exception"
    assert "BaseException" not in _SOURCE


def test_runtime_failure_codes_are_exactly_two_and_module_is_not_pure() -> None:
    assert _REL not in sg.PURE_MANIFEST
    assert runtime_module.DISPATCHER_EXCEPTION_FAILURE_CODE == "dispatcher_exception"
    assert runtime_module.DISPATCHER_CONTRACT_ERROR_FAILURE_CODE == "dispatcher_contract_error"


def test_documents_the_runtime_boundaries() -> None:
    doc = runtime_module.__doc__ or ""
    for needle in ("claim before dispatch", "at most one dispatch", "no automatic retry", "exactly-once", "lease", "review intent", "no renewal", "no takeover"):
        assert needle in doc, needle
