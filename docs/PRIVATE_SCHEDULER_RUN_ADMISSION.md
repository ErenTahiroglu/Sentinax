# Private Scheduler Run Admission (Phase 24C2A)

| Layer | Authority | Status |
|---|---|---|
| 24A | Primitive trigger envelope | closed |
| 24B | Deterministic scheduled occurrence (24B1), recurrence, calendar evidence and applicability (24B3B) | closed |
| 24C1 | Deterministic event-driven occurrence | closed |
| 24C2A | Unified run admission (this document) | implemented |
| 24C2B | Lifecycle and concurrency / claim transition authority | deferred |
| 24D | Persistence, uniqueness / CAS, queue and runtime | deferred |

Module: `backend/engine/private/scheduler_run_admission.py` (pure, in the static-guard `PURE_MANIFEST`; standard library plus the closed 24A / 24B1 / 24B3B / 24C1 public surface; it neither hashes nor rebuilds a trigger).

```python
admit_private_scheduler_scheduled_occurrence(*, occurrence)         # SCHEDULED_DIRECT
admit_private_scheduler_calendar_applicability(*, applicability)    # SCHEDULED_CALENDAR_APPLICABLE
admit_private_scheduler_event_occurrence(*, occurrence)             # EVENT_DRIVEN
PrivateSchedulerRunAdmission(source, scheduled_occurrence, calendar_applicability, event_occurrence, trigger, run_idempotency_sha256)    # 6 stored fields
```

## Canonical occurrence is not a raw 24A trigger

A raw 24A scheduled trigger may carry a syntactically valid caller hash with no proof that it came through 24B1; a raw event trigger may carry an arbitrary 64-hex hash with no proof of 24C1; and a bare scheduled
occurrence loses the fact that 24B3B evaluated the calendar. Admission therefore consumes only the closed wrapper authorities. There are three explicit builders, each with one keyword-only input; there is no
generic caller-facing constructor and no builder accepting a `PrivateSchedulerTrigger`.

## The three provenance paths

- `SCHEDULED_DIRECT`: an exact 24B1 `PrivateSchedulerScheduledOccurrence`; the stored trigger is `occurrence.trigger` (same object).
- `SCHEDULED_CALENDAR_APPLICABLE`: an exact 24B3B `PrivateSchedulerCalendarApplicability` with status `APPLICABLE`; the stored trigger is `applicability.candidate_occurrence.trigger` and **the applicability result itself is retained**
  as the audit proof that external-calendar applicability was evaluated. `NOT_APPLICABLE` and `UNAVAILABLE` results raise `ValueError`: there is no skipped, blocked or no-op run.
- `EVENT_DRIVEN`: an exact 24C1 `PrivateSchedulerEventOccurrence`; the stored trigger is `occurrence.trigger`.

The three provenance fields form a strict discriminated union: exactly the field matching the source is present and the other two are `None`; mixed and all-`None` shapes fail. Subclasses are rejected. The stored trigger must be
the source-derived trigger object by identity (an equal-looking clone, a trigger from another schedule, owner, policy revision or cause, or a raw trigger is rejected).

## Run idempotency identity: exactly the trigger hash

`run_idempotency_sha256 == trigger.idempotency_sha256`. There is **no second hash**: nothing about the admission source, calendar proof, clock, worker or attempt is hashed, and no UUID, salt or timestamp is added. One logical
occurrence is at most one logical scheduler run; persistence will later enforce uniqueness. Direct construction validates the exact `str` type and 64-lowercase-hex grammar and rejects any value that differs from the trigger hash.

## Proof path is not logical run identity

The admission path is audit provenance, not identity. The same scheduled candidate admitted directly and admitted through an APPLICABLE calendar proof has the same `run_idempotency_sha256` but a different `source` and a different stored
provenance field. Which scheduled path a configured schedule may use (calendar-free: direct; calendar-constrained: calendar applicable) is a **configuration** decision for Phase 24D; this module never silently converts one into the other.

## Not here

```text
no admission clock (admitted_at / created_at / received_at), no run id or UUID
no lifecycle state, claim, lease, lock, heartbeat or compare-and-swap (24C2B defines transitions and concurrency; 24D enforces them in storage)
no attempt, retry or backoff; no dispatch, enqueue, worker or persistence; no uniqueness query
the legacy backend/infrastructure/job_queue.py (uuid4 job ids, BackgroundTasks, Redis TTL, PENDING / RUNNING) is non-authoritative and not used
```
