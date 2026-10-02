"""
backend/tests/test_scheduler_work_dispatcher.py
===============================================
Phase 24D3B: exact WorkKind -> injected handler routing. The dispatcher owns route selection only; the lifecycle stays with the 24D3A runtime and every
business workflow stays inside the injected handlers. Four recording handler doubles, a repository double and a deterministic clock; no database, no engine.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from backend.engine.private import scheduler_work_dispatcher as dispatcher_module
from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_runtime import (
    PrivateSchedulerDispatchRequest,
    PrivateSchedulerDispatchResult,
    PrivateSchedulerDispatchStatus,
    PrivateSchedulerRuntime,
    PrivateSchedulerRuntimeStatus,
)
from backend.engine.private.scheduler_trigger import PrivateSchedulerScope, PrivateSchedulerWorkKind
from backend.engine.private.scheduler_work_dispatcher import (
    PrivateSchedulerTypedWorkDispatcher,
    PrivateSchedulerWorkContext,
)
from backend.tests.invariants import static_guards as sg
from backend.tests.test_scheduler_run_persistence_transport import ADMISSIONS, event_admission, not_before, scheduled_admission, system_admission
from backend.tests.test_scheduler_runtime import FakeRepository, SequenceClock, ready_run

WK, SC, DS = PrivateSchedulerWorkKind, PrivateSchedulerScope, PrivateSchedulerDispatchStatus
ROUTES = {
    WK.SOURCE_DATA_REFRESH: "source_data_refresh_handler",
    WK.PORTFOLIO_ANALYSIS_REFRESH: "portfolio_analysis_refresh_handler",
    WK.PORTFOLIO_HEALTH_CHECK: "portfolio_health_check_handler",
    WK.GAME_CHANGER_REVIEW: "game_changer_review_handler",
}


class Handler:
    def __init__(self, behavior="success") -> None:
        self.contexts: list[PrivateSchedulerWorkContext] = []
        self.behavior = behavior
        self.result = None

    def handle(self, *, context):
        self.contexts.append(context)
        b = self.behavior
        if b == "success":
            self.result = PrivateSchedulerDispatchResult(DS.SUCCEEDED, None)
        elif b == "failed":
            self.result = PrivateSchedulerDispatchResult(DS.FAILED, "source_refresh_unavailable")
        elif b == "raise":
            raise RuntimeError("sensitive downstream detail")
        elif callable(b):
            self.result = b()
        else:
            raise AssertionError(b)
        return self.result


def handlers(**behaviors):
    return {name: Handler(behaviors.get(name, "success")) for name in ROUTES.values()}


def build(**behaviors):
    hs = handlers(**behaviors)
    return PrivateSchedulerTypedWorkDispatcher(**hs), hs


def request_for(admission, claim_key="claim-1"):
    return PrivateSchedulerDispatchRequest(admission=admission, claim_key=claim_key)


# --- contracts -----------------------------------------------------------------------------------------------------

def test_context_shape_and_validation() -> None:
    assert [f.name for f in dataclasses.fields(PrivateSchedulerWorkContext)] == ["admission", "claim_key", "work_idempotency_key"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(PrivateSchedulerWorkContext))
    assert PrivateSchedulerWorkContext.__dataclass_params__.frozen is True
    admission = scheduled_admission()
    h = admission.run_idempotency_sha256
    assert PrivateSchedulerWorkContext(admission, "claim-1", h).work_idempotency_key == h
    for args in ((None, "k", h), (object(), "k", h), (admission.trigger, "k", h), (admission, "", h), (admission, " k", h), (admission, "k" * 129, h),
                 (admission, "k\n", h), (admission, None, h), (admission, 1, h), (admission, "k", "b" * 64), (admission, "k", h.upper()), (admission, "k", None),
                 (admission, "k", admission)):
        with pytest.raises((TypeError, ValueError)):
            PrivateSchedulerWorkContext(*args)  # type: ignore[arg-type]


def test_constructor_requires_exactly_four_keyword_only_handlers() -> None:
    parameters = list(inspect.signature(PrivateSchedulerTypedWorkDispatcher.__init__).parameters.values())[1:]
    assert [p.name for p in parameters] == list(ROUTES.values())
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    good = handlers()
    PrivateSchedulerTypedWorkDispatcher(**good)
    for name in ROUTES.values():
        for bad in (None, object(), "x", 5):
            with pytest.raises(TypeError):
                PrivateSchedulerTypedWorkDispatcher(**{**good, name: bad})

        class NotCallable:
            handle = 5

        with pytest.raises(TypeError):
            PrivateSchedulerTypedWorkDispatcher(**{**good, name: NotCallable()})
    with pytest.raises(TypeError):
        PrivateSchedulerTypedWorkDispatcher(**{k: v for k, v in good.items() if k != "game_changer_review_handler"})  # type: ignore[call-arg]


def test_dispatch_signature_is_structurally_the_runtime_port() -> None:
    parameters = list(inspect.signature(PrivateSchedulerTypedWorkDispatcher.dispatch).parameters.values())[1:]
    assert [p.name for p in parameters] == ["request"] and parameters[0].kind is inspect.Parameter.KEYWORD_ONLY
    handler_params = list(inspect.signature(Handler.handle).parameters.values())[1:]
    assert [p.name for p in handler_params] == ["context"]


# --- routing -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_exact_route_one_handler_one_call(name: str) -> None:
    admission = ADMISSIONS[name]()
    dispatcher, hs = build()
    result = dispatcher.dispatch(request=request_for(admission))
    selected = ROUTES[admission.trigger.work_kind]
    for handler_name, handler in hs.items():
        assert len(handler.contexts) == (1 if handler_name == selected else 0), handler_name
    context = hs[selected].contexts[0]
    assert type(context) is PrivateSchedulerWorkContext and context.admission is admission
    assert context.claim_key == "claim-1" and context.work_idempotency_key == admission.run_idempotency_sha256
    assert result is hs[selected].result


def test_all_four_work_kinds_are_routed() -> None:
    assert {ADMISSIONS[n]().trigger.work_kind for n in ADMISSIONS} == set(WK) == set(ROUTES)


def test_scope_is_preserved_exactly() -> None:
    dispatcher, hs = build()
    system = system_admission()
    dispatcher.dispatch(request=request_for(system))
    context = hs["source_data_refresh_handler"].contexts[0]
    assert context.admission.trigger.scope is SC.SYSTEM and context.admission.trigger.owner_id is None and context.admission.trigger.portfolio_id is None
    for name in ("scheduled", "event", "calendar"):
        admission = ADMISSIONS[name]()
        dispatcher.dispatch(request=request_for(admission))
        trigger = hs[ROUTES[admission.trigger.work_kind]].contexts[-1].admission.trigger
        assert trigger.scope is SC.PORTFOLIO and trigger.owner_id == admission.trigger.owner_id and trigger.portfolio_id == admission.trigger.portfolio_id
        assert trigger.owner_id is not None and trigger.portfolio_id is not None


def test_work_identity_is_the_run_hash_and_is_distinct_from_the_claim_key() -> None:
    dispatcher, hs = build()
    admission = scheduled_admission()
    dispatcher.dispatch(request=request_for(admission, "claim-a"))
    dispatcher.dispatch(request=request_for(admission, "claim-b"))
    first, second = hs["portfolio_health_check_handler"].contexts
    assert first.work_idempotency_key == second.work_idempotency_key == admission.run_idempotency_sha256
    assert first.claim_key != second.claim_key
    other = event_admission()
    dispatcher.dispatch(request=request_for(other, "claim-a"))
    assert hs["game_changer_review_handler"].contexts[0].work_idempotency_key != first.work_idempotency_key


def test_invalid_requests_call_no_handler() -> None:
    dispatcher, hs = build()

    class Sub(PrivateSchedulerDispatchRequest):
        pass

    admission = scheduled_admission()
    for bad in (None, object(), admission, {"admission": admission}, Sub(admission, "claim-1")):
        with pytest.raises(TypeError):
            dispatcher.dispatch(request=bad)  # type: ignore[arg-type]
    assert all(h.contexts == [] for h in hs.values())


def test_forged_work_scope_pair_is_rejected_before_any_handler() -> None:
    dispatcher, hs = build()
    base = scheduled_admission()                                   # PORTFOLIO_HEALTH_CHECK, PORTFOLIO scope
    system = system_admission()                                    # SOURCE_DATA_REFRESH, SYSTEM scope

    def forge(admission: PrivateSchedulerRunAdmission, **trigger_changes):
        trigger = object.__new__(type(admission.trigger))
        for field in dataclasses.fields(admission.trigger):
            object.__setattr__(trigger, field.name, trigger_changes.get(field.name, getattr(admission.trigger, field.name)))
        forged = object.__new__(PrivateSchedulerRunAdmission)
        for field in dataclasses.fields(admission):
            object.__setattr__(forged, field.name, trigger if field.name == "trigger" else getattr(admission, field.name))
        return forged

    forged_cases = [
        forge(base, work_kind=WK.SOURCE_DATA_REFRESH),                         # source refresh with PORTFOLIO scope
        forge(system, work_kind=WK.PORTFOLIO_HEALTH_CHECK),                    # portfolio work with SYSTEM scope
        forge(base, owner_id=None),                                            # portfolio work without an owner
        forge(base, portfolio_id=None),
        forge(system, owner_id=base.trigger.owner_id),                         # SYSTEM with an owner
        forge(system, portfolio_id=base.trigger.portfolio_id),
        forge(base, owner_id="not-a-uuid"),
    ]
    for forged in forged_cases:
        with pytest.raises(ValueError):
            dispatcher.dispatch(request=request_for(forged))
    assert all(h.contexts == [] for h in hs.values())


# --- results -------------------------------------------------------------------------------------------------------

def test_controlled_failure_result_is_returned_unchanged() -> None:
    dispatcher, hs = build(source_data_refresh_handler="failed")
    result = dispatcher.dispatch(request=request_for(system_admission()))
    assert result is hs["source_data_refresh_handler"].result
    assert result == PrivateSchedulerDispatchResult(DS.FAILED, "source_refresh_unavailable")
    assert sum(len(h.contexts) for h in hs.values()) == 1                           # no fallback to another handler


class _Sub(PrivateSchedulerDispatchResult):
    pass


@pytest.mark.parametrize("bad", [lambda: None, lambda: {"status": "succeeded"}, lambda: object(), lambda: "ok", lambda: _Sub(DS.SUCCEEDED, None)])
def test_wrong_handler_result_type_raises_type_error_after_one_call(bad) -> None:
    dispatcher, hs = build(portfolio_analysis_refresh_handler=bad)
    with pytest.raises(TypeError):
        dispatcher.dispatch(request=request_for(ADMISSIONS["calendar"]()))
    assert len(hs["portfolio_analysis_refresh_handler"].contexts) == 1 and sum(len(h.contexts) for h in hs.values()) == 1


def test_handler_exception_propagates_unchanged() -> None:
    dispatcher, hs = build(portfolio_health_check_handler="raise")
    with pytest.raises(RuntimeError) as info:
        dispatcher.dispatch(request=request_for(scheduled_admission()))
    assert str(info.value) == "sensitive downstream detail" and sum(len(h.contexts) for h in hs.values()) == 1


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_base_exceptions_propagate(exception) -> None:
    def boom():
        raise exception()

    dispatcher, hs = build(portfolio_health_check_handler=boom)
    with pytest.raises(exception):
        dispatcher.dispatch(request=request_for(scheduled_admission()))


# --- D3A composition -----------------------------------------------------------------------------------------------

def compose(**behaviors):
    hs = handlers(**behaviors)
    dispatcher = PrivateSchedulerTypedWorkDispatcher(**hs)
    admission = scheduled_admission()
    nb = not_before(admission)
    repo = FakeRepository()
    runtime = PrivateSchedulerRuntime(repository=repo, dispatcher=dispatcher, clock=SequenceClock(nb, nb + timedelta(minutes=5)))
    result = runtime.execute_ready_run(persisted_run=ready_run(admission), claim_key="claim-1", lease_expires_at=nb + timedelta(minutes=10))
    return result, repo, hs


def test_runtime_composition_success() -> None:
    result, repo, hs = compose()
    assert result.status is PrivateSchedulerRuntimeStatus.SUCCEEDED and repo.names() == ["claim_run", "succeed_run"]
    assert [len(h.contexts) for h in hs.values()] == [0, 0, 1, 0] and len(hs["portfolio_health_check_handler"].contexts) == 1


def test_runtime_composition_controlled_failure() -> None:
    result, repo, hs = compose(portfolio_health_check_handler="failed")
    assert result.status is PrivateSchedulerRuntimeStatus.FAILED and repo.names() == ["claim_run", "fail_run"]
    assert repo.calls[1][1]["failure_code"] == "source_refresh_unavailable"


def test_runtime_composition_exception_is_sanitized_by_the_runtime() -> None:
    result, repo, hs = compose(portfolio_health_check_handler="raise")
    assert result.status is PrivateSchedulerRuntimeStatus.FAILED and repo.calls[1][1]["failure_code"] == "dispatcher_exception"
    assert "sensitive downstream detail" not in repr(repo.calls) and "sensitive" not in repr(result)


def test_runtime_composition_wrong_handler_type_is_dispatcher_exception() -> None:
    result, repo, hs = compose(portfolio_health_check_handler=lambda: None)
    assert result.status is PrivateSchedulerRuntimeStatus.FAILED and repo.calls[1][1]["failure_code"] == "dispatcher_exception"


# --- source-level invariants ---------------------------------------------------------------------------------------

_SOURCE = Path(dispatcher_module.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_work_dispatcher.py"


def _imports() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def _identifiers() -> set[str]:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {n.name for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_imports_are_the_runtime_trigger_and_admission_surfaces_only() -> None:
    sentinax = {m for m in _imports() if m.startswith("backend")}
    assert sentinax <= {"backend.engine.private.scheduler_runtime", "backend.engine.private.scheduler_trigger", "backend.engine.private.scheduler_run_admission"}
    assert "backend.engine.private.scheduler_runtime" in sentinax and "backend.engine.private.scheduler_trigger" in sentinax


def test_no_provider_allocation_game_changer_or_trading_binding() -> None:
    imported = _imports()
    for forbidden in ("orchestrator", "provider_contract", "providers", "game_changer_gate", "allocation_rebalance", "allocation_rebalance_policy",
                      "allocation_cvar_optimizer", "allocation_hrp", "allocation_risk_parity", "portfolio", "scheduler_run_repository", "supabase", "infrastructure"):
        assert not any(forbidden == part or forbidden in part.split(".") for module in imported for part in module.split(".")), forbidden
    names = _identifiers()
    assert not names & {"build_game_changer_decision_gate", "FetchContext", "SourcePolicy", "ProviderOrchestrator", "DataProviderContract", "sell", "trade",
                        "liquidate", "rebalance", "quarantine", "target_weight", "order", "execute", "rpc", "table"}
    assert not [n for n in names if n.startswith(("allocation_", "build_game_changer"))]


def test_no_clock_randomness_async_loop_queue_or_exception_swallowing() -> None:
    names = _identifiers()
    assert not names & {"now", "utcnow", "today", "time", "sleep", "monotonic", "uuid4", "random", "secrets", "urandom", "getenv", "environ", "retry"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.For, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try, ast.ExceptHandler))]
    for forbidden in ("asyncio", "redis", "celery", "fastapi", "random", "secrets", "os", "importlib"):
        assert forbidden not in {part for module in _imports() for part in module.split(".")}
    calls = {n.func.id for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"getattr", "setattr", "__import__", "eval", "exec"} - {"getattr"}
    string_methods = {n.func.attr for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not string_methods & {"lower", "upper", "startswith", "endswith"}


def test_module_is_not_registered_pure_and_documents_boundaries() -> None:
    assert _REL not in sg.PURE_MANIFEST
    doc = dispatcher_module.__doc__ or ""
    for needle in ("route selection", "work_idempotency_key", "claim key", "no business", "TypeError", "dispatcher_exception"):
        assert needle in doc, needle
