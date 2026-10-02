"""
backend/tests/test_scheduler_event_occurrence.py
================================================
Phase 24C1: deterministic EVENT_DRIVEN trigger materialization. The idempotency identity is derived (domain-separated SHA-256 over canonical JSON)
and never caller supplied; the closed Phase 24A stays the semantic authority. No dispatch, no run state, no concurrency, no retry, no persistence,
no legacy job queue.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
from datetime import datetime, time, timedelta, timezone, date
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_event_occurrence as module_under_test
from backend.engine.private.scheduler_event_occurrence import (
    PrivateSchedulerEventOccurrence,
    build_private_scheduler_event_occurrence,
)
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerAmbiguousTimePolicy, build_private_scheduler_scheduled_occurrence
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
WK, SC, EC, TK = PrivateSchedulerWorkKind, PrivateSchedulerScope, PrivateSchedulerEventCauseKind, PrivateSchedulerTriggerKind
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
INSTANT = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
PORTFOLIO_WORK = (WK.PORTFOLIO_ANALYSIS_REFRESH, WK.PORTFOLIO_HEALTH_CHECK, WK.GAME_CHANGER_REVIEW)


def kwargs(**changes):
    base = dict(work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None, event_cause_kind=EC.MACRO_RELEASE_INGESTED,
                cause_key="Evt-1", cause_available_at=INSTANT, policy_key="private.scheduler.v1", policy_revision=1)
    base.update(changes)
    return base


def portfolio(**changes):
    return kwargs(**{**dict(work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO,
                            event_cause_kind=EC.DISCLOSURE_INGESTED), **changes})


def build(**changes) -> PrivateSchedulerEventOccurrence:
    return build_private_scheduler_event_occurrence(**kwargs(**changes))


def digest(**changes) -> str:
    return build_private_scheduler_event_occurrence(**portfolio(**changes)).trigger.idempotency_sha256


def raw_event_trigger(hash_value: str, **changes) -> PrivateSchedulerTrigger:
    values = dict(trigger_kind=TK.EVENT_DRIVEN, work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO,
                  scheduled_for=None, event_cause_kind=EC.DISCLOSURE_INGESTED, cause_key="Evt-1", cause_available_at=INSTANT,
                  policy_key="private.scheduler.v1", policy_revision=1, idempotency_sha256=hash_value)
    values.update(changes)
    return build_private_scheduler_trigger(**values)


# --- shapes ------------------------------------------------------------------------------------------------------

def test_stored_shape_and_builder_shape() -> None:
    fields = dataclasses.fields(PrivateSchedulerEventOccurrence)
    assert [f.name for f in fields] == ["trigger"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        build().trigger = build().trigger  # type: ignore[misc]
    parameters = list(inspect.signature(build_private_scheduler_event_occurrence).parameters.values())
    assert [p.name for p in parameters] == ["work_kind", "scope", "owner_id", "portfolio_id", "event_cause_kind", "cause_key", "cause_available_at",
                                            "policy_key", "policy_revision"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)
    for forbidden in ("idempotency_sha256", "trigger_kind", "scheduled_for"):
        with pytest.raises(TypeError):
            build_private_scheduler_event_occurrence(**kwargs(), **{forbidden: None})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_private_scheduler_event_occurrence(*kwargs().values())  # type: ignore[misc]


def test_the_built_trigger_is_a_closed_24a_event_driven_trigger() -> None:
    trigger = build().trigger
    assert type(trigger) is PrivateSchedulerTrigger and trigger.trigger_kind is TK.EVENT_DRIVEN and trigger.scheduled_for is None
    assert (trigger.event_cause_kind, trigger.cause_key, trigger.cause_available_at) == (EC.MACRO_RELEASE_INGESTED, "Evt-1", INSTANT)
    assert not {"event_verified", "source_verified", "event_exists"} & {f.name for f in dataclasses.fields(PrivateSchedulerEventOccurrence)}


# --- 24A delegation ----------------------------------------------------------------------------------------------

def test_phase_24a_remains_the_semantic_authority() -> None:
    class SubUUID(UUID):
        pass

    class SubStr(str):
        pass

    assert build().trigger.scope is SC.SYSTEM
    for work in PORTFOLIO_WORK:
        built = build_private_scheduler_event_occurrence(**portfolio(work_kind=work))
        assert (built.trigger.work_kind, built.trigger.owner_id, built.trigger.portfolio_id) == (work, OWNER, PORTFOLIO)
        for owner, pid in ((None, PORTFOLIO), (OWNER, None), (str(OWNER), PORTFOLIO), (OWNER, SubUUID(int=2))):
            with pytest.raises(TypeError):
                build_private_scheduler_event_occurrence(**portfolio(work_kind=work, owner_id=owner, portfolio_id=pid))
    for work, scope in ((WK.SOURCE_DATA_REFRESH, SC.PORTFOLIO), *((w, SC.SYSTEM) for w in PORTFOLIO_WORK)):
        owner, pid = (OWNER, PORTFOLIO) if scope is SC.PORTFOLIO else (None, None)
        with pytest.raises(ValueError):
            build_private_scheduler_event_occurrence(**kwargs(work_kind=work, scope=scope, owner_id=owner, portfolio_id=pid))
    with pytest.raises(ValueError):
        build(owner_id=OWNER)                                                                # SYSTEM scope with an owner
    for bad in ("", " x", "x ", "x" * 129, "a\nb"):
        with pytest.raises(ValueError):
            build(cause_key=bad)
    for bad in (None, 1, SubStr("x")):
        with pytest.raises(TypeError):
            build(cause_key=bad)
    for bad in (None, "disclosure_ingested", 1):
        with pytest.raises(TypeError):
            build(event_cause_kind=bad)                                                      # an event trigger needs its cause kind
    for bad in (None, datetime(2026, 10, 2, 7), "2026-10-02T07:00:00Z", INSTANT.date()):
        with pytest.raises(TypeError):
            build(cause_available_at=bad)
    for bad in ({"policy_key": "X"}, {"policy_revision": 0}):
        with pytest.raises(ValueError):
            build(**bad)
    for bad in ({"policy_revision": True}, {"policy_revision": 1.0}, {"policy_key": None}):
        with pytest.raises(TypeError):
            build(**bad)


def test_every_event_cause_builds_without_implying_work_or_scope() -> None:
    for cause in EC:
        system = build(event_cause_kind=cause)
        owned = build_private_scheduler_event_occurrence(**portfolio(event_cause_kind=cause, work_kind=WK.PORTFOLIO_ANALYSIS_REFRESH))
        assert system.trigger.event_cause_kind is cause and system.trigger.work_kind is WK.SOURCE_DATA_REFRESH and system.trigger.scheduled_for is None
        assert owned.trigger.event_cause_kind is cause and owned.trigger.work_kind is WK.PORTFOLIO_ANALYSIS_REFRESH
        with pytest.raises(ValueError):
            build(event_cause_kind=cause, work_kind=WK.GAME_CHANGER_REVIEW)                  # a cause never repairs a wrong pairing


# --- deterministic idempotency -----------------------------------------------------------------------------------

def test_hash_is_deterministic_and_lowercase_sha256() -> None:
    first, second = build(), build()
    assert first == second and first.trigger.idempotency_sha256 == second.trigger.idempotency_sha256
    value = first.trigger.idempotency_sha256
    assert len(value) == 64 and set(value) <= set("0123456789abcdef")


def test_canonical_payload_is_pinned_and_the_hash_is_independently_reproducible() -> None:
    occurrence = build_private_scheduler_event_occurrence(**portfolio())
    expected_payload = {
        "protocol": "sentinax.private.scheduler.event-occurrence.v1", "trigger_kind": "event_driven", "work_kind": "game_changer_review",
        "scope": "portfolio", "owner_id": str(OWNER), "portfolio_id": str(PORTFOLIO), "scheduled_for": None,
        "event_cause_kind": "disclosure_ingested", "cause_key": "Evt-1", "cause_available_at": "2026-10-02T07:00:00.000000+00:00",
        "policy_key": "private.scheduler.v1", "policy_revision": 1,
    }
    document = json.dumps(expected_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert occurrence.trigger.idempotency_sha256 == hashlib.sha256(document.encode("utf-8")).hexdigest()
    assert set(expected_payload) == {"protocol", "trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for", "event_cause_kind",
                                     "cause_key", "cause_available_at", "policy_key", "policy_revision"}
    system_payload = dict(expected_payload, work_kind="source_data_refresh", scope="system", owner_id=None, portfolio_id=None,
                          event_cause_kind="macro_release_ingested")
    system_document = json.dumps(system_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert build().trigger.idempotency_sha256 == hashlib.sha256(system_document.encode("utf-8")).hexdigest()           # explicit JSON nulls
    private = module_under_test._canonical_payload(WK.GAME_CHANGER_REVIEW, SC.PORTFOLIO, OWNER, PORTFOLIO, EC.DISCLOSURE_INGESTED, "Evt-1", INSTANT,
                                                   "private.scheduler.v1", 1)
    assert private == expected_payload
    assert not set(private) & {"created_at", "received_at", "attempt", "job_id", "worker_id", "run_id", "uuid"}      # no ambient / runtime field


def test_domain_marker_is_the_event_occurrence_protocol_not_the_scheduled_one() -> None:
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    assert "sentinax.private.scheduler.event-occurrence.v1" in source
    assert "sentinax.private.scheduler.scheduled-occurrence.v1" not in source                        # the scheduled domain marker is never reused
    scheduled = build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.test", schedule_revision=1, timezone_key="UTC", local_date=date(2026, 10, 2), local_time=time(7, 0),
        ambiguous_time_policy=PrivateSchedulerAmbiguousTimePolicy.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None,
        portfolio_id=None, policy_key="private.scheduler.v1", policy_revision=1)
    assert scheduled.trigger.idempotency_sha256 != build().trigger.idempotency_sha256                                # domains never collide


def test_equivalent_instants_across_offsets_share_a_hash_and_the_stored_form_is_preserved() -> None:
    plus3, minus5 = timezone(timedelta(hours=3)), timezone(timedelta(hours=-5))
    variants = [datetime(2026, 10, 2, 7, 0, tzinfo=UTC), datetime(2026, 10, 2, 10, 0, tzinfo=plus3), datetime(2026, 10, 2, 2, 0, tzinfo=minus5)]
    built = [build(cause_available_at=v) for v in variants]
    assert len({o.trigger.idempotency_sha256 for o in built}) == 1
    for occurrence, original in zip(built, variants):
        assert occurrence.trigger.cause_available_at is original and occurrence.trigger.cause_available_at.utcoffset() == original.utcoffset()
    assert len({o.trigger.cause_available_at.utcoffset() for o in built}) == 3                      # three different stored representations
    assert build(cause_available_at=INSTANT + timedelta(microseconds=1)).trigger.idempotency_sha256 != built[0].trigger.idempotency_sha256


def test_every_identity_dimension_changes_the_hash() -> None:
    base = digest()
    variants = {
        "work": digest(work_kind=WK.PORTFOLIO_HEALTH_CHECK), "owner": digest(owner_id=UUID(int=3)), "portfolio": digest(portfolio_id=UUID(int=4)),
        "cause_kind": digest(event_cause_kind=EC.MACRO_RELEASE_INGESTED), "cause_key": digest(cause_key="Evt-2"),
        "instant": digest(cause_available_at=INSTANT + timedelta(microseconds=1)), "policy_key": digest(policy_key="private.scheduler.v2"),
        "policy_revision": digest(policy_revision=2),
    }
    assert all(value != base for value in variants.values())
    assert len(set(variants.values()) | {base}) == len(variants) + 1                                  # and all are mutually distinct
    assert build().trigger.idempotency_sha256 != base                                                 # SYSTEM vs PORTFOLIO identity differs


def test_owner_and_portfolio_isolation_for_the_same_external_event() -> None:
    values = {digest(owner_id=owner, portfolio_id=pid) for owner, pid in ((OWNER, PORTFOLIO), (UUID(int=7), PORTFOLIO), (OWNER, UUID(int=8)),
                                                                          (UUID(int=7), UUID(int=8)))}
    assert len(values) == 4


def test_same_event_with_different_work_has_different_identity() -> None:
    values = {digest(work_kind=w) for w in PORTFOLIO_WORK}
    assert len(values) == 3                                                                          # no global event-only deduplication


def test_cause_key_is_exact_identity_without_normalization() -> None:
    assert digest(cause_key="CPI") != digest(cause_key="cpi") != digest(cause_key="Cpi")
    assert digest(cause_key="CPI") != digest(cause_key="CPI1")


def test_policy_revision_and_event_cause_kind_are_identity() -> None:
    assert digest(policy_revision=1) != digest(policy_revision=2)
    assert digest(event_cause_kind=EC.DISCLOSURE_INGESTED, cause_key="same") != digest(event_cause_kind=EC.MACRO_RELEASE_INGESTED, cause_key="same")


# --- direct construction / forge resistance ----------------------------------------------------------------------

def test_canonical_trigger_can_be_wrapped_directly() -> None:
    canonical = build_private_scheduler_event_occurrence(**portfolio()).trigger
    assert PrivateSchedulerEventOccurrence(trigger=canonical).trigger is canonical
    assert PrivateSchedulerEventOccurrence(trigger=canonical) == build_private_scheduler_event_occurrence(**portfolio())


def test_arbitrary_or_foreign_hashes_are_rejected() -> None:
    canonical = build_private_scheduler_event_occurrence(**portfolio()).trigger
    assert canonical.idempotency_sha256 != "0" * 64
    other = {
        "arbitrary": "0" * 64,
        "other_work": digest(work_kind=WK.PORTFOLIO_HEALTH_CHECK),
        "other_owner": digest(owner_id=UUID(int=9)),
        "other_policy_revision": digest(policy_revision=2),
        "other_cause": digest(event_cause_kind=EC.MACRO_RELEASE_INGESTED),
        "other_cause_key": digest(cause_key="Other"),
    }
    for name, value in other.items():
        with pytest.raises(ValueError):
            PrivateSchedulerEventOccurrence(trigger=raw_event_trigger(value))
    assert PrivateSchedulerEventOccurrence(trigger=raw_event_trigger(canonical.idempotency_sha256)).trigger.idempotency_sha256 == canonical.idempotency_sha256
    with pytest.raises(ValueError):                                                                   # right hash, different authority field
        PrivateSchedulerEventOccurrence(trigger=raw_event_trigger(canonical.idempotency_sha256, policy_revision=2))


def test_scheduled_triggers_are_rejected() -> None:
    raw_scheduled = build_private_scheduler_trigger(
        trigger_kind=TK.SCHEDULED, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None, scheduled_for=INSTANT,
        event_cause_kind=None, cause_key=None, cause_available_at=None, policy_key="private.scheduler.v1", policy_revision=1, idempotency_sha256="a" * 64)
    scheduled_occurrence = build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.test", schedule_revision=1, timezone_key="UTC", local_date=date(2026, 10, 2), local_time=time(7, 0),
        ambiguous_time_policy=PrivateSchedulerAmbiguousTimePolicy.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None,
        portfolio_id=None, policy_key="private.scheduler.v1", policy_revision=1)
    for trigger in (raw_scheduled, scheduled_occurrence.trigger):
        with pytest.raises(ValueError):
            PrivateSchedulerEventOccurrence(trigger=trigger)


def test_wrapper_requires_an_exact_trigger() -> None:
    class SubTrigger(PrivateSchedulerTrigger):
        pass

    canonical = build().trigger
    sub = SubTrigger(**{f.name: getattr(canonical, f.name) for f in dataclasses.fields(canonical)})
    for bad in (object(), None, {"x": 1}, sub, build()):
        with pytest.raises(TypeError):
            PrivateSchedulerEventOccurrence(trigger=bad)  # type: ignore[arg-type]


def test_no_event_proof_history_or_current_time_is_involved() -> None:
    for year in (1971, 2026, 2999):
        stamp = datetime(year, 1, 1, tzinfo=UTC)
        assert build(cause_available_at=stamp).trigger.cause_available_at == stamp               # past, present and future are all valid
    assert build() == build()                                                                    # nothing but explicit inputs participates


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_event_occurrence.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_24a_surface_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "hashlib", "json", "uuid", "backend.engine.private.scheduler_trigger"}
    assert imported["backend.engine.private.scheduler_trigger"] <= {
        "PrivateSchedulerEventCauseKind", "PrivateSchedulerScope", "PrivateSchedulerTrigger", "PrivateSchedulerTriggerKind",
        "PrivateSchedulerWorkKind", "build_private_scheduler_trigger"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)
    for forbidden in ("infrastructure", "job_queue", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis", "socket", "urllib", "random",
                      "secrets", "os", "pickle", "game_changer", "allocation", "rebalance", "scheduled_occurrence", "recurrence", "calendar"):
        assert not any(part == forbidden or (len(forbidden) > 3 and forbidden in part) for name in names for part in name.split(".")), forbidden


def test_no_ambient_clock_random_identity_or_arithmetic() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "sleep", "timestamp", "uuid4", "uuid1", "random", "urandom", "token_hex",
                        "repr", "pickle", "hash", "float"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    assert not [n for n in ast.walk(_TREE) if (isinstance(n, ast.BinOp) and isinstance(n.op, arithmetic)) or isinstance(n, ast.AugAssign)]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert ints <= {1, 128}
    calls = {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in ast.walk(_TREE) if isinstance(n, ast.Call)}
    assert "sha256" in calls and "dumps" in calls


_FRAGMENTS = ("scheduler_interval", "legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio", "fastapi",
              "supabase", "redis", "database", "repository", "job_queue", "background", "spawn", "enqueue", "submit", "dispatch", "execut", "worker",
              "pending", "running", "completed", "success", "failed", "error_state", "cancel", "skipped", "semaphore", "mutex", "heartbeat", "claim",
              "retry", "attempt", "backoff", "jitter", "verified", "persist", "game_changer", "allocation", "rebalance", "recurrence", "calendar")
_EXACT = {"lease", "lock", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today"}


def test_no_dispatch_run_state_concurrency_retry_persistence_or_legacy_queue_identifiers() -> None:
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
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "build_private_scheduler_event_occurrence")
    assert not function.args.args and len(function.args.kwonlyargs) == 9


def test_documents_the_event_identity_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("domain-separated", "sentinax.private.scheduler.event-occurrence.v1", "UTC", "no event-occurrence proof", "no previous-run lookup",
                   "no dispatch", "no run state", "no persistence", "24C2", "24D", "legacy", "owner"):
        assert needle in doc, needle
