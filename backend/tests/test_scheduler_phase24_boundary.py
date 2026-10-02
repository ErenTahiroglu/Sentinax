"""
backend/tests/test_scheduler_phase24_boundary.py
================================================
Phase 24 closure boundary: scheduler-wide architecture guard. Verifies the exact module inventory, legacy isolation, the closed WorkKind/scope contract, the
single run identity chain, claim-before-dispatch across the runtime and dispatcher, the RPC-only write authority, the absence of retry, the Gitleaks gate files
and the permanent explicit CI membership of the whole Phase 24 suite. Test-only: no production module, no database.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private.scheduler_run_admission import admit_private_scheduler_calendar_applicability, admit_private_scheduler_scheduled_occurrence
from backend.engine.private.scheduler_run_lifecycle import PrivateSchedulerRunState, initialize_private_scheduler_run_lifecycle
from backend.engine.private.scheduler_run_repository import PrivateSchedulerRunApplyStatus
from backend.engine.private.scheduler_runtime import PrivateSchedulerDispatchResult, PrivateSchedulerDispatchStatus, PrivateSchedulerRuntime, PrivateSchedulerRuntimeStatus
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerScope,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)
from backend.engine.private.scheduler_work_dispatcher import PrivateSchedulerTypedWorkDispatcher
from backend.tests.test_scheduler_run_persistence_transport import calendar_admission, event_admission, not_before, scheduled_admission
from backend.tests.test_scheduler_runtime import FakeRepository, SequenceClock, ready_run

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "backend" / "engine" / "private"
TESTS = ROOT / "backend" / "tests"
WK, SC, AS, RS, DS = PrivateSchedulerWorkKind, PrivateSchedulerScope, PrivateSchedulerRunApplyStatus, PrivateSchedulerRuntimeStatus, PrivateSchedulerDispatchStatus

MODULES = [
    "scheduler_trigger", "scheduler_scheduled_occurrence", "scheduler_recurrence", "scheduler_calendar_evidence", "scheduler_calendar_applicability",
    "scheduler_event_occurrence", "scheduler_run_admission", "scheduler_run_lifecycle",
    "scheduler_run_persistence_codec", "scheduler_run_persistence_transport", "scheduler_run_repository",
    "scheduler_runtime", "scheduler_work_dispatcher",
]
CI_TESTS = [
    "test_scheduler_trigger", "test_scheduler_scheduled_occurrence", "test_scheduler_recurrence", "test_scheduler_calendar_evidence",
    "test_scheduler_calendar_applicability", "test_scheduler_event_occurrence", "test_scheduler_run_admission", "test_scheduler_run_lifecycle",
    "test_scheduler_run_persistence_schema", "test_scheduler_run_persistence_codec", "test_scheduler_run_persistence_transport", "test_scheduler_run_repository",
    "test_scheduler_runtime", "test_scheduler_work_dispatcher", "test_scheduler_phase24_boundary",
]
STEP = "Phase 24 private scheduler correctness"
POLICY_ID = "private.scheduler" + ".v1"
LOGICAL_HASH = "0123456789abcdef" * 4


def tree(module: str) -> ast.Module:
    return ast.parse((PRIVATE / f"{module}.py").read_text(encoding="utf-8"))


def imports(module: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree(module)):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def identifiers(module: str) -> set[str]:
    t = tree(module)
    names = {n.id for n in ast.walk(t) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(t) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(t) if isinstance(n, ast.ImportFrom) for a in n.names}
    return names


# --- inventory / isolation -----------------------------------------------------------------------------------------

def test_phase24_module_inventory_is_exactly_the_closed_architecture() -> None:
    on_disk = sorted(p.stem for p in PRIVATE.glob("scheduler_*.py"))
    assert on_disk == sorted(MODULES)                      # growth forces a deliberate boundary review


def test_scheduler_graph_has_no_legacy_scheduler_or_queue_dependency() -> None:
    for module in MODULES:
        for imported in imports(module):
            assert not imported.startswith("backend.infrastructure"), (module, imported)
            assert "job_queue" not in imported and "redis" not in imported.split("."), (module, imported)
        names = identifiers(module)
        assert not names & {"spawn_background_job", "BackgroundTasks", "PENDING", "RUNNING", "COMPLETED"}, module
        lowered = {n.lower() for n in names}
        assert not [n for n in lowered if any(token in n for token in ("telegram", "macd", "watchlist"))], module
        assert not [n for n in ast.walk(tree(module)) if isinstance(n, ast.Constant) and n.value == 840 and type(n.value) is int], module


def test_work_kind_and_scope_contract_is_closed() -> None:
    assert {m.name: m.value for m in WK} == {"SOURCE_DATA_REFRESH": "source_data_refresh", "PORTFOLIO_ANALYSIS_REFRESH": "portfolio_analysis_refresh",
                                           "PORTFOLIO_HEALTH_CHECK": "portfolio_health_check", "GAME_CHANGER_REVIEW": "game_changer_review"}
    owner, portfolio = UUID(int=1), UUID(int=2)
    at = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)

    def trigger(kind, scope, owner_id, portfolio_id):
        return build_private_scheduler_trigger(
            trigger_kind=PrivateSchedulerTriggerKind.SCHEDULED, work_kind=kind, scope=scope, owner_id=owner_id, portfolio_id=portfolio_id, scheduled_for=at,
            event_cause_kind=None, cause_key=None, cause_available_at=None, policy_key=POLICY_ID, policy_revision=1, idempotency_sha256=LOGICAL_HASH)

    assert trigger(WK.SOURCE_DATA_REFRESH, SC.SYSTEM, None, None).scope is SC.SYSTEM
    with pytest.raises(ValueError):
        trigger(WK.SOURCE_DATA_REFRESH, SC.PORTFOLIO, owner, portfolio)
    for kind in (WK.PORTFOLIO_ANALYSIS_REFRESH, WK.PORTFOLIO_HEALTH_CHECK, WK.GAME_CHANGER_REVIEW):
        assert trigger(kind, SC.PORTFOLIO, owner, portfolio).scope is SC.PORTFOLIO
        with pytest.raises(ValueError):
            trigger(kind, SC.SYSTEM, None, None)


def test_runtime_and_dispatcher_have_no_automatic_trade_path() -> None:
    for module in ("scheduler_runtime", "scheduler_work_dispatcher"):
        for imported in imports(module):
            parts = set(imported.split("."))
            assert not parts & {"game_changer_gate", "allocation_rebalance", "allocation_rebalance_policy", "orchestrator", "execution", "order", "orders"}, (module, imported)
        assert not identifiers(module) & {"sell", "trade", "liquidate", "rebalance", "quarantine", "target_weight", "build_game_changer_decision_gate"}, module


# --- persistence authority -----------------------------------------------------------------------------------------

def test_repository_writes_only_through_the_two_rpcs_and_reconciles_by_exact_history() -> None:
    t = tree("scheduler_run_repository")
    calls = [n for n in ast.walk(t) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    rpc = sorted(c.args[0].value for c in calls if c.func.attr == "rpc")
    assert rpc == ["apply_private_scheduler_run_transition", "initialize_private_scheduler_run"]
    assert not {c.func.attr for c in calls} & {"insert", "update", "upsert", "delete"}
    assert not [c for c in calls if c.func.attr == "get_run"]                           # APPLIED reconciliation never rereads the current row
    apply_fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "_apply")
    history_calls = [c for c in ast.walk(apply_fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "_durable_history"]
    assert len(history_calls) == 1 and isinstance(history_calls[0].args[1], ast.Attribute) and history_calls[0].args[1].attr == "state_version"
    durable = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "_durable_history")
    assert [c for c in ast.walk(durable) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "get_transition"]


def test_no_automatic_retry_anywhere_in_the_write_path() -> None:
    for module in ("scheduler_run_repository", "scheduler_runtime", "scheduler_work_dispatcher"):
        t = tree(module)
        assert not [n for n in ast.walk(t) if isinstance(n, (ast.While, ast.For, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await))], module
        assert not identifiers(module) & {"sleep", "backoff", "jitter", "retry", "monotonic"}, module


# --- identity chains -----------------------------------------------------------------------------------------------

def test_event_identity_chain_has_one_hash() -> None:
    admission = event_admission()
    lifecycle = initialize_private_scheduler_run_lifecycle(admission=admission).after
    assert admission.event_occurrence.trigger.idempotency_sha256 == admission.run_idempotency_sha256 == lifecycle.admission.run_idempotency_sha256
    assert lifecycle.state is PrivateSchedulerRunState.READY and lifecycle.admission is admission


def test_scheduled_identity_chain_has_one_hash() -> None:
    admission = scheduled_admission()
    lifecycle = initialize_private_scheduler_run_lifecycle(admission=admission).after
    assert admission.scheduled_occurrence.trigger.idempotency_sha256 == admission.run_idempotency_sha256 == lifecycle.admission.run_idempotency_sha256


def test_calendar_proof_path_keeps_the_direct_run_identity() -> None:
    proof = calendar_admission()
    direct = admit_private_scheduler_scheduled_occurrence(occurrence=proof.calendar_applicability.candidate_occurrence)
    assert proof.source is not direct.source and proof.calendar_applicability is not None and direct.calendar_applicability is None
    assert proof.run_idempotency_sha256 == direct.run_idempotency_sha256 == proof.calendar_applicability.candidate_occurrence.trigger.idempotency_sha256
    assert admit_private_scheduler_calendar_applicability(applicability=proof.calendar_applicability).run_idempotency_sha256 == proof.run_idempotency_sha256


# --- cross-module runtime / dispatcher -----------------------------------------------------------------------------

class OrderedHandler:
    def __init__(self, repository: FakeRepository | None = None) -> None:
        self.repository = repository
        self.seen_calls: list[list[str]] = []
        self.contexts: list = []

    def handle(self, *, context):
        self.contexts.append(context)
        self.seen_calls.append(self.repository.names() if self.repository is not None else [])
        return PrivateSchedulerDispatchResult(DS.SUCCEEDED, None)


def compose(claim_status=AS.APPLIED):
    admission = scheduled_admission()
    nb = not_before(admission)
    repo = FakeRepository()
    repo.claim_status = claim_status
    handlers = [OrderedHandler(repo) for _ in range(4)]
    dispatcher = PrivateSchedulerTypedWorkDispatcher(
        source_data_refresh_handler=handlers[0], portfolio_analysis_refresh_handler=handlers[1], portfolio_health_check_handler=handlers[2],
        game_changer_review_handler=handlers[3])
    runtime = PrivateSchedulerRuntime(repository=repo, dispatcher=dispatcher, clock=SequenceClock(nb, nb + timedelta(minutes=5)))
    return admission, nb, repo, handlers, runtime


def test_claim_is_applied_before_the_handler_and_conflicts_never_reach_a_handler() -> None:
    admission, nb, repo, handlers, runtime = compose()
    result = runtime.execute_ready_run(persisted_run=ready_run(admission), claim_key="claim-1", lease_expires_at=nb + timedelta(minutes=10))
    assert result.status is RS.SUCCEEDED and handlers[2].seen_calls == [["claim_run"]] and [len(h.contexts) for h in handlers] == [0, 0, 1, 0]
    for status in (AS.NOT_FOUND, AS.VERSION_CONFLICT, AS.TRANSITION_CONFLICT):
        admission, nb, repo, handlers, runtime = compose(status)
        result = runtime.execute_ready_run(persisted_run=ready_run(admission), claim_key="claim-1", lease_expires_at=nb + timedelta(minutes=10))
        assert result.status.value.startswith("claim_") and all(h.contexts == [] for h in handlers) and repo.names() == ["claim_run"]


def test_work_identity_is_the_run_hash_and_not_the_claim_key() -> None:
    contexts = []
    for key in ("claim-a", "claim-b"):
        admission, nb, repo, handlers, runtime = compose()
        runtime.execute_ready_run(persisted_run=ready_run(admission), claim_key=key, lease_expires_at=nb + timedelta(minutes=10))
        contexts.append(handlers[2].contexts[0])
    assert contexts[0].work_idempotency_key == contexts[1].work_idempotency_key == admission.run_idempotency_sha256
    assert contexts[0].claim_key != contexts[1].claim_key


# --- security gate / documentation / CI ----------------------------------------------------------------------------

def test_gitleaks_gate_files_exist() -> None:
    for relative in (".github/workflows/gitleaks.yml", ".gitleaksignore", "docs/SECURITY_SECRET_SCANNING.md"):
        assert (ROOT / relative).is_file(), relative
    assert not (ROOT / ".gitleaks.toml").exists()


def _ci_step() -> str:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    start = text.index(f"- name: {STEP}")
    following = re.search(r"\n    - name: ", text[start + 1:])
    return text[start:start + 1 + following.start()] if following else text[start:]


def test_ci_runs_the_complete_explicit_phase24_suite_exactly_once() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert text.count(f"- name: {STEP}") == 1 and text.index("- name: Phase 23 Game Changer correctness") < text.index(f"- name: {STEP}")
    step = _ci_step()
    listed = re.findall(r"backend/tests/([A-Za-z0-9_]+)\.py", step)
    assert sorted(listed) == sorted(CI_TESTS) and len(listed) == len(set(listed)) == 15
    assert "-q --disable-socket" in step and "*" not in step.replace("\\", "")
    assert sorted(p.stem for p in TESTS.glob("test_scheduler_*.py")) == sorted(CI_TESTS)      # a new scheduler test needs deliberate CI review
    for name in CI_TESTS:
        assert (TESTS / f"{name}.py").is_file()


def test_boundary_document_states_the_limits() -> None:
    doc = (ROOT / "docs" / "PRIVATE_SCHEDULER_PHASE24_BOUNDARY.md").read_text(encoding="utf-8")
    for needle in ("no automatic renewal", "heartbeat", "real PostgreSQL", "exactly-once", "no scheduler polling daemon", "KAP", "tzdb", "PORTFOLIO scoped"):
        assert needle in doc, needle
