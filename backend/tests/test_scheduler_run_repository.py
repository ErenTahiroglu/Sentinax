"""
backend/tests/test_scheduler_run_repository.py
==============================================
Phase 24D2B2: RPC-only scheduler run repository. Writes only through the two migration-024 RPCs, reads only through explicit-projection selects, predicts
every transition through the CLOSED 24C2B domain before a single RPC, and reconciles APPLIED results against the immutable exact-version history row.
A scripted deterministic client double records every call; no real database is involved.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.engine.private import scheduler_run_repository as repo_module
from backend.engine.private.scheduler_run_lifecycle import (
    PrivateSchedulerRunLifecycle,
    PrivateSchedulerRunState,
    PrivateSchedulerRunTransitionKind,
    initialize_private_scheduler_run_lifecycle,
)
from backend.engine.private.scheduler_run_persistence_codec import serialize_private_scheduler_run_admission
from backend.engine.private.scheduler_run_persistence_transport import (
    PRIVATE_SCHEDULER_RUN_SELECT,
    PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT,
    PrivateSchedulerPersistedRun,
    PrivateSchedulerPersistedTransition,
)
from backend.engine.private.scheduler_run_repository import (
    PrivateSchedulerRunApplyResult,
    PrivateSchedulerRunApplyStatus,
    PrivateSchedulerRunInitializationResult,
    PrivateSchedulerRunInitializationStatus,
    PrivateSchedulerRunRepository,
)
from backend.tests.invariants import static_guards as sg
from backend.tests.test_scheduler_run_persistence_transport import (
    ADMISSIONS,
    chain,
    event_admission,
    history_row,
    minutes,
    not_before,
    run_row,
    scheduled_admission,
    ts,
)

KIND, STATE = PrivateSchedulerRunTransitionKind, PrivateSchedulerRunState
INIT, APPLY = "initialize_private_scheduler_run", "apply_private_scheduler_run_transition"
RUNS, HISTORY = "private_scheduler_runs", "private_scheduler_run_transitions"
HASH = "a" * 64


# --- deterministic client double -----------------------------------------------------------------------------------

class FakeClient:
    """Supports only client.rpc(name, params).execute() and client.table(name).select(c).eq(k, v).limit(n).execute()."""

    def __init__(self) -> None:
        self.rpc_calls: list[tuple[str, dict]] = []
        self.table_calls: list[dict] = []
        self.rpc_data: dict[str, object] = {}
        self.rpc_error: Exception | None = None
        self.tables: dict[str, object] = {}                 # a list of row dicts, or a raw object returned as `data`

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        outer = self

        class Call:
            def execute(self):
                if outer.rpc_error is not None:
                    raise outer.rpc_error
                return SimpleNamespace(data=outer.rpc_data[name])
        return Call()

    def table(self, name):
        record = {"table": name, "select": None, "filters": [], "limit": None, "executed": 0}
        self.table_calls.append(record)
        outer = self

        class Query:
            def select(self, columns):
                record["select"] = columns
                return self

            def eq(self, column, value):
                record["filters"].append((column, value))
                return self

            def limit(self, count):
                record["limit"] = count
                return self

            def execute(self):
                record["executed"] += 1
                data = outer.tables.get(name, [])
                if isinstance(data, list):
                    data = [row for row in data if all(not isinstance(row, dict) or row.get(c) == v for c, v in record["filters"])]
                return SimpleNamespace(data=data)
        return Query()

    def reads(self, table):
        return [c for c in self.table_calls if c["table"] == table]


def rpc_row(status, run_hash, state, version):
    return [{"status": status, "run_idempotency_sha256": run_hash, "state": state, "state_version": version}]


def initialized_client(admission, status="initialized", state="ready", version=1):
    client = FakeClient()
    client.rpc_data[INIT] = rpc_row(status, admission.run_idempotency_sha256, state, version)
    initial = initialize_private_scheduler_run_lifecycle(admission=admission)
    client.tables[HISTORY] = [history_row(initial, None)]
    return client


# --- construction / contracts --------------------------------------------------------------------------------------

def test_constructor_requires_a_client_and_has_no_other_parameters() -> None:
    assert list(inspect.signature(PrivateSchedulerRunRepository.__init__).parameters) == ["self", "client"]
    with pytest.raises(ValueError):
        PrivateSchedulerRunRepository(None)
    PrivateSchedulerRunRepository(FakeClient())


def test_status_enums_and_result_shapes_are_exact() -> None:
    assert {m.name: m.value for m in PrivateSchedulerRunInitializationStatus} == {
        "INITIALIZED": "initialized", "IDEMPOTENT_DUPLICATE": "idempotent_duplicate", "CONFLICT": "conflict"}
    assert {m.name: m.value for m in PrivateSchedulerRunApplyStatus} == {
        "APPLIED": "applied", "NOT_FOUND": "not_found", "VERSION_CONFLICT": "version_conflict", "TRANSITION_CONFLICT": "transition_conflict"}
    assert [f.name for f in dataclasses.fields(PrivateSchedulerRunInitializationResult)] == [
        "status", "run_idempotency_sha256", "state", "state_version", "initialize_transition"]
    assert [f.name for f in dataclasses.fields(PrivateSchedulerRunApplyResult)] == [
        "status", "run_idempotency_sha256", "state", "state_version", "predicted_transition", "persisted_transition"]
    for cls in (PrivateSchedulerRunInitializationResult, PrivateSchedulerRunApplyResult):
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(cls))
        assert cls.__dataclass_params__.frozen is True


def test_public_method_signatures_are_keyword_only() -> None:
    expected = {
        "initialize_run": ["admission"], "get_run": ["run_idempotency_sha256"], "get_transition": ["admission", "after_state_version"],
        "claim_run": ["lifecycle", "expected_version", "claim_key", "claimed_at", "lease_expires_at"],
        "renew_claim": ["lifecycle", "expected_version", "claim_key", "renewed_at", "lease_expires_at"],
        "take_over_expired_claim": ["lifecycle", "expected_version", "claim_key", "claimed_at", "lease_expires_at"],
        "succeed_run": ["lifecycle", "expected_version", "claim_key", "terminal_at"],
        "fail_run": ["lifecycle", "expected_version", "claim_key", "terminal_at", "failure_code"],
    }
    for name, params in expected.items():
        parameters = list(inspect.signature(getattr(PrivateSchedulerRunRepository, name)).parameters.values())[1:]
        assert [p.name for p in parameters] == params
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)


# --- initialization ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_initialize_rpc_parameters_equal_the_closed_admission_authority(name: str) -> None:
    admission = ADMISSIONS[name]()
    client = initialized_client(admission)
    PrivateSchedulerRunRepository(client).initialize_run(admission=admission)
    assert len(client.rpc_calls) == 1
    rpc_name, params = client.rpc_calls[0]
    trigger = admission.trigger
    assert rpc_name == INIT
    assert params == {
        "p_run_idempotency_sha256": admission.run_idempotency_sha256, "p_admission_source": admission.source.value,
        "p_trigger_kind": trigger.trigger_kind.value, "p_work_kind": trigger.work_kind.value, "p_scope": trigger.scope.value,
        "p_owner_id": None if trigger.owner_id is None else str(trigger.owner_id),
        "p_portfolio_id": None if trigger.portfolio_id is None else str(trigger.portfolio_id),
        "p_scheduled_for": None if trigger.scheduled_for is None else trigger.scheduled_for.isoformat(timespec="microseconds"),
        "p_event_cause_kind": None if trigger.event_cause_kind is None else trigger.event_cause_kind.value, "p_cause_key": trigger.cause_key,
        "p_cause_available_at": None if trigger.cause_available_at is None else trigger.cause_available_at.isoformat(timespec="microseconds"),
        "p_policy_key": trigger.policy_key, "p_policy_revision": trigger.policy_revision,
        "p_admission_payload": serialize_private_scheduler_run_admission(admission=admission),
    }


@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_initialized_reads_exact_initialize_history_and_reconciles(name: str) -> None:
    admission = ADMISSIONS[name]()
    client = initialized_client(admission)
    result = PrivateSchedulerRunRepository(client).initialize_run(admission=admission)
    assert result.status is PrivateSchedulerRunInitializationStatus.INITIALIZED
    assert (result.run_idempotency_sha256, result.state, result.state_version) == (admission.run_idempotency_sha256, STATE.READY, 1)
    transition = result.initialize_transition
    assert type(transition) is PrivateSchedulerPersistedTransition and transition.kind is KIND.INITIALIZE
    assert transition.after.admission is admission and transition.after.state is STATE.READY and transition.before_state_version is None
    assert transition.transition_at is None
    assert len(client.rpc_calls) == 1 and len(client.table_calls) == 1
    read = client.table_calls[0]
    assert read["table"] == HISTORY and read["select"] == PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT and read["limit"] == 2
    assert read["filters"] == [("run_idempotency_sha256", admission.run_idempotency_sha256), ("after_state_version", 1)]


def test_idempotent_duplicate_may_report_an_advanced_state_and_still_verifies_history() -> None:
    admission = scheduled_admission()
    client = initialized_client(admission, status="idempotent_duplicate", state="claimed", version=3)
    result = PrivateSchedulerRunRepository(client).initialize_run(admission=admission)
    assert result.status is PrivateSchedulerRunInitializationStatus.IDEMPOTENT_DUPLICATE
    assert (result.state, result.state_version) == (STATE.CLAIMED, 3)
    assert result.initialize_transition.kind is KIND.INITIALIZE
    assert client.table_calls[0]["filters"][1] == ("after_state_version", 1)
    assert len(client.rpc_calls) == 1


def test_initialization_conflict_reads_nothing_and_adopts_nothing() -> None:
    admission = scheduled_admission()
    client = initialized_client(admission, status="conflict", state="ready", version=1)
    result = PrivateSchedulerRunRepository(client).initialize_run(admission=admission)
    assert result.status is PrivateSchedulerRunInitializationStatus.CONFLICT and result.initialize_transition is None
    assert client.table_calls == [] and len(client.rpc_calls) == 1


def test_initialization_rejects_malformed_rpc_responses() -> None:
    admission = scheduled_admission()
    good = rpc_row("initialized", admission.run_idempotency_sha256, "ready", 1)[0]
    bad_payloads = [
        None, {}, [], [good, good], ["x"], [{**good, "extra": 1}], [{k: v for k, v in good.items() if k != "state"}], [{**good, "status": "created"}],
        [{**good, "status": 1}], [{**good, "state": "pending"}], [{**good, "state": None}], [{**good, "state": "READY"}], [{**good, "state_version": True}],
        [{**good, "state_version": 1.0}], [{**good, "state_version": "1"}], [{**good, "state_version": 0}], [{**good, "state_version": None}],
        [{**good, "run_idempotency_sha256": "b" * 64}], [{**good, "run_idempotency_sha256": None}], [{**good, "run_idempotency_sha256": good["run_idempotency_sha256"].upper()}],
        [{**good, "state_version": 2}],                                 # INITIALIZED must be READY / v1
        [{**good, "state": "claimed"}],
    ]
    for payload in bad_payloads:
        client = initialized_client(admission)
        client.rpc_data[INIT] = payload
        with pytest.raises(RuntimeError):
            PrivateSchedulerRunRepository(client).initialize_run(admission=admission)


def test_initialization_requires_valid_durable_initialize_history() -> None:
    admission = scheduled_admission()
    steps = chain(admission)
    initial_row = history_row(*steps["initialize"])
    claim_row = history_row(*steps["claim"])
    other = event_admission()
    other_row = history_row(initialize_private_scheduler_run_lifecycle(admission=other), None)
    bad_histories = [[], [claim_row], [other_row], [{**initial_row, "after_state_version": 2}], [{**initial_row, "extra": 1}], [initial_row, initial_row],
                     ["x"], None, {}]
    for status in ("initialized", "idempotent_duplicate"):
        for history in bad_histories:
            client = initialized_client(admission, status=status)
            client.tables[HISTORY] = history
            with pytest.raises(RuntimeError):
                PrivateSchedulerRunRepository(client).initialize_run(admission=admission)


def test_initialization_domain_failure_and_type_checks_make_no_rpc() -> None:
    client = FakeClient()
    repository = PrivateSchedulerRunRepository(client)
    for bad in (None, object(), scheduled_admission().trigger):
        with pytest.raises(TypeError):
            repository.initialize_run(admission=bad)  # type: ignore[arg-type]
    assert client.rpc_calls == [] and client.table_calls == []


# --- reads ---------------------------------------------------------------------------------------------------------

def test_get_run_row_counts_and_projection() -> None:
    admission = scheduled_admission()
    row = run_row(admission)
    client = FakeClient()
    repository = PrivateSchedulerRunRepository(client)
    client.tables[RUNS] = []
    assert repository.get_run(run_idempotency_sha256=admission.run_idempotency_sha256) is None
    client.tables[RUNS] = [row]
    persisted = repository.get_run(run_idempotency_sha256=admission.run_idempotency_sha256)
    assert type(persisted) is PrivateSchedulerPersistedRun and persisted.lifecycle.admission == admission
    read = client.table_calls[-1]
    assert read == {"table": RUNS, "select": PRIVATE_SCHEDULER_RUN_SELECT, "filters": [("run_idempotency_sha256", admission.run_idempotency_sha256)],
                    "limit": 2, "executed": 1}
    for malformed in ([row, row], None, {}, [None], ["x"], "x"):
        client.tables[RUNS] = malformed
        with pytest.raises(RuntimeError):
            repository.get_run(run_idempotency_sha256=admission.run_idempotency_sha256)


def test_get_run_rejects_non_canonical_hashes_before_querying() -> None:
    client = FakeClient()
    repository = PrivateSchedulerRunRepository(client)
    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None, 1, b"a" * 64, ("a" * 64,), " " + "a" * 63):
        with pytest.raises((TypeError, ValueError)):
            repository.get_run(run_idempotency_sha256=bad)  # type: ignore[arg-type]
    assert client.table_calls == []


def test_get_run_hydration_failure_propagates() -> None:
    admission = scheduled_admission()
    client = FakeClient()
    client.tables[RUNS] = [{**run_row(admission), "policy_revision": 99}]
    with pytest.raises(ValueError):
        PrivateSchedulerRunRepository(client).get_run(run_idempotency_sha256=admission.run_idempotency_sha256)


def test_get_transition_row_counts_filters_and_hydration() -> None:
    admission = scheduled_admission()
    claim, at = chain(admission)["claim"]
    row = history_row(claim, at)
    client = FakeClient()
    repository = PrivateSchedulerRunRepository(client)
    client.tables[HISTORY] = []
    assert repository.get_transition(admission=admission, after_state_version=2) is None
    client.tables[HISTORY] = [row]
    persisted = repository.get_transition(admission=admission, after_state_version=2)
    assert persisted.kind is KIND.CLAIM and persisted.after.admission is admission
    assert client.table_calls[-1] == {"table": HISTORY, "select": PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT, "limit": 2, "executed": 1,
                                      "filters": [("run_idempotency_sha256", admission.run_idempotency_sha256), ("after_state_version", 2)]}
    for malformed in ([row, row], None, {}, [None]):
        client.tables[HISTORY] = malformed
        with pytest.raises(RuntimeError):
            repository.get_transition(admission=admission, after_state_version=2)
    client.tables[HISTORY] = [{**row, "after_state": "pending"}]
    with pytest.raises(ValueError):
        repository.get_transition(admission=admission, after_state_version=2)


def test_get_transition_argument_validation_makes_no_query() -> None:
    admission = scheduled_admission()
    client = FakeClient()
    repository = PrivateSchedulerRunRepository(client)
    for version in (True, 0, -1, 2.0, "2", None):
        with pytest.raises((TypeError, ValueError)):
            repository.get_transition(admission=admission, after_state_version=version)  # type: ignore[arg-type]
    for bad in (None, object(), admission.trigger):
        with pytest.raises(TypeError):
            repository.get_transition(admission=bad, after_state_version=1)  # type: ignore[arg-type]
    assert client.table_calls == []


# --- transitions ---------------------------------------------------------------------------------------------------

def command(key: str, admission):
    """Prior lifecycle + repository method name + kwargs + RPC params for the chain step named `key`."""
    steps = chain(admission)
    nb = not_before(admission)
    initial = steps["initialize"][0].after
    claimed = steps["claim"][0].after
    renewed = steps["renew"][0].after
    table = {
        "claim": (initial, "claim_run", dict(claim_key="claim-a", claimed_at=nb, lease_expires_at=minutes(nb, 10)),
                  dict(p_claim_key="claim-a", p_transition_at=nb, p_lease_expires_at=minutes(nb, 10), p_failure_code=None)),
        "renew": (claimed, "renew_claim", dict(claim_key="claim-a", renewed_at=minutes(nb, 4), lease_expires_at=minutes(nb, 20)),
                  dict(p_claim_key="claim-a", p_transition_at=minutes(nb, 4), p_lease_expires_at=minutes(nb, 20), p_failure_code=None)),
        "takeover": (renewed, "take_over_expired_claim", dict(claim_key="claim-b", claimed_at=minutes(nb, 20), lease_expires_at=minutes(nb, 40)),
                     dict(p_claim_key="claim-b", p_transition_at=minutes(nb, 20), p_lease_expires_at=minutes(nb, 40), p_failure_code=None)),
        "succeed": (steps["takeover"][0].after, "succeed_run", dict(claim_key="claim-b", terminal_at=minutes(nb, 21)),
                    dict(p_claim_key="claim-b", p_transition_at=minutes(nb, 21), p_lease_expires_at=None, p_failure_code=None)),
        "fail": (claimed, "fail_run", dict(claim_key="claim-a", terminal_at=minutes(nb, 5), failure_code="engine_error"),
                 dict(p_claim_key="claim-a", p_transition_at=minutes(nb, 5), p_lease_expires_at=None, p_failure_code="engine_error")),
    }
    prior, method, kwargs, params = table[key]
    params = {k: (v.isoformat(timespec="microseconds") if hasattr(v, "isoformat") else v) for k, v in params.items()}
    return steps[key], prior, method, kwargs, params


def applied_client(key, admission):
    (transition, at), prior, method, kwargs, params = command(key, admission)
    client = FakeClient()
    client.rpc_data[APPLY] = rpc_row("applied", admission.run_idempotency_sha256, transition.after.state.value, transition.after.state_version)
    client.tables[HISTORY] = [history_row(transition, at)]
    return client


def run_command(client, key, admission, **overrides):
    (transition, at), prior, method, kwargs, params = command(key, admission)
    kwargs = {**kwargs, **overrides}
    return getattr(PrivateSchedulerRunRepository(client), method)(lifecycle=prior, expected_version=prior.state_version, **kwargs)


@pytest.mark.parametrize("name", list(ADMISSIONS))
@pytest.mark.parametrize("key", ["claim", "renew", "takeover", "succeed", "fail"])
def test_all_five_applied_transitions(key: str, name: str) -> None:
    admission = ADMISSIONS[name]()
    (transition, at), prior, method, kwargs, params = command(key, admission)
    client = applied_client(key, admission)
    result = run_command(client, key, admission)
    assert result.status is PrivateSchedulerRunApplyStatus.APPLIED
    assert (result.run_idempotency_sha256, result.state, result.state_version) == (admission.run_idempotency_sha256, transition.after.state, transition.after.state_version)
    assert result.predicted_transition == transition and result.predicted_transition.before == prior
    persisted = result.persisted_transition
    assert persisted.kind is transition.kind and persisted.before_state_version == prior.state_version and persisted.after == transition.after
    assert persisted.after.admission is result.predicted_transition.after.admission
    assert persisted.transition_at == at.astimezone(at.tzinfo) and persisted.transition_at == at
    assert len(client.rpc_calls) == 1
    rpc_name, sent = client.rpc_calls[0]
    assert rpc_name == APPLY
    assert sent == {"p_run_idempotency_sha256": admission.run_idempotency_sha256, "p_expected_version": prior.state_version,
                    "p_transition_kind": transition.kind.value, **params}
    assert len(client.table_calls) == 1 and client.table_calls[0]["table"] == HISTORY
    assert client.table_calls[0]["filters"] == [("run_idempotency_sha256", admission.run_idempotency_sha256), ("after_state_version", transition.after.state_version)]
    assert client.table_calls[0]["select"] == PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT and client.reads(RUNS) == []


def test_renewal_is_fully_reconciled_from_prior_lifecycle_renewed_at_and_history() -> None:
    admission = scheduled_admission()
    nb = not_before(admission)
    (transition, at), prior, method, kwargs, params = command("renew", admission)
    client = applied_client("renew", admission)
    result = run_command(client, "renew", admission)
    persisted = result.persisted_transition
    assert persisted.transition_at == minutes(nb, 4) == kwargs["renewed_at"]
    assert persisted.transition_at < prior.lease_expires_at < persisted.after.lease_expires_at           # prior expiry known here, unknown to D2B1
    assert persisted.after.claimed_at == prior.claimed_at and persisted.transition_at != persisted.after.claimed_at
    for wrong in (minutes(nb, 3), minutes(nb, 5), nb):
        client = applied_client("renew", admission)
        client.tables[HISTORY] = [history_row(transition, wrong)]
        with pytest.raises(RuntimeError):
            run_command(client, "renew", admission)


def test_current_row_is_never_read_and_may_have_advanced_after_applied() -> None:
    admission = scheduled_admission()
    client = applied_client("claim", admission)
    advanced = chain(admission)["renew"][0].after
    client.tables[RUNS] = [run_row(admission, advanced)]
    result = run_command(client, "claim", admission)
    assert result.status is PrivateSchedulerRunApplyStatus.APPLIED and client.reads(RUNS) == []


@pytest.mark.parametrize("status", ["not_found", "version_conflict", "transition_conflict"])
def test_non_applied_statuses_return_no_transitions_and_do_not_retry_or_read(status: str) -> None:
    admission = scheduled_admission()
    (transition, at), prior, method, kwargs, params = command("claim", admission)
    client = FakeClient()
    state, version = {"not_found": (None, None), "version_conflict": ("claimed", 5), "transition_conflict": ("ready", 1)}[status]
    client.rpc_data[APPLY] = rpc_row(status, admission.run_idempotency_sha256, state, version)
    result = run_command(client, "claim", admission)
    assert result.status is PrivateSchedulerRunApplyStatus(status)
    assert result.predicted_transition is None and result.persisted_transition is None
    assert (result.state.value if result.state else None, result.state_version) == (state, version)
    assert len(client.rpc_calls) == 1 and client.table_calls == []


def test_conflict_contract_violations_are_runtime_errors() -> None:
    admission = scheduled_admission()
    h = admission.run_idempotency_sha256
    cases = [
        rpc_row("version_conflict", h, "ready", 1),                  # expected_version is 1: a version conflict cannot report it
        rpc_row("transition_conflict", h, "claimed", 2),             # a transition conflict is reached only at the expected version
        rpc_row("version_conflict", h, None, 3), rpc_row("version_conflict", h, "ready", None), rpc_row("transition_conflict", h, None, 1),
        rpc_row("not_found", h, "ready", None), rpc_row("not_found", h, None, 1), rpc_row("not_found", h, "ready", 1),
        rpc_row("applied", h, "claimed", 3),                         # applied with a state/version that is not the prediction
        rpc_row("applied", h, "ready", 2), rpc_row("applied", h, "claimed", True), rpc_row("applied", "b" * 64, "claimed", 2),
        rpc_row("applied", None, "claimed", 2), rpc_row("pending", h, "claimed", 2), rpc_row("applied", h, "running", 2),
        rpc_row("version_conflict", h, "claimed", True), rpc_row("version_conflict", h, "claimed", 1.5),
        None, [], {}, "x", [{}], rpc_row("applied", h, "claimed", 2) * 2, [{**rpc_row("applied", h, "claimed", 2)[0], "extra": 1}],
        [{k: v for k, v in rpc_row("applied", h, "claimed", 2)[0].items() if k != "status"}],
    ]
    for payload in cases:
        client = applied_client("claim", admission)
        client.rpc_data[APPLY] = payload
        with pytest.raises(RuntimeError):
            run_command(client, "claim", admission)


def test_applied_history_mismatches_are_runtime_errors() -> None:
    admission = scheduled_admission()
    (transition, at), prior, method, kwargs, params = command("claim", admission)
    good = history_row(transition, at)
    renewed, renewed_at = chain(admission)["renew"]
    other = event_admission()
    other_claim, other_at = chain(other)["claim"]
    bad_histories = [
        [], None, {}, [good, good], ["x"],
        [history_row(renewed, renewed_at)],                                           # a different version/kind, filtered out by the version predicate
        [{**good, "transition_kind": "renew_claim", "before_state_version": 2, "after_state_version": 3, "transition_at": ts(minutes(not_before(admission), 1))}],
        [{**good, "before_state_version": 0}], [{**good, "claim_key": "claim-zzz"}], [{**good, "lease_expires_at": ts(minutes(not_before(admission), 11))}],
        [{**good, "transition_at": ts(minutes(not_before(admission), 1))}], [{**good, "transition_at": None}],
        [{**good, "run_idempotency_sha256": other_claim.after.admission.run_idempotency_sha256}], [{**good, "extra": 1}],
        [history_row(other_claim, other_at)],
    ]
    for history in bad_histories:
        client = applied_client("claim", admission)
        client.tables[HISTORY] = history
        with pytest.raises(RuntimeError):
            run_command(client, "claim", admission)
        assert len(client.rpc_calls) == 1


def test_domain_rejection_means_zero_rpc_calls() -> None:
    admission = scheduled_admission()
    nb = not_before(admission)
    steps = chain(admission)
    initial, claimed = steps["initialize"][0].after, steps["claim"][0].after
    rejected = [
        ("claim_run", initial, dict(claim_key="c", claimed_at=nb - timedelta(microseconds=1), lease_expires_at=minutes(nb, 10))),          # early claim
        ("claim_run", claimed, dict(claim_key="c", claimed_at=nb, lease_expires_at=minutes(nb, 10))),                                       # not READY
        ("renew_claim", claimed, dict(claim_key="claim-a", renewed_at=minutes(nb, 10), lease_expires_at=minutes(nb, 20))),                  # expired
        ("renew_claim", claimed, dict(claim_key="claim-x", renewed_at=minutes(nb, 4), lease_expires_at=minutes(nb, 20))),                   # wrong key
        ("renew_claim", claimed, dict(claim_key="claim-a", renewed_at=minutes(nb, 4), lease_expires_at=minutes(nb, 10))),                   # no extension
        ("take_over_expired_claim", claimed, dict(claim_key="claim-b", claimed_at=minutes(nb, 9), lease_expires_at=minutes(nb, 30))),       # premature
        ("take_over_expired_claim", claimed, dict(claim_key="claim-a", claimed_at=minutes(nb, 10), lease_expires_at=minutes(nb, 30))),      # same key
        ("succeed_run", claimed, dict(claim_key="claim-x", terminal_at=minutes(nb, 5))),                                                    # wrong claim key
        ("succeed_run", claimed, dict(claim_key="claim-a", terminal_at=minutes(nb, 10))),                                                   # lease over
        ("fail_run", claimed, dict(claim_key="claim-a", terminal_at=minutes(nb, 5), failure_code="Bad Code")),                              # invalid code
        ("fail_run", claimed, dict(claim_key="claim-a", terminal_at=minutes(nb, 5), failure_code=None)),
    ]
    for method, lifecycle, kwargs in rejected:
        client = FakeClient()
        with pytest.raises((TypeError, ValueError)):
            getattr(PrivateSchedulerRunRepository(client), method)(lifecycle=lifecycle, expected_version=lifecycle.state_version, **kwargs)
        assert client.rpc_calls == [] and client.table_calls == []
    for bad in (None, object(), initial.admission):
        client = FakeClient()
        with pytest.raises(TypeError):
            PrivateSchedulerRunRepository(client).claim_run(lifecycle=bad, expected_version=1, claim_key="c", claimed_at=nb, lease_expires_at=minutes(nb, 5))
        assert client.rpc_calls == []

    class SubLifecycle(PrivateSchedulerRunLifecycle):
        pass

    sub = SubLifecycle(**{f.name: getattr(initial, f.name) for f in dataclasses.fields(initial)})
    client = FakeClient()
    with pytest.raises(TypeError):
        PrivateSchedulerRunRepository(client).claim_run(lifecycle=sub, expected_version=1, claim_key="c", claimed_at=nb, lease_expires_at=minutes(nb, 5))
    assert client.rpc_calls == []


def test_execute_errors_propagate_without_retry() -> None:
    admission = scheduled_admission()
    client = applied_client("claim", admission)
    client.rpc_error = ConnectionError("boom")
    with pytest.raises(ConnectionError):
        run_command(client, "claim", admission)
    assert len(client.rpc_calls) == 1
    client = initialized_client(admission)
    client.rpc_error = ConnectionError("boom")
    with pytest.raises(ConnectionError):
        PrivateSchedulerRunRepository(client).initialize_run(admission=admission)
    assert len(client.rpc_calls) == 1


# --- source-level invariants ---------------------------------------------------------------------------------------

_SOURCE = Path(repo_module.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_run_repository.py"


def _attribute_calls() -> list[ast.Call]:
    return [n for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]


def test_no_write_method_is_ever_called_on_any_client_chain() -> None:
    forbidden = {"insert", "update", "upsert", "delete"}
    assert not [c.func.attr for c in _attribute_calls() if c.func.attr in forbidden]
    names = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not names & (forbidden | {"getattr", "setattr", "eval", "exec"})
    table_chain_methods = {c.func.attr for c in _attribute_calls()
                           if isinstance(c.func.value, ast.Call) and isinstance(c.func.value.func, ast.Attribute)}
    assert table_chain_methods <= {"select", "eq", "limit", "execute", "isoformat", "rpc", "table", "astimezone", "get", "hex"} | {"fullmatch"}


def test_rpc_names_are_the_two_literal_allowlist() -> None:
    rpc_calls = [c for c in _attribute_calls() if c.func.attr == "rpc"]
    assert len(rpc_calls) == 2
    assert sorted(c.args[0].value for c in rpc_calls) == [APPLY, INIT]
    assert all(isinstance(c.args[0], ast.Constant) and type(c.args[0].value) is str and len(c.args) == 2 and not c.keywords for c in rpc_calls)
    table_calls = [c for c in _attribute_calls() if c.func.attr == "table"]
    assert {c.args[0].value for c in table_calls if isinstance(c.args[0], ast.Constant)} == {RUNS, HISTORY} and all(isinstance(c.args[0], ast.Constant) for c in table_calls)


def test_projections_come_from_the_transport_constants_only() -> None:
    selects = [c for c in _attribute_calls() if c.func.attr == "select"]
    assert len(selects) == 2
    assert sorted(c.args[0].id for c in selects) == ["PRIVATE_SCHEDULER_RUN_SELECT", "PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT"]
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is str and "," in n.value and "run_idempotency" in n.value]


def test_no_retry_loops_clock_randomness_dispatch_or_legacy_queue() -> None:
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.For, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await))]
    names = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert not names & {"now", "utcnow", "today", "time", "sleep", "monotonic", "uuid4", "uuid1", "random", "secrets", "environ", "getenv", "retry", "backoff", "jitter"}
    imported: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for forbidden in ("job_queue", "spawn_background_job", "BackgroundTasks", "redis", "fastapi", "supabase", "os", "random", "secrets", "uuid", "time", "asyncio", "hashlib"):
        assert not any(forbidden == part or forbidden in part.split(".") for part in imported), forbidden
    assert "spawn_background_job" not in _SOURCE and "BackgroundTasks" not in _SOURCE


def test_module_is_not_registered_pure() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documents_the_trust_boundary() -> None:
    doc = repo_module.__doc__ or ""
    for needle in ("service-role", "trusted backend", "RPC", "no automatic retry", "immutable", "never exposed"):
        assert needle in doc, needle
