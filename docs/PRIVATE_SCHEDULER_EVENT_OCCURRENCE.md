# Private Scheduler Event Occurrence (Phase 24C1)

| Layer | Authority | Status |
|---|---|---|
| 24A | Generic trigger envelope (idempotency hash is opaque caller input) | closed |
| 24B | Scheduled deterministic identity (24B1) + recurrence + calendar applicability | closed |
| 24C1 | Event-driven deterministic identity (this document) | implemented |
| 24C2 | Unified run admission, idempotent lifecycle / concurrency authority | deferred |
| 24D | Persistence / queue / runtime integration | deferred |

Module: `backend/engine/private/scheduler_event_occurrence.py` (pure, in the static-guard `PURE_MANIFEST`; standard library plus the closed 24A public surface).

```python
build_private_scheduler_event_occurrence(*, work_kind, scope, owner_id, portfolio_id, event_cause_kind, cause_key, cause_available_at, policy_key, policy_revision)
PrivateSchedulerEventOccurrence(trigger)      # exactly one stored field
```

## Why the raw 24A caller hash is not the production event path

24A accepts `idempotency_sha256` as opaque caller authority. That is enough for the primitive envelope but lets any caller attach any 64-hex value to an event trigger. The scheduled side already derives
its identity in 24B1; 24C1 is the symmetric event-driven path. The builder takes nine inputs and has **no** argument for the trigger kind, `scheduled_for` or the idempotency hash: they are canonical or derived.

## Delegation

24A stays the semantic authority for the work / scope matrix, owner and portfolio UUIDs, cause key, aware datetimes, policy key and revision. The builder calls `build_private_scheduler_trigger` first with a fixed
placeholder hash purely as a validation probe (never returned, stored or used as identity), then with the derived hash. `PrivateSchedulerEventOccurrence` requires an exact EVENT_DRIVEN trigger (a scheduled trigger,
including a 24B1 occurrence trigger, raises `ValueError`).

## Domain-separated canonical hashing

SHA-256 over one canonical JSON document (UTF-8, `sort_keys=True`, compact separators, explicit nulls, enum values, canonical UUID strings; no `repr`, object hash, pickle, salt, `uuid4` or clock). The payload starts with the
single fixed marker `sentinax.private.scheduler.event-occurrence.v1` (distinct from the scheduled-occurrence domain) and contains exactly these twelve dimensions:

```text
protocol, trigger_kind ("event_driven"), work_kind, scope, owner_id, portfolio_id, scheduled_for (null),
event_cause_kind, cause_key, cause_available_at, policy_key, policy_revision
```

No runtime or ambient field (created / received time, attempt, job or worker id) participates. The tests recompute the hash independently from a hand-written payload.

## UTC instant canonicalization, stored form preserved

`cause_available_at` is canonicalized to UTC with microsecond precision **for the hash only**: `07:00Z`, `10:00+03:00` and `02:00-05:00` give the same hash (all other fields equal), while the stored 24A trigger keeps the
caller's original aware datetime. One microsecond later is a different identity. The cause key is hashed exactly as stored (`CPI` != `cpi`; no case folding, trimming or normalization).

## Identity consequences

```text
same inputs                      -> equal occurrence, same hash
different owner or portfolio     -> different hash (owner isolation)
different work kind              -> different hash (no global event-only deduplication)
different event cause kind       -> different hash (a cause key alone is not enough)
different policy key / revision  -> different hash (policy revision is part of run identity)
```

## Forge resistance

Direct construction recomputes the canonical hash from the trigger's own fields and rejects any EVENT_DRIVEN trigger whose hash is merely syntactically valid (an arbitrary value, or a hash taken from another work kind, owner, policy
revision or cause). This is the reason the wrapper exists.

## Not here

```text
no event-occurrence proof (the explicit envelope is canonicalized, never verified; cause_available_at is not compared with a clock and may be historical or future)
no previous-run lookup, duplicate search, event history or revision resolution (uniqueness is a persistence concern)
no dispatch, enqueue, worker, run state, lifecycle, concurrency claim / lease / lock, retry or persistence
the legacy backend/infrastructure/job_queue.py (uuid4 job ids, BackgroundTasks, Redis TTL, PENDING / RUNNING states) and the legacy scheduler are non-authoritative and not used
```
