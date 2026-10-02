"""
backend/tests/test_scheduler_run_admission.py
=============================================
Phase 24C2A: unified run admission. Only closed wrapper authorities (24B1 scheduled occurrence, 24B3B APPLICABLE calendar result, 24C1 event
occurrence) can become a run candidate; a raw 24A trigger can never be admitted. The run idempotency identity is exactly the occurrence trigger
hash (no second hash), and the admission path is audit provenance, not identity. No lifecycle, claim, dispatch or persistence.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, time, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_run_admission as module_under_test
from backend.engine.private.scheduler_calendar_applicability import (
    PrivateSchedulerCalendarApplicability,
    PrivateSchedulerCalendarApplicabilityMode,
    PrivateSchedulerCalendarApplicabilityStatus,
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
from backend.engine.private.scheduler_event_occurrence import PrivateSchedulerEventOccurrence, build_private_scheduler_event_occurrence
from backend.engine.private.scheduler_recurrence import (
    PrivateSchedulerRecurrenceKind,
    build_private_scheduler_recurrence,
    evaluate_private_scheduler_civil_date,
)
from backend.engine.private.scheduler_run_admission import (
    PrivateSchedulerRunAdmission,
    PrivateSchedulerRunAdmissionSource,
    admit_private_scheduler_calendar_applicability,
    admit_private_scheduler_event_occurrence,
    admit_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_scheduled_occurrence import (
    PrivateSchedulerAmbiguousTimePolicy,
    PrivateSchedulerScheduledOccurrence,
    build_private_scheduler_scheduled_occurrence,
)
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerTrigger,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
    build_private_scheduler_trigger,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
SRC = PrivateSchedulerRunAdmissionSource
ST = PrivateSchedulerCalendarApplicabilityStatus
CV = PrivateSchedulerCalendarCoverage
WK, SC, EC, TK = PrivateSchedulerWorkKind, PrivateSchedulerScope, PrivateSchedulerEventCauseKind, PrivateSchedulerTriggerKind
AP = PrivateSchedulerAmbiguousTimePolicy
DAY = date(2026, 10, 2)
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)


def U(h, mi=0) -> datetime:
    return datetime(2026, 10, 2, h, mi, tzinfo=UTC)


def scheduled(**changes) -> PrivateSchedulerScheduledOccurrence:
    base = dict(schedule_key="private.schedule.test", schedule_revision=1, timezone_key="Europe/Istanbul", local_date=DAY, local_time=time(9, 55),
                ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
                policy_key="private.scheduler.v1", policy_revision=1)
    base.update(changes)
    return build_private_scheduler_scheduled_occurrence(**base)


def applicability(coverage=CV.COMPLETE_FOR_DATE, sessions=(PrivateSchedulerExchangeSessionWindow(opens_at=U(7), closes_at=U(15)),),
                  **changes) -> PrivateSchedulerCalendarApplicability:
    recurrence = build_private_scheduler_recurrence(
        schedule_key=changes.get("schedule_key", "private.schedule.test"), schedule_revision=1, timezone_key="Europe/Istanbul", local_time=time(9, 55),
        ambiguous_time_policy=AP.REJECT, recurrence_kind=PrivateSchedulerRecurrenceKind.DAILY, weekdays=(), day_of_month=None,
        work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None, policy_key="private.scheduler.v1", policy_revision=1)
    decision = evaluate_private_scheduler_civil_date(recurrence=recurrence, local_date=DAY)
    evidence = build_private_scheduler_calendar_date_evidence(
        calendar_key="exchange.test.core", calendar_revision=1, calendar_kind=PrivateSchedulerCalendarKind.EXCHANGE_SESSION,
        timezone_key="Europe/Istanbul", local_date=DAY, coverage=coverage, exchange_sessions=tuple(sessions) if coverage is CV.COMPLETE_FOR_DATE else (),
        source_releases=(), source_key="source.test", published_at=None, observed_at=U(3), source_content_sha256="a" * 64)
    constraint = build_private_scheduler_calendar_constraint(
        calendar_key="exchange.test.core", calendar_revision=1, calendar_kind=PrivateSchedulerCalendarKind.EXCHANGE_SESSION,
        applicability_mode=PrivateSchedulerCalendarApplicabilityMode.EXCHANGE_SESSION_EXISTS, release_key=None)
    return evaluate_private_scheduler_calendar_applicability(
        decision=decision, calendar_binding=bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=U(5)), constraint=constraint)


def event(**changes) -> PrivateSchedulerEventOccurrence:
    base = dict(work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, event_cause_kind=EC.DISCLOSURE_INGESTED,
                cause_key="Evt-1", cause_available_at=U(7), policy_key="private.scheduler.v1", policy_revision=1)
    base.update(changes)
    return build_private_scheduler_event_occurrence(**base)


def raw_scheduled(hash_value="a" * 64) -> PrivateSchedulerTrigger:
    return build_private_scheduler_trigger(
        trigger_kind=TK.SCHEDULED, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None, scheduled_for=U(7),
        event_cause_kind=None, cause_key=None, cause_available_at=None, policy_key="private.scheduler.v1", policy_revision=1, idempotency_sha256=hash_value)


def raw_event(hash_value="b" * 64) -> PrivateSchedulerTrigger:
    return build_private_scheduler_trigger(
        trigger_kind=TK.EVENT_DRIVEN, work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, scheduled_for=None,
        event_cause_kind=EC.DISCLOSURE_INGESTED, cause_key="Evt-1", cause_available_at=U(7), policy_key="private.scheduler.v1", policy_revision=1,
        idempotency_sha256=hash_value)


def fields_of(admission: PrivateSchedulerRunAdmission) -> dict:
    return {f.name: getattr(admission, f.name) for f in dataclasses.fields(admission)}


def forge(good: PrivateSchedulerRunAdmission, **changes) -> PrivateSchedulerRunAdmission:
    return PrivateSchedulerRunAdmission(**{**fields_of(good), **changes})


# --- shapes ------------------------------------------------------------------------------------------------------

def test_enum_stored_fields_and_builder_shapes() -> None:
    assert [(m.name, m.value) for m in SRC] == [("SCHEDULED_DIRECT", "scheduled_direct"),
                                                ("SCHEDULED_CALENDAR_APPLICABLE", "scheduled_calendar_applicable"), ("EVENT_DRIVEN", "event_driven")]
    fields = dataclasses.fields(PrivateSchedulerRunAdmission)
    assert [f.name for f in fields] == ["source", "scheduled_occurrence", "calendar_applicability", "event_occurrence", "trigger", "run_idempotency_sha256"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        admit_private_scheduler_scheduled_occurrence(occurrence=scheduled()).source = SRC.EVENT_DRIVEN  # type: ignore[misc]
    assert not {"admitted_at", "created_at", "received_at", "run_id", "job_id", "attempt", "state", "status"} & {f.name for f in fields}
    for function, name in ((admit_private_scheduler_scheduled_occurrence, "occurrence"), (admit_private_scheduler_calendar_applicability, "applicability"),
                           (admit_private_scheduler_event_occurrence, "occurrence")):
        parameters = list(inspect.signature(function).parameters.values())
        assert [p.name for p in parameters] == [name]
        assert parameters[0].kind is inspect.Parameter.KEYWORD_ONLY and parameters[0].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        admit_private_scheduler_scheduled_occurrence(scheduled())  # type: ignore[misc]
    with pytest.raises(TypeError):
        admit_private_scheduler_event_occurrence(event())  # type: ignore[misc]
    with pytest.raises(TypeError):
        admit_private_scheduler_calendar_applicability(applicability())  # type: ignore[misc]
    public = {n for n in dir(module_under_test) if not n.startswith("_")}
    assert not [n for n in public if "trigger" in n.lower() and callable(getattr(module_under_test, n)) and n.startswith(("admit", "build"))]


# --- the three admission paths -----------------------------------------------------------------------------------

def test_scheduled_direct_admission() -> None:
    occurrence = scheduled()
    admission = admit_private_scheduler_scheduled_occurrence(occurrence=occurrence)
    assert admission.source is SRC.SCHEDULED_DIRECT
    assert admission.scheduled_occurrence is occurrence and admission.calendar_applicability is None and admission.event_occurrence is None
    assert admission.trigger is occurrence.trigger and admission.trigger.trigger_kind is TK.SCHEDULED
    assert admission.run_idempotency_sha256 == occurrence.trigger.idempotency_sha256


def test_calendar_applicable_admission_retains_the_proof() -> None:
    proof = applicability()
    assert proof.status is ST.APPLICABLE
    admission = admit_private_scheduler_calendar_applicability(applicability=proof)
    assert admission.source is SRC.SCHEDULED_CALENDAR_APPLICABLE
    assert admission.calendar_applicability is proof                                          # the audit proof, not only its candidate
    assert admission.scheduled_occurrence is None and admission.event_occurrence is None
    assert admission.trigger is proof.candidate_occurrence.trigger
    assert admission.run_idempotency_sha256 == proof.candidate_occurrence.trigger.idempotency_sha256


def test_not_applicable_and_unavailable_calendar_results_cannot_be_admitted() -> None:
    not_applicable = applicability(sessions=())
    unavailable = applicability(coverage=CV.UNAVAILABLE)
    assert (not_applicable.status, unavailable.status) == (ST.NOT_APPLICABLE, ST.UNAVAILABLE)
    for result in (not_applicable, unavailable):
        with pytest.raises(ValueError):
            admit_private_scheduler_calendar_applicability(applicability=result)


def test_event_driven_admission() -> None:
    occurrence = event()
    admission = admit_private_scheduler_event_occurrence(occurrence=occurrence)
    assert admission.source is SRC.EVENT_DRIVEN
    assert admission.event_occurrence is occurrence and admission.scheduled_occurrence is None and admission.calendar_applicability is None
    assert admission.trigger is occurrence.trigger and admission.trigger.trigger_kind is TK.EVENT_DRIVEN
    assert admission.run_idempotency_sha256 == occurrence.trigger.idempotency_sha256


def test_proof_path_is_provenance_not_logical_run_identity() -> None:
    proof = applicability()
    direct = admit_private_scheduler_scheduled_occurrence(occurrence=proof.candidate_occurrence)
    calendar = admit_private_scheduler_calendar_applicability(applicability=proof)
    assert direct.run_idempotency_sha256 == calendar.run_idempotency_sha256                  # one logical occurrence, one logical run identity
    assert direct.source is not calendar.source
    assert direct.scheduled_occurrence is proof.candidate_occurrence and direct.calendar_applicability is None
    assert calendar.calendar_applicability is proof and calendar.scheduled_occurrence is None
    assert direct != calendar


def test_distinct_occurrences_have_distinct_run_identities_and_repeats_are_equal() -> None:
    assert admit_private_scheduler_scheduled_occurrence(occurrence=scheduled()).run_idempotency_sha256 != \
           admit_private_scheduler_scheduled_occurrence(occurrence=scheduled(schedule_key="private.schedule.other")).run_idempotency_sha256
    assert admit_private_scheduler_event_occurrence(occurrence=event()).run_idempotency_sha256 != \
           admit_private_scheduler_event_occurrence(occurrence=event(cause_key="Evt-2")).run_idempotency_sha256
    occurrence = scheduled()
    assert admit_private_scheduler_scheduled_occurrence(occurrence=occurrence) == admit_private_scheduler_scheduled_occurrence(occurrence=occurrence)


# --- exact types / no raw trigger --------------------------------------------------------------------------------

def test_builders_require_exact_closed_wrappers() -> None:
    occurrence, proof, event_occurrence = scheduled(), applicability(), event()

    class SubScheduled(PrivateSchedulerScheduledOccurrence):
        pass

    class SubEvent(PrivateSchedulerEventOccurrence):
        pass

    class SubApplicability(PrivateSchedulerCalendarApplicability):
        pass

    sub_scheduled = SubScheduled(**{f.name: getattr(occurrence, f.name) for f in dataclasses.fields(occurrence)})
    sub_event = SubEvent(trigger=event_occurrence.trigger)
    sub_proof = SubApplicability(**{f.name: getattr(proof, f.name) for f in dataclasses.fields(proof)})
    for bad in (None, object(), occurrence.trigger, raw_scheduled(), event_occurrence, sub_scheduled):
        with pytest.raises(TypeError):
            admit_private_scheduler_scheduled_occurrence(occurrence=bad)  # type: ignore[arg-type]
    for bad in (None, object(), proof.candidate_occurrence, occurrence, sub_proof):
        with pytest.raises(TypeError):
            admit_private_scheduler_calendar_applicability(applicability=bad)  # type: ignore[arg-type]
    for bad in (None, object(), event_occurrence.trigger, raw_event(), occurrence, sub_event):
        with pytest.raises(TypeError):
            admit_private_scheduler_event_occurrence(occurrence=bad)  # type: ignore[arg-type]


def test_raw_24a_triggers_can_never_be_admitted() -> None:
    scheduled_trigger, event_trigger = raw_scheduled(), raw_event()
    for source, trigger in ((SRC.SCHEDULED_DIRECT, scheduled_trigger), (SRC.EVENT_DRIVEN, event_trigger), (SRC.SCHEDULED_CALENDAR_APPLICABLE, scheduled_trigger)):
        with pytest.raises(ValueError):                                                      # no wrapper: all-None provenance
            PrivateSchedulerRunAdmission(source=source, scheduled_occurrence=None, calendar_applicability=None, event_occurrence=None, trigger=trigger,
                                         run_idempotency_sha256=trigger.idempotency_sha256)
    for source, kwargs in ((SRC.SCHEDULED_DIRECT, dict(scheduled_occurrence=scheduled_trigger)), (SRC.EVENT_DRIVEN, dict(event_occurrence=event_trigger)),
                           (SRC.SCHEDULED_CALENDAR_APPLICABLE, dict(calendar_applicability=scheduled_trigger))):
        values = dict(scheduled_occurrence=None, calendar_applicability=None, event_occurrence=None)
        values.update(kwargs)
        with pytest.raises(TypeError):                                                       # a raw trigger is not an exact wrapper
            PrivateSchedulerRunAdmission(source=source, trigger=scheduled_trigger if source is not SRC.EVENT_DRIVEN else event_trigger,
                                         run_idempotency_sha256="a" * 64, **values)
    wrong_hash_event = event().trigger
    assert wrong_hash_event.idempotency_sha256 != event_trigger.idempotency_sha256            # arbitrary valid hash differs from the canonical one


# --- direct construction / forge resistance ----------------------------------------------------------------------

def test_discriminated_union_shape_is_strict() -> None:
    occurrence, proof, event_occurrence = scheduled(), applicability(), event()
    direct = admit_private_scheduler_scheduled_occurrence(occurrence=occurrence)
    calendar = admit_private_scheduler_calendar_applicability(applicability=proof)
    driven = admit_private_scheduler_event_occurrence(occurrence=event_occurrence)
    assert forge(direct) == direct and forge(calendar) == calendar and forge(driven) == driven
    mixed = [
        (direct, dict(calendar_applicability=proof)), (direct, dict(event_occurrence=event_occurrence)),
        (direct, dict(scheduled_occurrence=None)),
        (calendar, dict(scheduled_occurrence=occurrence)), (calendar, dict(event_occurrence=event_occurrence)),
        (calendar, dict(calendar_applicability=None)),
        (driven, dict(scheduled_occurrence=occurrence)), (driven, dict(calendar_applicability=proof)), (driven, dict(event_occurrence=None)),
    ]
    for good, change in mixed:
        with pytest.raises(ValueError):
            forge(good, **change)
    with pytest.raises(ValueError):                                                          # all None
        forge(direct, scheduled_occurrence=None)
    for source in SRC:                                                                       # source/shape disagreement
        if source is not SRC.SCHEDULED_DIRECT:
            with pytest.raises(ValueError):
                forge(direct, source=source)
    with pytest.raises(TypeError):
        forge(direct, source="scheduled_direct")
    with pytest.raises(TypeError):
        forge(direct, source=None)


def test_foreign_or_equal_looking_triggers_and_hashes_are_rejected() -> None:
    direct = admit_private_scheduler_scheduled_occurrence(occurrence=scheduled())
    other_schedule = scheduled(schedule_key="private.schedule.other").trigger
    other_policy = scheduled(policy_revision=2).trigger
    clone = dataclasses.replace(direct.trigger)
    assert clone == direct.trigger and clone is not direct.trigger
    for foreign in (other_schedule, other_policy, clone, raw_scheduled(), raw_event()):
        with pytest.raises((ValueError, TypeError)):
            forge(direct, trigger=foreign)                                                   # identity retention: not merely equal
    for bad_hash in (other_schedule.idempotency_sha256, "0" * 64, "A" * 64):
        with pytest.raises(ValueError):
            forge(direct, run_idempotency_sha256=bad_hash)
    for bad_hash in (None, 1, b"a" * 64):
        with pytest.raises(TypeError):
            forge(direct, run_idempotency_sha256=bad_hash)
    driven = admit_private_scheduler_event_occurrence(occurrence=event())
    other_owner = event(owner_id=UUID(int=9)).trigger
    other_cause = event(cause_key="Other").trigger
    for foreign in (other_owner, other_cause, dataclasses.replace(driven.trigger)):
        with pytest.raises(ValueError):
            forge(driven, trigger=foreign)
    proof_admission = admit_private_scheduler_calendar_applicability(applicability=applicability())
    with pytest.raises(ValueError):
        forge(proof_admission, trigger=scheduled(schedule_key="private.schedule.other").trigger)
    with pytest.raises(TypeError):
        forge(driven, trigger=object())


def test_run_hash_equals_the_trigger_hash_with_no_second_hash() -> None:
    for admission in (admit_private_scheduler_scheduled_occurrence(occurrence=scheduled()), admit_private_scheduler_event_occurrence(occurrence=event()),
                      admit_private_scheduler_calendar_applicability(applicability=applicability())):
        assert admission.run_idempotency_sha256 == admission.trigger.idempotency_sha256
        assert len(admission.run_idempotency_sha256) == 64 and set(admission.run_idempotency_sha256) <= set("0123456789abcdef")


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_run_admission.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_wrapper_surfaces_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "enum", "re", "backend.engine.private.scheduler_trigger",
                     "backend.engine.private.scheduler_scheduled_occurrence", "backend.engine.private.scheduler_calendar_applicability",
                     "backend.engine.private.scheduler_event_occurrence"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)
    assert imported["backend.engine.private.scheduler_trigger"] <= {"PrivateSchedulerTrigger", "PrivateSchedulerTriggerKind"}
    assert "build_private_scheduler_trigger" not in {n for values in imported.values() for n in values}   # no raw trigger reconstruction
    assert "require_private_scheduler_applicable_occurrence" not in {n for values in imported.values() for n in values}
    for forbidden in ("hashlib", "json", "uuid", "random", "secrets", "infrastructure", "job_queue", "httpx", "requests", "asyncio", "fastapi",
                      "supabase", "redis", "socket", "urllib", "scheduler_recurrence", "calendar_evidence", "game_changer", "allocation", "rebalance"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_second_hash_clock_random_or_arithmetic() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"hashlib", "json", "sha256", "dumps", "hexdigest", "now", "today", "utcnow", "timestamp", "uuid4", "uuid1", "random",
                        "repr", "pickle", "hash", "float", "build_private_scheduler_trigger", "require_private_scheduler_applicable_occurrence"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]


_FRAGMENTS = ("legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis",
              "database", "repository", "job_queue", "background", "spawn", "enqueue", "submit", "dispatch", "execut", "worker", "pending", "running",
              "succe", "failed", "cancel", "skipped", "semaphore", "mutex", "heartbeat", "claim", "retry", "attempt", "backoff", "jitter", "persist",
              "game_changer", "allocation", "rebalance", "admitted")
_EXACT = {"run_id", "job_id", "lease", "lock", "cas", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today", "state"}


def test_no_lifecycle_concurrency_dispatch_persistence_or_legacy_queue_identifiers() -> None:
    identifiers: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for identifier in identifiers:
        lowered = identifier.lower()
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    functions = {n.name: n for n in _TREE.body if isinstance(n, ast.FunctionDef)}
    for name in ("admit_private_scheduler_scheduled_occurrence", "admit_private_scheduler_calendar_applicability", "admit_private_scheduler_event_occurrence"):
        assert not functions[name].args.args and len(functions[name].args.kwonlyargs) == 1
    assert not {n for n in functions if n.startswith("build_")}                                 # no generic caller-facing constructor


def test_documents_the_admission_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("canonical occurrence", "SCHEDULED_DIRECT", "SCHEDULED_CALENDAR_APPLICABLE", "EVENT_DRIVEN", "identity", "no second hash", "proof path",
                   "configuration", "no lifecycle", "no claim", "no dispatch", "no persistence", "legacy", "24C2B", "24D"):
        assert needle in doc, needle
