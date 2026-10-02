"""
backend/tests/test_scheduler_run_lifecycle.py
=============================================
Phase 24C2B: lifecycle and claim-lease authority for ONE admitted run. READY / CLAIMED / SUCCEEDED / FAILED, an optimistic state_version +
expected_version CAS contract, half-open claim leases, renewal only before expiry, takeover only at or after expiry, terminal finality, stale-claim
rejection and transition records with before/after provenance. Pure domain: no storage, worker, dispatch, retry, clock or randomness.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import scheduler_run_lifecycle as module_under_test
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
    PrivateSchedulerRunAdmissionSource,
    admit_private_scheduler_calendar_applicability,
    admit_private_scheduler_event_occurrence,
    admit_private_scheduler_scheduled_occurrence,
)
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
from backend.engine.private.scheduler_scheduled_occurrence import PrivateSchedulerAmbiguousTimePolicy, build_private_scheduler_scheduled_occurrence
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerWorkKind,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
STATE, KIND = PrivateSchedulerRunState, PrivateSchedulerRunTransitionKind
WK, SC, EC, AP = PrivateSchedulerWorkKind, PrivateSchedulerScope, PrivateSchedulerEventCauseKind, PrivateSchedulerAmbiguousTimePolicy
DAY = date(2026, 10, 2)
OWNER, PORTFOLIO = UUID(int=1), UUID(int=2)
T0 = datetime(2026, 10, 2, 6, 55, tzinfo=UTC)                          # 09:55 Istanbul == the scheduled instant
EVENT_T = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
US = timedelta(microseconds=1)


def at(minutes=0, base=T0) -> datetime:
    return base + timedelta(minutes=minutes)


def scheduled_admission() -> PrivateSchedulerRunAdmission:
    occurrence = build_private_scheduler_scheduled_occurrence(
        schedule_key="private.schedule.test", schedule_revision=1, timezone_key="Europe/Istanbul", local_date=DAY, local_time=time(9, 55),
        ambiguous_time_policy=AP.REJECT, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM, owner_id=None, portfolio_id=None,
        policy_key="private.scheduler.v1", policy_revision=1)
    return admit_private_scheduler_scheduled_occurrence(occurrence=occurrence)


def calendar_admission() -> PrivateSchedulerRunAdmission:
    recurrence = build_private_scheduler_recurrence(
        schedule_key="private.schedule.test", schedule_revision=1, timezone_key="Europe/Istanbul", local_time=time(9, 55), ambiguous_time_policy=AP.REJECT,
        recurrence_kind=PrivateSchedulerRecurrenceKind.DAILY, weekdays=(), day_of_month=None, work_kind=WK.SOURCE_DATA_REFRESH, scope=SC.SYSTEM,
        owner_id=None, portfolio_id=None, policy_key="private.scheduler.v1", policy_revision=1)
    decision = evaluate_private_scheduler_civil_date(recurrence=recurrence, local_date=DAY)
    evidence = build_private_scheduler_calendar_date_evidence(
        calendar_key="exchange.test.core", calendar_revision=1, calendar_kind=PrivateSchedulerCalendarKind.EXCHANGE_SESSION, timezone_key="Europe/Istanbul",
        local_date=DAY, coverage=PrivateSchedulerCalendarCoverage.COMPLETE_FOR_DATE,
        exchange_sessions=(PrivateSchedulerExchangeSessionWindow(opens_at=at(5), closes_at=at(485)),), source_releases=(), source_key="source.test",
        published_at=None, observed_at=at(-200), source_content_sha256="a" * 64)
    constraint = build_private_scheduler_calendar_constraint(
        calendar_key="exchange.test.core", calendar_revision=1, calendar_kind=PrivateSchedulerCalendarKind.EXCHANGE_SESSION,
        applicability_mode=PrivateSchedulerCalendarApplicabilityMode.EXCHANGE_SESSION_EXISTS, release_key=None)
    applicability = evaluate_private_scheduler_calendar_applicability(
        decision=decision, calendar_binding=bind_private_scheduler_calendar_pit(evidence=evidence, knowledge_cutoff=at(-100)), constraint=constraint)
    return admit_private_scheduler_calendar_applicability(applicability=applicability)


def event_admission(cause_at=EVENT_T) -> PrivateSchedulerRunAdmission:
    return admit_private_scheduler_event_occurrence(occurrence=build_private_scheduler_event_occurrence(
        work_kind=WK.GAME_CHANGER_REVIEW, scope=SC.PORTFOLIO, owner_id=OWNER, portfolio_id=PORTFOLIO, event_cause_kind=EC.DISCLOSURE_INGESTED,
        cause_key="Evt-1", cause_available_at=cause_at, policy_key="private.scheduler.v1", policy_revision=1))


def ready(admission=None) -> PrivateSchedulerRunLifecycle:
    return initialize_private_scheduler_run_lifecycle(admission=admission or scheduled_admission()).after


def claimed(admission=None, key="claim-a", start=T0, minutes=10) -> PrivateSchedulerRunLifecycle:
    base = ready(admission)
    return claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key=key, claimed_at=start, lease_expires_at=at(minutes, start)).after


def lifecycle_kwargs(**changes):
    base = dict(admission=scheduled_admission(), state=STATE.READY, state_version=1, claim_key=None, claimed_at=None, lease_expires_at=None,
                terminal_at=None, failure_code=None)
    base.update(changes)
    return base


# --- enums / shapes ----------------------------------------------------------------------------------------------

def test_enums_and_stored_shapes() -> None:
    assert [(m.name, m.value) for m in STATE] == [("READY", "ready"), ("CLAIMED", "claimed"), ("SUCCEEDED", "succeeded"), ("FAILED", "failed")]
    assert [(m.name, m.value) for m in KIND] == [
        ("INITIALIZE", "initialize"), ("CLAIM", "claim"), ("RENEW_CLAIM", "renew_claim"), ("TAKE_OVER_EXPIRED_CLAIM", "take_over_expired_claim"),
        ("SUCCEED", "succeed"), ("FAIL", "fail")]
    lifecycle_fields = dataclasses.fields(PrivateSchedulerRunLifecycle)
    transition_fields = dataclasses.fields(PrivateSchedulerRunTransition)
    assert [f.name for f in lifecycle_fields] == ["admission", "state", "state_version", "claim_key", "claimed_at", "lease_expires_at", "terminal_at",
                                                  "failure_code"]
    assert [f.name for f in transition_fields] == ["kind", "before", "after"]
    for fields in (lifecycle_fields, transition_fields):
        assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fields)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ready().state = STATE.CLAIMED  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        initialize_private_scheduler_run_lifecycle(admission=scheduled_admission()).kind = KIND.CLAIM  # type: ignore[misc]
    names = {f.name for f in lifecycle_fields}
    assert not names & {"run_id", "attempt", "retry_count", "result", "exception", "renewed_at", "second_hash", "run_idempotency_sha256"}
    expected = {initialize_private_scheduler_run_lifecycle: 1, claim_private_scheduler_run: 5, renew_private_scheduler_run_claim: 5,
                take_over_expired_private_scheduler_run_claim: 5, succeed_private_scheduler_run: 4, fail_private_scheduler_run: 5}
    for function, count in expected.items():
        parameters = list(inspect.signature(function).parameters.values())
        assert len(parameters) == count and all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in parameters)


# --- initialization ----------------------------------------------------------------------------------------------

def test_initialization_for_every_admission_source() -> None:
    for admission, source in ((scheduled_admission(), PrivateSchedulerRunAdmissionSource.SCHEDULED_DIRECT),
                              (calendar_admission(), PrivateSchedulerRunAdmissionSource.SCHEDULED_CALENDAR_APPLICABLE),
                              (event_admission(), PrivateSchedulerRunAdmissionSource.EVENT_DRIVEN)):
        assert admission.source is source
        transition = initialize_private_scheduler_run_lifecycle(admission=admission)
        assert type(transition) is PrivateSchedulerRunTransition and transition.kind is KIND.INITIALIZE and transition.before is None
        after = transition.after
        assert after.admission is admission and after.state is STATE.READY and after.state_version == 1
        assert (after.claim_key, after.claimed_at, after.lease_expires_at, after.terminal_at, after.failure_code) == (None,) * 5


def test_exact_admission_type_is_required() -> None:
    class SubAdmission(PrivateSchedulerRunAdmission):
        pass

    admission = scheduled_admission()
    sub = SubAdmission(**{f.name: getattr(admission, f.name) for f in dataclasses.fields(admission)})
    for bad in (None, object(), admission.trigger, sub):
        with pytest.raises(TypeError):
            initialize_private_scheduler_run_lifecycle(admission=bad)  # type: ignore[arg-type]


# --- not-before frontier -----------------------------------------------------------------------------------------

def test_claim_cannot_precede_the_run_not_before_instant() -> None:
    for admission, frontier in ((scheduled_admission(), T0), (calendar_admission(), T0), (event_admission(), EVENT_T)):
        base = ready(admission)
        trigger = admission.trigger
        assert (trigger.scheduled_for if trigger.scheduled_for is not None else trigger.cause_available_at) == frontier
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=frontier - US, lease_expires_at=frontier + timedelta(minutes=5))
        for stamp in (frontier, frontier + US):
            transition = claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=stamp, lease_expires_at=stamp + timedelta(minutes=5))
            assert transition.after.state is STATE.CLAIMED and transition.after.claimed_at == stamp


def test_not_before_for_an_event_with_a_non_utc_cause_instant_uses_the_utc_instant() -> None:
    plus3 = timezone(timedelta(hours=3))
    admission = event_admission(datetime(2026, 10, 2, 10, 0, tzinfo=plus3))                     # == 07:00Z
    base = ready(admission)
    with pytest.raises(ValueError):
        claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=EVENT_T - US, lease_expires_at=EVENT_T + timedelta(minutes=5))
    assert claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=EVENT_T,
                                       lease_expires_at=EVENT_T + timedelta(minutes=5)).after.claimed_at == EVENT_T


# --- claim -------------------------------------------------------------------------------------------------------

def test_claim_transition() -> None:
    base = ready()
    transition = claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="Claim-A", claimed_at=T0, lease_expires_at=at(10))
    assert transition.kind is KIND.CLAIM and transition.before is base
    after = transition.after
    assert (after.state, after.state_version, after.claim_key, after.claimed_at, after.lease_expires_at) == (STATE.CLAIMED, 2, "Claim-A", T0, at(10))
    assert after.admission is base.admission and after.claimed_at.tzinfo is UTC and after.lease_expires_at.tzinfo is UTC
    for lease in (T0, T0 - US, at(-1)):
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=T0, lease_expires_at=lease)
    with pytest.raises(ValueError):                                                                # only a READY run can be claimed
        claim_private_scheduler_run(lifecycle=after, expected_version=2, claim_key="c", claimed_at=at(1), lease_expires_at=at(20))


def test_utc_canonical_inputs_and_keys() -> None:
    base = ready()
    plus3 = timezone(timedelta(hours=3))

    class SubDatetime(datetime):
        pass

    class SubStr(str):
        pass

    for bad in (T0.astimezone(plus3), datetime(2026, 10, 2, 6, 55)):
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=bad, lease_expires_at=at(10))
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=T0, lease_expires_at=bad)
    for bad in (SubDatetime(2026, 10, 2, 6, 55, tzinfo=UTC), "2026-10-02T06:55:00Z", None, DAY):
        with pytest.raises(TypeError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="c", claimed_at=bad, lease_expires_at=at(10))
    for bad in ("", " x", "x ", "x" * 129, "a\nb", "a\tb"):
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key=bad, claimed_at=T0, lease_expires_at=at(10))
    for bad in (None, 1, b"x", SubStr("x")):
        with pytest.raises(TypeError):
            claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key=bad, claimed_at=T0, lease_expires_at=at(10))
    assert claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="x" * 128, claimed_at=T0, lease_expires_at=at(10)).after.claim_key == "x" * 128


# --- expected version / CAS contract -----------------------------------------------------------------------------

def test_stale_and_future_expected_versions_fail_for_every_transition_family() -> None:
    base, active = ready(), claimed()
    for version in (0, 2, 3, -1):
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=base, expected_version=version, claim_key="c", claimed_at=T0, lease_expires_at=at(10))
    for bad in (True, 1.0, "1", None):
        with pytest.raises(TypeError):
            claim_private_scheduler_run(lifecycle=base, expected_version=bad, claim_key="c", claimed_at=T0, lease_expires_at=at(10))
    for version in (active.state_version - 1, active.state_version + 1):
        with pytest.raises(ValueError):
            renew_private_scheduler_run_claim(lifecycle=active, expected_version=version, claim_key="claim-a", renewed_at=at(1), lease_expires_at=at(20))
        with pytest.raises(ValueError):
            take_over_expired_private_scheduler_run_claim(lifecycle=active, expected_version=version, claim_key="claim-b", claimed_at=at(10),
                                                          lease_expires_at=at(30))
        with pytest.raises(ValueError):
            succeed_private_scheduler_run(lifecycle=active, expected_version=version, claim_key="claim-a", terminal_at=at(1))
        with pytest.raises(ValueError):
            fail_private_scheduler_run(lifecycle=active, expected_version=version, claim_key="claim-a", terminal_at=at(1), failure_code="engine_error")
    for bad in (True, 2.0, "2", None):
        with pytest.raises(TypeError):
            succeed_private_scheduler_run(lifecycle=active, expected_version=bad, claim_key="claim-a", terminal_at=at(1))


def test_every_transition_increments_the_version_by_exactly_one_and_keeps_the_admission() -> None:
    first = ready()
    second = claim_private_scheduler_run(lifecycle=first, expected_version=1, claim_key="claim-a", claimed_at=T0, lease_expires_at=at(10)).after
    third = renew_private_scheduler_run_claim(lifecycle=second, expected_version=2, claim_key="claim-a", renewed_at=at(1), lease_expires_at=at(20)).after
    fourth = take_over_expired_private_scheduler_run_claim(lifecycle=third, expected_version=3, claim_key="claim-b", claimed_at=at(20), lease_expires_at=at(30)).after
    fifth = succeed_private_scheduler_run(lifecycle=fourth, expected_version=4, claim_key="claim-b", terminal_at=at(21)).after
    chain = [first, second, third, fourth, fifth]
    assert [c.state_version for c in chain] == [1, 2, 3, 4, 5]
    assert all(c.admission is first.admission for c in chain)
    assert fifth.state is STATE.SUCCEEDED


# --- renewal -----------------------------------------------------------------------------------------------------

def test_renewal_extends_the_active_claim_only() -> None:
    active = claimed(minutes=10)
    transition = renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", renewed_at=at(5), lease_expires_at=at(25))
    after = transition.after
    assert transition.kind is KIND.RENEW_CLAIM and transition.before is active
    assert (after.state, after.state_version, after.claim_key, after.claimed_at, after.lease_expires_at) == (STATE.CLAIMED, 3, "claim-a", T0, at(25))
    assert not hasattr(after, "renewed_at")
    assert renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", renewed_at=T0, lease_expires_at=at(25)).after.claimed_at == T0
    assert renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", renewed_at=at(10) - US,
                                             lease_expires_at=at(25)).after.state_version == 3
    for renewed in (at(10), at(10) + US, at(30), T0 - US):                                       # at expiry is already too late
        with pytest.raises(ValueError):
            renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", renewed_at=renewed, lease_expires_at=at(60))
    for lease in (at(10), at(10) - US, at(5)):
        with pytest.raises(ValueError):
            renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", renewed_at=at(5), lease_expires_at=lease)
    for wrong in ("claim-b", "Claim-A", "claim-a "):
        with pytest.raises(ValueError if wrong != "claim-a " else ValueError):
            renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key=wrong, renewed_at=at(5), lease_expires_at=at(25))
    with pytest.raises(ValueError):
        renew_private_scheduler_run_claim(lifecycle=ready(), expected_version=1, claim_key="claim-a", renewed_at=at(5), lease_expires_at=at(25))


# --- takeover ----------------------------------------------------------------------------------------------------

def test_expired_claim_takeover_boundaries() -> None:
    active = claimed(key="claim-a", minutes=10)
    for stamp in (at(10), at(10) + US, at(60)):
        transition = take_over_expired_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-b", claimed_at=stamp,
                                                                   lease_expires_at=stamp + timedelta(minutes=5))
        after = transition.after
        assert transition.kind is KIND.TAKE_OVER_EXPIRED_CLAIM and transition.before is active
        assert (after.state, after.state_version, after.claim_key, after.claimed_at) == (STATE.CLAIMED, 3, "claim-b", stamp)
        assert after.lease_expires_at == stamp + timedelta(minutes=5) and after.admission is active.admission
    for early in (at(10) - US, at(5), T0):
        with pytest.raises(ValueError):
            take_over_expired_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-b", claimed_at=early, lease_expires_at=at(60))
    with pytest.raises(ValueError):                                                                # same claim key
        take_over_expired_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", claimed_at=at(10), lease_expires_at=at(60))
    with pytest.raises(ValueError):                                                                # new lease must be a valid interval
        take_over_expired_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-b", claimed_at=at(10), lease_expires_at=at(10))
    with pytest.raises(ValueError):
        take_over_expired_private_scheduler_run_claim(lifecycle=ready(), expected_version=1, claim_key="claim-b", claimed_at=at(10), lease_expires_at=at(60))


def test_takeover_is_not_a_retry_of_a_terminal_run() -> None:
    failed = fail_private_scheduler_run(lifecycle=claimed(), expected_version=2, claim_key="claim-a", terminal_at=at(1), failure_code="engine_error").after
    with pytest.raises(ValueError):
        take_over_expired_private_scheduler_run_claim(lifecycle=failed, expected_version=failed.state_version, claim_key="claim-b", claimed_at=at(30),
                                                      lease_expires_at=at(60))


# --- success / failure -------------------------------------------------------------------------------------------

def test_success_requires_the_active_claim() -> None:
    active = claimed(minutes=10)
    for stamp in (T0, at(5), at(10) - US):
        transition = succeed_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=stamp)
        after = transition.after
        assert transition.kind is KIND.SUCCEED and transition.before is active
        assert (after.state, after.state_version, after.claim_key, after.claimed_at, after.lease_expires_at, after.terminal_at, after.failure_code) == \
               (STATE.SUCCEEDED, 3, "claim-a", T0, at(10), stamp, None)
    for stamp in (at(10), at(10) + US, T0 - US):
        with pytest.raises(ValueError):
            succeed_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=stamp)
    for wrong in ("claim-b", "Claim-A"):
        with pytest.raises(ValueError):
            succeed_private_scheduler_run(lifecycle=active, expected_version=2, claim_key=wrong, terminal_at=at(1))
    with pytest.raises(ValueError):
        succeed_private_scheduler_run(lifecycle=ready(), expected_version=1, claim_key="claim-a", terminal_at=at(1))


def test_failure_requires_the_active_claim_and_a_canonical_code() -> None:
    active = claimed(minutes=10)
    for stamp in (T0, at(5), at(10) - US):
        after = fail_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=stamp, failure_code="provider_timeout").after
        assert (after.state, after.state_version, after.terminal_at, after.failure_code) == (STATE.FAILED, 3, stamp, "provider_timeout")
    for stamp in (at(10), at(10) + US):
        with pytest.raises(ValueError):
            fail_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=stamp, failure_code="engine_error")
    for bad in ("", "Engine", " x", "x ", "a" * 129, "has space", "x\n", "-x", "exception: boom at line 3"):
        with pytest.raises(ValueError):
            fail_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=at(1), failure_code=bad)
    for bad in (None, 1, b"x"):
        with pytest.raises(TypeError):
            fail_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=at(1), failure_code=bad)
    with pytest.raises(ValueError):
        fail_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-b", terminal_at=at(1), failure_code="engine_error")


def test_terminal_states_are_final_for_every_transition() -> None:
    succeeded = succeed_private_scheduler_run(lifecycle=claimed(), expected_version=2, claim_key="claim-a", terminal_at=at(1)).after
    failed = fail_private_scheduler_run(lifecycle=claimed(), expected_version=2, claim_key="claim-a", terminal_at=at(1), failure_code="engine_error").after
    for terminal in (succeeded, failed):
        version = terminal.state_version
        with pytest.raises(ValueError):
            claim_private_scheduler_run(lifecycle=terminal, expected_version=version, claim_key="claim-z", claimed_at=at(30), lease_expires_at=at(60))
        with pytest.raises(ValueError):
            renew_private_scheduler_run_claim(lifecycle=terminal, expected_version=version, claim_key="claim-a", renewed_at=at(1), lease_expires_at=at(60))
        with pytest.raises(ValueError):
            take_over_expired_private_scheduler_run_claim(lifecycle=terminal, expected_version=version, claim_key="claim-z", claimed_at=at(30),
                                                          lease_expires_at=at(60))
        with pytest.raises(ValueError):
            succeed_private_scheduler_run(lifecycle=terminal, expected_version=version, claim_key="claim-a", terminal_at=at(2))
        with pytest.raises(ValueError):
            fail_private_scheduler_run(lifecycle=terminal, expected_version=version, claim_key="claim-a", terminal_at=at(2), failure_code="engine_error")


def test_stale_claimant_cannot_finish_after_a_takeover() -> None:
    first = claimed(key="claim-a", minutes=10)
    second = take_over_expired_private_scheduler_run_claim(lifecycle=first, expected_version=2, claim_key="claim-b", claimed_at=at(10), lease_expires_at=at(30)).after
    with pytest.raises(ValueError):
        succeed_private_scheduler_run(lifecycle=second, expected_version=3, claim_key="claim-a", terminal_at=at(11))
    with pytest.raises(ValueError):
        fail_private_scheduler_run(lifecycle=second, expected_version=3, claim_key="claim-a", terminal_at=at(11), failure_code="engine_error")
    with pytest.raises(ValueError):
        renew_private_scheduler_run_claim(lifecycle=second, expected_version=3, claim_key="claim-a", renewed_at=at(11), lease_expires_at=at(60))
    assert succeed_private_scheduler_run(lifecycle=second, expected_version=3, claim_key="claim-b", terminal_at=at(11)).after.state is STATE.SUCCEEDED


# --- direct snapshot validation ----------------------------------------------------------------------------------

def test_direct_snapshot_shapes_are_enforced() -> None:
    admission = scheduled_admission()
    good_claim = dict(claim_key="c", claimed_at=T0, lease_expires_at=at(10))

    def make(**changes):
        return PrivateSchedulerRunLifecycle(**lifecycle_kwargs(admission=admission, **changes))

    assert make().state is STATE.READY
    ready_bad = (dict(claim_key="c"), dict(claimed_at=T0), dict(lease_expires_at=at(10)), dict(terminal_at=at(1)), dict(failure_code="x"), dict(state_version=2))
    for change in ready_bad:
        with pytest.raises(ValueError):
            make(**change)
    claimed_ok = dict(state=STATE.CLAIMED, state_version=2, **good_claim)
    assert make(**claimed_ok).claim_key == "c"
    claimed_bad = (dict(claim_key=None), dict(claimed_at=None), dict(lease_expires_at=None), dict(lease_expires_at=T0), dict(lease_expires_at=T0 - US),
                   dict(state_version=1), dict(terminal_at=at(1)), dict(failure_code="x"))
    for change in claimed_bad:
        with pytest.raises(ValueError):
            make(**{**claimed_ok, **change})
    succeeded_ok = dict(state=STATE.SUCCEEDED, state_version=3, terminal_at=at(5), **good_claim)
    assert make(**succeeded_ok).terminal_at == at(5)
    for change in (dict(terminal_at=None), dict(failure_code="x"), dict(state_version=2), dict(terminal_at=at(10)), dict(terminal_at=T0 - US), dict(claim_key=None)):
        with pytest.raises(ValueError):
            make(**{**succeeded_ok, **change})
    failed_ok = dict(state=STATE.FAILED, state_version=3, terminal_at=at(5), failure_code="engine_error", **good_claim)
    assert make(**failed_ok).failure_code == "engine_error"
    for change in (dict(failure_code=None), dict(terminal_at=None), dict(terminal_at=at(10)), dict(terminal_at=T0 - US), dict(state_version=2), dict(claim_key=None)):
        with pytest.raises(ValueError):
            make(**{**failed_ok, **change})
    for change in (dict(admission=object()), dict(admission=admission.trigger), dict(state="ready"), dict(state_version=True), dict(state_version=1.0),
                   dict(failure_code=1)):
        with pytest.raises(TypeError):
            make(**change)
    with pytest.raises(ValueError):
        make(state_version=0)
    with pytest.raises(ValueError):                                                              # a non-UTC stored instant is never converted
        make(**{**claimed_ok, "claimed_at": T0.astimezone(timezone(timedelta(hours=3)))})


# --- transition record forge resistance --------------------------------------------------------------------------

def test_transition_record_is_a_valid_canonical_object() -> None:
    admission = scheduled_admission()
    initial = initialize_private_scheduler_run_lifecycle(admission=admission)
    assert PrivateSchedulerRunTransition(kind=KIND.INITIALIZE, before=None, after=initial.after) == initial
    claim = claim_private_scheduler_run(lifecycle=initial.after, expected_version=1, claim_key="claim-a", claimed_at=T0, lease_expires_at=at(10))
    assert PrivateSchedulerRunTransition(kind=KIND.CLAIM, before=initial.after, after=claim.after) == claim


def test_transition_record_rejects_forgeries() -> None:
    admission = scheduled_admission()
    base = ready(admission)
    active = claim_private_scheduler_run(lifecycle=base, expected_version=1, claim_key="claim-a", claimed_at=T0, lease_expires_at=at(10)).after
    renewed = renew_private_scheduler_run_claim(lifecycle=active, expected_version=2, claim_key="claim-a", renewed_at=at(1), lease_expires_at=at(20)).after
    done = succeed_private_scheduler_run(lifecycle=active, expected_version=2, claim_key="claim-a", terminal_at=at(1)).after

    def lifecycle(**changes):
        return PrivateSchedulerRunLifecycle(**{**dict(admission=admission, state=STATE.CLAIMED, state_version=3, claim_key="claim-a", claimed_at=T0,
                                                      lease_expires_at=at(20), terminal_at=None, failure_code=None), **changes})

    forged = [
        (KIND.RENEW_CLAIM, base, active),                                                         # wrong kind for the states
        (KIND.CLAIM, active, renewed),
        (KIND.SUCCEED, active, renewed),
        (KIND.FAIL, active, done),                                                                # succeeded state under a FAIL record
        (KIND.INITIALIZE, base, base),                                                            # INITIALIZE with a before
        (KIND.CLAIM, None, active),                                                               # missing before
        (KIND.CLAIM, base, lifecycle(state_version=3)),                                           # skipped version
        (KIND.CLAIM, base, lifecycle(state_version=2, lease_expires_at=at(10), claimed_at=T0 - US)),       # claimed before the not-before instant
        (KIND.RENEW_CLAIM, active, lifecycle(state_version=3, claim_key="claim-z")),              # other claim key
        (KIND.RENEW_CLAIM, active, lifecycle(state_version=3, claimed_at=at(1), lease_expires_at=at(20))),    # original claimed_at moved
        (KIND.RENEW_CLAIM, active, lifecycle(state_version=3, lease_expires_at=at(10))),          # not extended
        (KIND.TAKE_OVER_EXPIRED_CLAIM, active, lifecycle(state_version=3, claim_key="claim-a", claimed_at=at(10), lease_expires_at=at(30))),   # same key
        (KIND.TAKE_OVER_EXPIRED_CLAIM, active, lifecycle(state_version=3, claim_key="claim-b", claimed_at=at(9), lease_expires_at=at(30))),    # before expiry
        (KIND.SUCCEED, active, dataclasses.replace(done, claim_key="claim-z")),
        (KIND.SUCCEED, active, dataclasses.replace(done, lease_expires_at=at(11))),
        (KIND.SUCCEED, active, dataclasses.replace(done, state_version=4)),
    ]
    for kind, before, after in forged:
        with pytest.raises(ValueError):
            PrivateSchedulerRunTransition(kind=kind, before=before, after=after)
    other_admission = scheduled_admission()
    assert other_admission == admission and other_admission is not admission                       # equal-but-distinct admission clone
    foreign_after = PrivateSchedulerRunLifecycle(admission=other_admission, state=STATE.CLAIMED, state_version=2, claim_key="claim-a", claimed_at=T0,
                                                 lease_expires_at=at(10), terminal_at=None, failure_code=None)
    with pytest.raises(ValueError):
        PrivateSchedulerRunTransition(kind=KIND.CLAIM, before=base, after=foreign_after)           # run identity cannot change
    for kind, before, after in ((object(), base, active), (KIND.CLAIM, object(), active), (KIND.CLAIM, base, object()), ("claim", base, active),
                                (KIND.CLAIM, base, None)):
        with pytest.raises(TypeError):
            PrivateSchedulerRunTransition(kind=kind, before=before, after=after)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        claim_private_scheduler_run(lifecycle=object(), expected_version=1, claim_key="c", claimed_at=T0, lease_expires_at=at(10))  # type: ignore[arg-type]


def test_exact_lifecycle_type_is_required_by_transitions() -> None:
    class SubLifecycle(PrivateSchedulerRunLifecycle):
        pass

    base = ready()
    sub = SubLifecycle(**{f.name: getattr(base, f.name) for f in dataclasses.fields(base)})
    with pytest.raises(TypeError):
        claim_private_scheduler_run(lifecycle=sub, expected_version=1, claim_key="c", claimed_at=T0, lease_expires_at=at(10))
    with pytest.raises(TypeError):
        PrivateSchedulerRunTransition(kind=KIND.CLAIM, before=sub, after=claimed())


def test_lifecycle_is_deterministic() -> None:
    assert ready() == ready()
    assert claimed() == claimed()


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/scheduler_run_lifecycle.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_and_the_closed_admission_surface_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "backend.engine.private.scheduler_run_admission",
                     "backend.engine.private.scheduler_trigger"}
    assert imported["backend.engine.private.scheduler_run_admission"] <= {"PrivateSchedulerRunAdmission"}
    assert imported["backend.engine.private.scheduler_trigger"] <= {"PrivateSchedulerTriggerKind"}
    assert not any(n.startswith("_") for values in imported.values() for n in values)
    for forbidden in ("uuid", "random", "secrets", "hashlib", "json", "infrastructure", "job_queue", "httpx", "requests", "asyncio", "fastapi", "supabase",
                      "redis", "socket", "urllib", "game_changer", "allocation", "rebalance", "recurrence", "calendar", "event_occurrence"):
        assert not any(forbidden in part for name in names for part in name.split(".")), forbidden


def test_no_clock_randomness_or_unbounded_logic() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "monotonic", "sleep", "timestamp", "timedelta", "uuid4", "uuid1", "random", "urandom",
                        "token_hex", "hashlib", "sha256", "float", "repr", "pickle"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor))]
    ints = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(n.value, bool)}
    assert ints <= {1, 2, 3, 128}                                                                  # versions and key length only: no timeout, no lease constant
    arithmetic = [n for n in ast.walk(_TREE) if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.Sub))]
    assert not arithmetic


_FRAGMENTS = ("legacy", "watchlist", "radar", "macd", "bearish", "telegram", "notif", "httpx", "requests", "asyncio", "fastapi", "supabase", "redis",
              "database", "repository", "job_queue", "background", "spawn", "enqueue", "submit", "dispatch", "execut", "worker", "pending", "running",
              "completed", "retry", "retries", "attempt", "backoff", "jitter", "cancel", "skipped", "persist", "payload", "stack_trace", "exception_text",
              "game_changer", "allocation", "rebalance", "recurrence", "calendar", "uuid", "random")
_EXACT = {"lock", "cas", "semaphore", "mutex", "heartbeat", "buy", "sell", "hold", "trade", "order", "tax", "llm", "now", "today", "error", "result"}


def test_no_retry_dispatch_persistence_randomness_or_legacy_queue_identifiers() -> None:
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
    members = {m.name for enum in (STATE, KIND) for m in enum}
    assert not members & {"PENDING", "RUNNING", "COMPLETED", "ERROR", "RETRYING", "CANCELLED", "SKIPPED", "RETRY", "RESET"}


def test_documents_the_lifecycle_boundaries() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("state_version", "expected_version", "distributed", "half-open", "not-before", "renew", "takeover", "terminal", "no retry", "24D",
                   "atomic", "legacy", "no persistence", "compare-and-swap", "stale claimant"):
        assert needle in doc, needle
