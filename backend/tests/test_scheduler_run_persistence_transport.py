"""
backend/tests/test_scheduler_run_persistence_transport.py
=========================================================
Phase 24D2B1: raw PostgREST row transport for scheduler persistence. A current-run row is hydrated through the CLOSED D2A payload codec and then
reconciled against every immutable denormalized column; SQL TIMESTAMPTZ transport values are parsed as aware instants and canonicalized to UTC
(their lexical offset is not the audit payload's). History rows keep `transition_at` (including the renewal instant) separately from the closed
lifecycle snapshot. No database call, RPC, repository, client, clock or hashing.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import re
from collections import OrderedDict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest

from backend.engine.private import scheduler_run_persistence_transport as transport
from backend.engine.private.scheduler_calendar_applicability import (
    PrivateSchedulerCalendarApplicabilityMode,
    build_private_scheduler_calendar_constraint,
    evaluate_private_scheduler_calendar_applicability,
)
from backend.engine.private.scheduler_calendar_evidence import (
    PrivateSchedulerCalendarCoverage,
    PrivateSchedulerCalendarKind,
    PrivateSchedulerExchangeSessionWindow,
    bind_private_scheduler_calendar_pit,
    build_private_scheduler_calendar_date_evidence,
)
from backend.engine.private.scheduler_event_occurrence import build_private_scheduler_event_occurrence
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerRecurrenceKind,
    build_private_scheduler_recurrence,
    evaluate_private_scheduler_civil_date,
)
from backend.engine.private.scheduler_run_admission import (
    PrivateSchedulerRunAdmission,
    admit_private_scheduler_calendar_applicability,
    admit_private_scheduler_event_occurrence,
    admit_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_run_lifecycle import (
    PrivateSchedulerRunLifecycle,
    PrivateSchedulerRunState,
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
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerAmbiguousTimePolicy, build_private_scheduler_scheduled_occurrence
from backend.engine.private.scheduler_trigger import PrivateSchedulerEventCauseKind, PrivateSchedulerScope, PrivateSchedulerWorkKind
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
AP, SC, WK, EC = PrivateSchedulerAmbiguousTimePolicy, PrivateSchedulerScope, PrivateSchedulerWorkKind, PrivateSchedulerEventCauseKind
STATE, KIND = PrivateSchedulerRunState, PrivateSchedulerRunTransitionKind
CK, CV, MODE = PrivateSchedulerCalendarKind, PrivateSchedulerCalendarCoverage, PrivateSchedulerCalendarApplicabilityMode
DAY = date(2026, 10, 2)
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "024_private_scheduler_run_persistence.sql"

RUN_COLUMNS = ["run_idempotency_sha256", "admission_source", "trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for",
               "event_cause_kind", "cause_key", "cause_available_at", "policy_key", "policy_revision", "admission_payload", "state", "state_version",
               "claim_key", "claimed_at", "lease_expires_at", "terminal_at", "failure_code", "created_at", "updated_at"]
HISTORY_COLUMNS = ["run_idempotency_sha256", "after_state_version", "transition_kind", "before_state_version", "transition_at", "after_state", "claim_key",
                   "claimed_at", "lease_expires_at", "terminal_at", "failure_code", "recorded_at"]
IMMUTABLE = ["run_idempotency_sha256", "admission_source", "trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for",
             "event_cause_kind", "cause_key", "cause_available_at", "policy_key", "policy_revision"]


# --- admissions --------------------------------------------------------------------------------------------------

def scheduled_admission():
    return admit_private_scheduler_scheduled_occurrence(occurrence=build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.direct", schedule_revision=7, timezone_key="Europe/Istanbul", local_date=DAY, local_time=time(9, 55),
        ambiguous_time_policy=AP.LATER, work_kind=WK.PORTFOLIO_HEALTH_CHECK, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO,
        policy_key="private.scheduler.v2", policy_revision=5))


def system_admission():
    return admit_private_scheduler_scheduled_occurrence(occurrence=build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.system", schedule_revision=1, timezone_key="UTC", local_date=DAY, local_time=time(6, 55),
        ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
        policy_key="private.scheduler.v1", policy_revision=1))


def event_admission():
    return admit_private_scheduler_event_occurrence(occurrence=build_private_scheduler_event_occurrence(
        work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, event_cause_kind=EC.DISCLOSURE_INGESTED,
        cause_key="KAP-Disclosure-AbC-1", cause_available_at=datetime(2026, 10, 2, 10, 0, 0, 250000, tzinfo=PLUS3), policy_key="private.scheduler.v3",
        policy_revision=12))


def calendar_admission():
    recurrence = build_private_scheduler_recurrence(
        schedule_key="private.schedule.cal", schedule_revision=2, timezone_key="Europe/Istanbul", local_time=time(9, 55), ambiguous_time_policy=AP.EARLIER,
        recurrence_kind=PrivateSchedulerRecurrenceKind.DAILY, weekdays=(), day_of_month=None, work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH, scope=SC.PORTFOLIO,
        owner_id=OWNER, portfolio_id=PORTFOLIO, policy_key="private.scheduler.v1", policy_revision=3)
    decision = evaluate_private_scheduler_civil_date(recurrence=recurrence, local_date=DAY)
    evidence = build_private_scheduler_calendar_date_evidence(
        calendar_key="exchange.test.core", calendar_revision=4, calendar_kind=CK.EXCHANGE_SESSION, timezone_key="Europe/Istanbul", local_date=DAY,
        coverage=CV.COMPLETE_FOR_DATE,
        exchange_sessions=(PrivateSchedulerExchangeSessionWindow(opens_at=datetime(2026, 10, 2, 7, tzinfo=UTC), closes_at=datetime(2026, 10, 2, 15, tzinfo=UTC)),),
        source_releases=(), source_key="source.test", published_at=None, observed_at=datetime(2026, 10, 2, 3, tzinfo=UTC), source_content_sha256="a" * 64)
    constraint = build_private_scheduler_calendar_constraint(calendar_key="exchange.test.core", calendar_revision=4, calendar_kind=CK.EXCHANGE_SESSION,
                                                             applicability_mode=MODE.EXCHANGE_SESSION_EXISTS, release_key=None)
    binding = bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=datetime(2026, 10, 2, 7, 50, tzinfo=PLUS3))
    return admit_private_scheduler_calendar_applicability(applicability=evaluate_private_scheduler_calendar_applicability(
        decision=decision, calendar_binding=binding, constraint=constraint))


ADMISSIONS = {"scheduled": scheduled_admission, "system": system_admission, "event": event_admission, "calendar": calendar_admission}


def not_before(admission) -> datetime:
    trigger = admission.trigger
    return (trigger.scheduled_for or trigger.cause_available_at).astimezone(UTC)


def minutes(base: datetime, n: int) -> datetime:
    return base + timedelta(minutes=n)


# --- chain of real closed transitions ----------------------------------------------------------------------------

def chain(admission):
    nb = not_before(admission)
    initial = initialize_private_scheduler_run_lifecycle(admission=admission)
    claim = claim_private_scheduler_run(lifecycle=initial.after, expected_version=1, claim_key="claim-a", claimed_at=nb, lease_expires_at=minutes(nb, 10))
    renew = renew_private_scheduler_run_claim(lifecycle=claim.after, expected_version=2, claim_key="claim-a", renewed_at=minutes(nb, 4),
                                              lease_expires_at=minutes(nb, 20))
    takeover = take_over_expired_private_scheduler_run_claim(lifecycle=renew.after, expected_version=3, claim_key="claim-b", claimed_at=minutes(nb, 20),
                                                             lease_expires_at=minutes(nb, 40))
    succeed = succeed_private_scheduler_run(lifecycle=takeover.after, expected_version=4, claim_key="claim-b", terminal_at=minutes(nb, 21))
    fail = fail_private_scheduler_run(lifecycle=claim.after, expected_version=2, claim_key="claim-a", terminal_at=minutes(nb, 5), failure_code="engine_error")
    return {"initialize": (initial, None), "claim": (claim, nb), "renew": (renew, minutes(nb, 4)), "takeover": (takeover, minutes(nb, 20)),
            "succeed": (succeed, minutes(nb, 21)), "fail": (fail, minutes(nb, 5))}


def ts(value: datetime | None, style: str = "iso") -> str | None:
    if value is None:
        return None
    utc = value.astimezone(UTC)
    if style == "z":
        return utc.strftime("%Y-%m-%dT%H:%M:%S") + (f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else "") + "Z"
    if style == "plus3":
        return utc.astimezone(PLUS3).isoformat()
    return utc.isoformat()


def lifecycle_columns(lifecycle: PrivateSchedulerRunLifecycle, style: str = "iso") -> dict:
    return {"state": lifecycle.state.value, "state_version": lifecycle.state_version, "claim_key": lifecycle.claim_key, "claimed_at": ts(lifecycle.claimed_at, style),
            "lease_expires_at": ts(lifecycle.lease_expires_at, style), "terminal_at": ts(lifecycle.terminal_at, style), "failure_code": lifecycle.failure_code}


def run_row(admission, lifecycle: PrivateSchedulerRunLifecycle | None = None, style: str = "iso") -> dict:
    trigger = admission.trigger
    lifecycle = lifecycle or initialize_private_scheduler_run_lifecycle(admission=admission).after
    created = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)
    return {
        "run_idempotency_sha256": admission.run_idempotency_sha256, "admission_source": admission.source.value, "trigger_kind": trigger.trigger_kind.value,
        "work_kind": trigger.work_kind.value, "scope": trigger.scope.value,
        "owner_id": None if trigger.owner_id is None else str(trigger.owner_id),
        "portfolio_id": None if trigger.portfolio_id is None else str(trigger.portfolio_id),
        "scheduled_for": ts(trigger.scheduled_for, style),
        "event_cause_kind": None if trigger.event_cause_kind is None else trigger.event_cause_kind.value,
        "cause_key": trigger.cause_key, "cause_available_at": ts(trigger.cause_available_at, style),
        "policy_key": trigger.policy_key, "policy_revision": trigger.policy_revision,
        "admission_payload": serialize_private_scheduler_run_admission(admission=admission),
        **lifecycle_columns(lifecycle, style),
        "created_at": ts(created, style), "updated_at": ts(created + timedelta(minutes=30), style),
    }


def history_row(transition, transition_at, style: str = "iso") -> dict:
    after = transition.after
    return {
        "run_idempotency_sha256": after.admission.run_idempotency_sha256, "after_state_version": after.state_version, "transition_kind": transition.kind.value,
        "before_state_version": None if transition.before is None else transition.before.state_version, "transition_at": ts(transition_at, style),
        "after_state": after.state.value, "claim_key": after.claim_key, "claimed_at": ts(after.claimed_at, style),
        "lease_expires_at": ts(after.lease_expires_at, style), "terminal_at": ts(after.terminal_at, style), "failure_code": after.failure_code,
        "recorded_at": ts(datetime(2026, 10, 2, 8, 0, 0, 5, tzinfo=UTC), style),
    }


def hydrate_run(row):
    return hydrate_private_scheduler_persisted_run(row=row)


def hydrate_history(row, admission):
    return hydrate_private_scheduler_persisted_transition(row=row, admission=admission)


def fails_run(row):
    with pytest.raises((ValueError, TypeError)):
        hydrate_run(row)


# --- projections / shapes ----------------------------------------------------------------------------------------

def migration_columns(table: str) -> list[str]:
    sql = re.sub(r"--[^\n]*", " ", MIGRATION.read_text(encoding="utf-8"))
    body = sql[sql.index(f"CREATE TABLE public.{table} (") + len(f"CREATE TABLE public.{table} ("):]
    names = []
    for line in body.splitlines():
        match = re.match(r"\s{4}([a-z_0-9]+)\s+(VARCHAR|TEXT|UUID|BIGINT|JSONB|TIMESTAMPTZ)\b", line)
        if match:
            names.append(match.group(1))
        if line.startswith(");"):
            break
    return names


def test_select_projections_are_explicit_and_match_migration_024() -> None:
    assert PRIVATE_SCHEDULER_RUN_SELECT.split(",") == RUN_COLUMNS
    assert PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT.split(",") == HISTORY_COLUMNS
    for constant, columns in ((PRIVATE_SCHEDULER_RUN_SELECT, RUN_COLUMNS), (PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT, HISTORY_COLUMNS)):
        assert type(constant) is str and "*" not in constant and " " not in constant and len(set(constant.split(","))) == len(columns)
    assert migration_columns("private_scheduler_runs") == RUN_COLUMNS
    assert migration_columns("private_scheduler_run_transitions") == HISTORY_COLUMNS


def test_persisted_object_shapes_and_function_signatures() -> None:
    assert [f.name for f in dataclasses.fields(PrivateSchedulerPersistedRun)] == ["lifecycle", "created_at", "updated_at"]
    assert [f.name for f in dataclasses.fields(PrivateSchedulerPersistedTransition)] == ["kind", "before_state_version", "transition_at", "after", "recorded_at"]
    for cls in (PrivateSchedulerPersistedRun, PrivateSchedulerPersistedTransition):
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in dataclasses.fields(cls))
    with pytest.raises(dataclasses.FrozenInstanceError):
        hydrate_run(run_row(scheduled_admission())).created_at = datetime(2026, 1, 1, tzinfo=UTC)  # type: ignore[misc]
    for function, names in ((hydrate_private_scheduler_persisted_run, ["row"]), (hydrate_private_scheduler_persisted_transition, ["row", "admission"])):
        parameters = list(inspect.signature(function).parameters.values())
        assert [p.name for p in parameters] == names
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    assert not hasattr(PrivateSchedulerPersistedRun, "run_idempotency_sha256")                  # the hash stays reachable via lifecycle.admission only


# --- row structure -----------------------------------------------------------------------------------------------

def test_row_may_be_any_mapping_but_must_have_exactly_the_projection_keys() -> None:
    admission = scheduled_admission()
    row = run_row(admission)
    for candidate in (row, OrderedDict(row), MappingProxyType(row)):
        assert hydrate_run(candidate).lifecycle.admission == admission
    for bad in (None, "x", b"x", list(row.items()), tuple(row.items()), set(row), 1):
        with pytest.raises((TypeError, ValueError)):
            hydrate_run(bad)
    for key in RUN_COLUMNS:
        missing = dict(row)
        del missing[key]
        fails_run(missing)
    fails_run({**row, "unexpected": 1})
    fails_run({**{k: v for k, v in row.items() if k != "state"}, 1: "ready"})
    fails_run({**{k: v for k, v in row.items() if k != "state"}, b"state": "ready"})
    renamed = dict(row)
    renamed["status"] = renamed.pop("state")
    fails_run(renamed)


def test_history_row_structure() -> None:
    admission = scheduled_admission()
    transition, at = chain(admission)["claim"]
    row = history_row(transition, at)
    assert hydrate_history(row, admission).kind is KIND.CLAIM
    for bad in (None, "x", list(row.items()), 1):
        with pytest.raises((TypeError, ValueError)):
            hydrate_history(bad, admission)
    for key in HISTORY_COLUMNS:
        missing = dict(row)
        del missing[key]
        with pytest.raises((TypeError, ValueError)):
            hydrate_history(missing, admission)
    for extra in ({**row, "unexpected": 1}, {**{k: v for k, v in row.items() if k != "claim_key"}, 5: "x"}):
        with pytest.raises((TypeError, ValueError)):
            hydrate_history(extra, admission)
    for bad in (None, object(), admission.trigger, dict(row)):
        with pytest.raises(TypeError):
            hydrate_history(row, bad)  # type: ignore[arg-type]

    class SubAdmission(PrivateSchedulerRunAdmission):
        pass

    sub = SubAdmission(**{f.name: getattr(admission, f.name) for f in dataclasses.fields(admission)})
    with pytest.raises(TypeError):
        hydrate_history(row, sub)


# --- current-run hydration ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_every_admission_source_hydrates_and_reconciles(name: str) -> None:
    admission = ADMISSIONS[name]()
    persisted = hydrate_run(run_row(admission))
    assert type(persisted) is PrivateSchedulerPersistedRun and persisted.lifecycle.admission == admission
    assert persisted.lifecycle.admission is not admission                                         # reconstructed from the payload, never reused
    assert persisted.lifecycle.admission.source is admission.source
    assert persisted.lifecycle.admission.run_idempotency_sha256 == admission.run_idempotency_sha256
    assert persisted.lifecycle.state is STATE.READY and persisted.lifecycle.state_version == 1
    assert persisted.created_at.tzinfo is UTC and persisted.updated_at.tzinfo is UTC and persisted.created_at <= persisted.updated_at
    assert persisted.created_at == datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_all_four_lifecycle_states_hydrate(name: str) -> None:
    admission = ADMISSIONS[name]()
    steps = chain(admission)
    for key, state in (("initialize", STATE.READY), ("claim", STATE.CLAIMED), ("renew", STATE.CLAIMED), ("takeover", STATE.CLAIMED),
                       ("succeed", STATE.SUCCEEDED), ("fail", STATE.FAILED)):
        lifecycle = steps[key][0].after
        persisted = hydrate_run(run_row(admission, lifecycle))
        assert persisted.lifecycle.state is state
        rebuilt = persisted.lifecycle
        assert (rebuilt.state_version, rebuilt.claim_key, rebuilt.claimed_at, rebuilt.lease_expires_at, rebuilt.terminal_at, rebuilt.failure_code) == (
            lifecycle.state_version, lifecycle.claim_key, lifecycle.claimed_at, lifecycle.lease_expires_at, lifecycle.terminal_at, lifecycle.failure_code)
        for value in (rebuilt.claimed_at, rebuilt.lease_expires_at, rebuilt.terminal_at):
            assert value is None or value.tzinfo is UTC


def test_malformed_lifecycle_rows_are_rejected_by_the_closed_lifecycle_authority() -> None:
    admission = scheduled_admission()
    steps = chain(admission)
    base = run_row(admission)
    claimed = run_row(admission, steps["claim"][0].after)
    done = run_row(admission, steps["succeed"][0].after)
    failed = run_row(admission, steps["fail"][0].after)
    nb = not_before(admission)
    cases = [
        {**base, "state_version": 2},
        {**base, "claim_key": "c"},
        {**base, "claimed_at": ts(nb)},
        {**claimed, "lease_expires_at": None},
        {**claimed, "lease_expires_at": claimed["claimed_at"]},
        {**claimed, "claim_key": None},
        {**claimed, "state_version": 1},
        {**done, "failure_code": "engine_error"},
        {**done, "terminal_at": None},
        {**done, "terminal_at": done["lease_expires_at"]},
        {**failed, "failure_code": None},
        {**failed, "terminal_at": failed["lease_expires_at"]},
        {**failed, "state_version": 2},
        {**base, "state": "pending"},
        {**base, "state": "READY"},
        {**base, "state": "running"},
        {**base, "state_version": True},
        {**base, "state_version": 1.0},
        {**base, "state_version": "1"},
        {**claimed, "claimed_at": ts(minutes(nb, 11))},
    ]
    for row in cases:
        fails_run(row)


def test_exact_transport_types_are_enforced_without_coercion() -> None:
    admission = scheduled_admission()
    owner_admission = event_admission()
    base = run_row(admission)
    owner_row = run_row(owner_admission)
    for key, bad in (("state_version", 1.0), ("state_version", "1"), ("state_version", True), ("policy_revision", True), ("policy_revision", 5.0),
                     ("policy_revision", "5"), ("claim_key", 1), ("failure_code", 1), ("run_idempotency_sha256", b"x"), ("admission_source", 1),
                     ("trigger_kind", None), ("work_kind", None), ("scope", 1), ("policy_key", None), ("cause_key", 1), ("event_cause_kind", 1)):
        fails_run({**base, key: bad})
    for key in ("scheduled_for", "created_at", "updated_at"):
        for bad in (datetime(2026, 10, 2, 7, 0, tzinfo=UTC), 20261002, b"2026-10-02T07:00:00Z", None if key != "scheduled_for" else "x"):
            fails_run({**base, key: bad})
    for bad in (OWNER, OWNER.int, {"x": 1}, "1", b"x"):
        fails_run({**base, "owner_id": bad})
    for bad in ("{" + str(OWNER) + "}", str(OWNER).replace("-", ""), str(UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")).upper(), " " + str(OWNER)):
        fails_run({**base, "owner_id": bad})
        fails_run({**base, "portfolio_id": bad})
    for bad in (None, "", "not-a-timestamp", "2026-10-02", "2026-10-02T07:00:00", "2026-13-01T00:00:00Z", 1):
        fails_run({**owner_row, "cause_available_at": bad})
    fails_run({**base, "admission_payload": None})
    fails_run({**base, "admission_payload": "{}"})
    fails_run({**base, "admission_payload": [base["admission_payload"]]})


# --- immutable column reconciliation -----------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_every_denormalized_column_must_agree_with_the_payload(name: str) -> None:
    admission = ADMISSIONS[name]()
    row = run_row(admission)
    trigger = admission.trigger
    replacements = {
        "run_idempotency_sha256": "f" * 64,
        "admission_source": "event_driven" if admission.source.value != "event_driven" else "scheduled_direct",
        "trigger_kind": "event_driven" if trigger.trigger_kind.value == "scheduled" else "scheduled",
        "work_kind": "source_data_refresh" if trigger.work_kind.value != "source_data_refresh" else "game_changer_review",
        "scope": "system" if trigger.scope.value == "portfolio" else "portfolio",
        "owner_id": str(UUID(int=9)) if trigger.owner_id is not None else str(OWNER),
        "portfolio_id": str(UUID(int=9)) if trigger.portfolio_id is not None else str(PORTFOLIO),
        "policy_key": "private.scheduler.other",
        "policy_revision": trigger.policy_revision + 1,
    }
    if trigger.scheduled_for is not None:
        replacements["scheduled_for"] = ts(trigger.scheduled_for + timedelta(microseconds=1))
        replacements["event_cause_kind"] = "disclosure_ingested"
        replacements["cause_key"] = "x"
        replacements["cause_available_at"] = ts(trigger.scheduled_for)
    else:
        replacements["scheduled_for"] = ts(trigger.cause_available_at)
        replacements["event_cause_kind"] = "macro_release_ingested"
        replacements["cause_key"] = "kap-disclosure-abc-1"
        replacements["cause_available_at"] = ts(trigger.cause_available_at + timedelta(microseconds=1))
    assert set(replacements) == set(IMMUTABLE)
    for key, value in replacements.items():
        mutated = dict(row)
        mutated[key] = value
        fails_run(mutated)
    for key in IMMUTABLE:                                                                           # a NULL where a value is required (and the reverse)
        if row[key] is not None:
            fails_run({**row, key: None})


def test_payload_mutation_with_unchanged_columns_is_rejected_or_documented() -> None:
    admission = event_admission()
    row = run_row(admission)
    for path, value in ((("provenance", "event_occurrence", "cause_key"), "other-key"), (("provenance", "event_occurrence", "policy_revision"), 13),
                        (("provenance", "event_occurrence", "owner_id"), str(UUID(int=9))), (("run_idempotency_sha256",), "f" * 64),
                        (("source",), "scheduled_direct")):
        mutated = copy.deepcopy(row)
        target = mutated["admission_payload"]
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
        fails_run(mutated)
    calendar = calendar_admission()
    row = run_row(calendar)
    mutated = copy.deepcopy(row)
    mutated["admission_payload"]["provenance"]["calendar_binding"]["evidence"]["source_content_sha256"] = "c" * 64
    changed = hydrate_run(mutated)                                                                  # a non-identity audit field: no payload hash exists
    assert changed.lifecycle.admission != calendar and changed.lifecycle.admission.run_idempotency_sha256 == calendar.run_idempotency_sha256


@pytest.mark.parametrize("style", ["iso", "z", "plus3"])
def test_same_instant_in_any_sql_offset_is_accepted_and_canonicalized_to_utc(style: str) -> None:
    for name in ("scheduled", "event", "calendar"):
        admission = ADMISSIONS[name]()
        steps = chain(admission)
        lifecycle = steps["renew"][0].after
        persisted = hydrate_run(run_row(admission, lifecycle, style=style))
        assert persisted.lifecycle == lifecycle                                                       # equal instants, canonical UTC
        assert persisted.lifecycle.claimed_at.tzinfo is UTC and persisted.lifecycle.lease_expires_at.tzinfo is UTC
        assert persisted.created_at == datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC) and persisted.created_at.tzinfo is UTC


def test_event_audit_offset_survives_in_the_payload_while_sql_is_canonical_utc() -> None:
    admission = event_admission()
    assert admission.trigger.cause_available_at.utcoffset() == timedelta(hours=3)
    for style in ("iso", "z"):
        row = run_row(admission, style=style)
        assert row["cause_available_at"].startswith("2026-10-02T07:00:00") and row["cause_available_at"] != "2026-10-02T10:00:00.250000+03:00"
        assert row["admission_payload"]["provenance"]["event_occurrence"]["cause_available_at"] == "2026-10-02T10:00:00.250000+03:00"
        persisted = hydrate_run(row)
        assert persisted.lifecycle.admission.trigger.cause_available_at.utcoffset() == timedelta(hours=3)         # the audit representation is retained
        assert persisted.lifecycle.admission == admission


def test_sql_transport_precision_variants_for_the_same_instant() -> None:
    admission = system_admission()
    base = run_row(admission)
    instant = admission.trigger.scheduled_for
    for text in ("2026-10-02T06:55:00Z", "2026-10-02T06:55:00+00:00", "2026-10-02T06:55:00.000000+00:00", "2026-10-02T09:55:00+03:00",
                 "2026-10-02T06:55:00.000+00:00"):
        persisted = hydrate_run({**base, "scheduled_for": text})
        assert persisted.lifecycle.admission == admission and instant.tzinfo is UTC
    for text in ("2026-10-02T06:55:00.000001Z", "2026-10-02T06:54:59.999999+00:00", "2026-10-02T06:55:00+03:00"):
        fails_run({**base, "scheduled_for": text})
    for key in ("created_at", "updated_at"):
        for text in ("2026-10-01T12:00:00Z", "2026-10-01T12:00:00.5+00:00", "2026-10-01T15:00:00+03:00"):
            persisted = hydrate_run({**base, key: text, **({"updated_at": "2026-10-02T00:00:00Z"} if key == "created_at" else {})}) if key == "created_at" else \
                hydrate_run({**base, "created_at": "2026-10-01T00:00:00Z", key: text})
            assert persisted.created_at.tzinfo is UTC and persisted.updated_at.tzinfo is UTC


def test_database_metadata_clocks_must_be_ordered_and_are_not_identity() -> None:
    admission = scheduled_admission()
    base = run_row(admission)
    fails_run({**base, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"})
    equal = hydrate_run({**base, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"})
    assert equal.created_at == equal.updated_at
    other = hydrate_run({**base, "created_at": "2020-01-01T00:00:00Z", "updated_at": "2030-01-01T00:00:00Z"})
    assert other.lifecycle == hydrate_run(base).lifecycle                                           # metadata never participates in identity


# --- history hydration -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ADMISSIONS))
def test_all_six_transition_kinds_hydrate(name: str) -> None:
    admission = ADMISSIONS[name]()
    steps = chain(admission)
    expected = {"initialize": KIND.INITIALIZE, "claim": KIND.CLAIM, "renew": KIND.RENEW_CLAIM, "takeover": KIND.TAKE_OVER_EXPIRED_CLAIM,
                "succeed": KIND.SUCCEED, "fail": KIND.FAIL}
    for key, kind in expected.items():
        transition, at = steps[key]
        for style in ("iso", "z", "plus3"):
            persisted = hydrate_history(history_row(transition, at, style), admission)
            assert type(persisted) is PrivateSchedulerPersistedTransition and persisted.kind is kind
            assert persisted.after == transition.after and persisted.after.admission is admission
            assert persisted.before_state_version == (None if transition.before is None else transition.before.state_version)
            assert persisted.transition_at == (None if at is None or key == "initialize" else at)
            assert persisted.recorded_at == datetime(2026, 10, 2, 8, 0, 0, 5, tzinfo=UTC) and persisted.recorded_at.tzinfo is UTC
            assert persisted.transition_at is None or persisted.transition_at.tzinfo is UTC


def test_renewal_transition_at_is_preserved_separately_from_the_original_claim() -> None:
    admission = scheduled_admission()
    renew, at = chain(admission)["renew"]
    nb = not_before(admission)
    persisted = hydrate_history(history_row(renew, at), admission)
    assert persisted.after.claimed_at == nb and persisted.transition_at == minutes(nb, 4) and persisted.after.lease_expires_at == minutes(nb, 20)
    assert persisted.transition_at != persisted.after.claimed_at                                    # the renewed_at instant the closed record cannot hold
    for bad in (minutes(nb, -1), minutes(nb, 4) - timedelta(minutes=5)):
        with pytest.raises(ValueError):
            hydrate_history(history_row(renew, bad), admission)                                    # before the original claim
    assert hydrate_history(history_row(renew, nb), admission).transition_at == nb                   # equality with claimed_at is allowed for a renewal


def test_transition_at_relations_for_the_other_kinds() -> None:
    admission = event_admission()
    steps = chain(admission)
    for key in ("claim", "takeover", "succeed", "fail"):
        transition, at = steps[key]
        for delta in (timedelta(microseconds=1), -timedelta(microseconds=1)):
            with pytest.raises(ValueError):
                hydrate_history(history_row(transition, at + delta), admission)                    # must equal claimed_at / terminal_at exactly
    initial, _ = steps["initialize"]
    with pytest.raises(ValueError):
        hydrate_history(history_row(initial, not_before(admission)), admission)                    # INITIALIZE has no transition_at
    claim, at = steps["claim"]
    with pytest.raises(ValueError):
        hydrate_history(history_row(claim, None), admission)                                        # non-initial needs one


def test_history_shape_and_version_step_rules() -> None:
    admission = scheduled_admission()
    steps = chain(admission)
    claim, at = steps["claim"]
    base = history_row(claim, at)
    initial = history_row(*steps["initialize"])
    cases = [
        {**base, "before_state_version": 3}, {**base, "before_state_version": None}, {**base, "before_state_version": 0}, {**base, "before_state_version": True},
        {**base, "before_state_version": 1.0}, {**base, "after_state_version": 3}, {**base, "after_state_version": "2"}, {**base, "after_state_version": True},
        {**initial, "before_state_version": 1}, {**initial, "after_state_version": 2}, {**initial, "after_state": "claimed"},
        {**base, "transition_kind": "retry"}, {**base, "transition_kind": "CLAIM"}, {**base, "transition_kind": None}, {**base, "transition_kind": KIND.CLAIM},
        {**base, "after_state": "pending"}, {**base, "run_idempotency_sha256": "f" * 64}, {**base, "run_idempotency_sha256": None},
        {**base, "claim_key": None}, {**base, "recorded_at": None}, {**base, "recorded_at": "not-a-timestamp"}, {**base, "transition_at": "2026-10-02T07:00:00"},
        {**base, "transition_kind": "succeed"}, {**base, "transition_kind": "renew_claim", "transition_at": ts(minutes(not_before(admission), -1))},
    ]
    for row in cases:
        with pytest.raises((ValueError, TypeError)):
            hydrate_history(row, admission)
    assert hydrate_history({**base, "recorded_at": ts(datetime(2000, 1, 1, tzinfo=UTC))}, admission).kind is KIND.CLAIM      # recorded_at is metadata only


def test_persisted_objects_validate_on_direct_construction() -> None:
    admission = scheduled_admission()
    steps = chain(admission)
    claim, at = steps["claim"]
    good = hydrate_history(history_row(claim, at), admission)
    assert dataclasses.replace(good) == good
    plus3 = at.astimezone(PLUS3)
    for change in (dict(transition_at=plus3), dict(recorded_at=datetime(2026, 10, 2, 8, 0)), dict(kind="claim"), dict(after=object()),
                   dict(before_state_version=True), dict(transition_at=None), dict(before_state_version=5), dict(kind=KIND.SUCCEED)):
        with pytest.raises((ValueError, TypeError)):
            dataclasses.replace(good, **change)
    run = hydrate_run(run_row(admission))
    for change in (dict(created_at=run.updated_at + timedelta(seconds=1)), dict(created_at=run.created_at.astimezone(PLUS3)), dict(lifecycle=object()),
                   dict(updated_at=datetime(2026, 10, 2))):
        with pytest.raises((ValueError, TypeError)):
            dataclasses.replace(run, **change)


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(transport.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_run_persistence_transport.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_scheduler_surfaces_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "collections.abc", "dataclasses", "datetime", "uuid", "backend.engine.private.scheduler_run_persistence_codec",
                     "backend.engine.private.scheduler_run_admission", "backend.engine.private.scheduler_run_lifecycle",
                     "backend.engine.private.scheduler_trigger"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)
    assert imported["backend.engine.private.scheduler_run_persistence_codec"] == {"hydrate_private_scheduler_run_admission"}
    for forbidden in ("supabase", "postgrest", "httpx", "requests", "asyncio", "fastapi", "redis", "infrastructure", "job_queue", "hashlib", "json", "random",
                      "secrets", "pickle", "portfolio", "game_changer", "allocation", "rebalance"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_hydration_delegates_to_the_closed_codec_and_lifecycle_and_calls_no_database() -> None:
    calls = {getattr(n.func, "id", getattr(n.func, "attr", "")) for n in ast.walk(_TREE) if isinstance(n, ast.Call)}
    assert "hydrate_private_scheduler_run_admission" in calls and "PrivateSchedulerRunLifecycle" in calls
    assert not calls & {"build_private_scheduler_trigger", "PrivateSchedulerTrigger", "PrivateSchedulerRunAdmission", "serialize_private_scheduler_run_admission"}
    assert not calls & {"select", "insert", "update", "delete", "upsert", "rpc", "table", "execute", "from_", "request", "get", "post"}
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "monotonic", "sleep", "timestamp", "uuid4", "uuid1", "random", "sha256", "hexdigest", "dumps", "loads",
                        "float", "client", "rpc", "upsert"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]


def test_documents_the_transport_boundaries() -> None:
    doc = transport.__doc__ or ""
    for needle in ("TIMESTAMPTZ", "payload", "denormalized", "UTC", "transition_at", "renew", "no database", "no payload hash", "24D2B2", "audit"):
        assert needle in doc, needle


# --- Phase 24D2B1.R1: causal-shape hardening ---------------------------------------------------------------------

def test_renew_and_takeover_cannot_be_version_two_transitions() -> None:
    admission = scheduled_admission()
    steps = chain(admission)
    for key in ("renew", "takeover"):
        transition, at = steps[key]
        row = history_row(transition, at)
        with pytest.raises(ValueError):
            hydrate_history({**row, "before_state_version": 1, "after_state_version": 2}, admission)


def test_positive_version_boundaries_for_claim_renew_takeover() -> None:
    admission = scheduled_admission()
    steps = chain(admission)
    claim, claim_at = steps["claim"]
    assert hydrate_history(history_row(claim, claim_at), admission).after.state_version == 2
    for key in ("renew", "takeover"):
        transition, at = steps[key]
        row = history_row(transition, at)
        assert hydrate_history(row, admission).before_state_version == row["after_state_version"] - 1 and row["before_state_version"] == (2 if key == "renew" else 3)
        assert hydrate_history({**row, "before_state_version": 2, "after_state_version": 3}, admission).after.state_version == 3
        for before in (3, 4, 9):
            assert hydrate_history({**row, "before_state_version": before, "after_state_version": before + 1}, admission).before_state_version == before


def test_renew_transition_at_must_precede_the_new_lease_expiry() -> None:
    admission = scheduled_admission()
    renew, at = chain(admission)["renew"]
    expiry = renew.after.lease_expires_at
    for bad in (expiry, expiry + timedelta(microseconds=1), expiry + timedelta(minutes=5)):
        with pytest.raises(ValueError):
            hydrate_history(history_row(renew, bad), admission)
    edge = expiry - timedelta(microseconds=1)
    assert hydrate_history(history_row(renew, edge), admission).transition_at == edge
    assert hydrate_history(history_row(renew, renew.after.claimed_at), admission).transition_at == renew.after.claimed_at
