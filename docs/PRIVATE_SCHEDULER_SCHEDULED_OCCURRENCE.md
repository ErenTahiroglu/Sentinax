# Private Scheduler Scheduled Occurrence (Phase 24B1)

| Checkpoint | Scope | Status |
|---|---|---|
| 24A | Trigger envelope, causality, owner scope (`docs/PRIVATE_SCHEDULER_TRIGGER.md`) | closed |
| 24B1 | Explicit local wall-time materialization, timezone safety, deterministic scheduled idempotency (this document) | implemented |
| 24B2 | Recurrence and calendar / date eligibility, versioned schedule policy | deferred |
| 24C | Event-driven dispatch and run authority | deferred |
| 24D | Runtime / persistence integration | deferred |

24A accepts an exact aware `scheduled_for` but does not calculate it. 24B1 turns ONE explicit IANA timezone, local calendar date and local wall-clock
time into one deterministic UTC occurrence instant and materializes the closed 24A `PrivateSchedulerTrigger` (always `SCHEDULED`, no event cause) with a
deterministic scheduled-occurrence idempotency identity.

Module: `backend/engine/private/scheduler_scheduled_occurrence.py` (pure, in the static-guard `PURE_MANIFEST`; standard library plus the closed 24A surface).

```python
build_private_scheduler_scheduled_occurrence(*, schedule_key, schedule_revision, timezone_key, local_date, local_time, ambiguous_time_policy,
                                             work_kind, scope, owner_id, portfolio_id, policy_key, policy_revision)
```

Twelve keyword-only inputs; the UTC instant and the idempotency hash are derived, never caller supplied. The occurrence stores seven fields (`schedule_key`,
`schedule_revision`, `timezone_key`, `local_date`, `local_time`, `ambiguous_time_policy`, `trigger`); the UTC instant lives only at `occurrence.trigger.scheduled_for`.

## IANA timezone authority

`zoneinfo.ZoneInfo(timezone_key)` is the only time-rule authority. `tzdata` is already in `backend/requirements.txt` as the platform fallback. There is no private
offset table, no alias normalization, no manual UTC offset, no UTC fallback and no network. `timezone_key` is an exact, trimmed, printable str of 1..128 characters.
`local_date` must be an exact `date` (a `datetime` is rejected) and `local_time` an exact naive `time` with `fold == 0`; ambiguity is controlled only by the explicit policy.

## Local wall time vs UTC occurrence; DST safety

The naive `replace(tzinfo=zone)` result is never trusted. For fold 0 and fold 1 the wall time is attached to the zone, converted to UTC and round-tripped back; a candidate is
accepted only if the round trip reproduces date, hour, minute, second and microsecond exactly.

```text
no valid candidate           nonexistent (spring-forward gap): always rejected, no silent DST shift in either direction
one UTC instant              unambiguous: used under every policy
two different UTC instants   ambiguous (repeated hour): REJECT fails closed; EARLIER / LATER choose the chronologically earlier / later UTC instant
```

`PrivateSchedulerAmbiguousTimePolicy` is versioned schedule configuration and part of the idempotency identity even when the wall time is unambiguous. The canonical
trigger instant is UTC (`trigger.scheduled_for.tzinfo is timezone.utc`).

## Deterministic, domain-separated idempotency

`idempotency_sha256` is SHA-256 over one canonical UTF-8 JSON document (`sort_keys=True`, compact separators, explicit nulls, UUID canonical strings, enum values,
microsecond isoformat timestamps; no `repr`, object hash, binary serialization or salt). It begins with the fixed protocol marker
`sentinax.private.scheduler.scheduled-occurrence.v1` (domain separation from any future event-driven hashing) and includes the schedule key and revision, timezone key, local date and
time, ambiguity policy, the resolved UTC instant, work kind, scope, owner, portfolio, policy key and policy revision. Schedule configuration is part of the logical identity: two
schedules that resolve to the same UTC instant but differ in schedule key, zone or local wall-clock configuration get different hashes.

## 24A delegation and forge resistance

24A remains the authority for work kind, scope, owner, portfolio, policy key and revision (the materializer calls `build_private_scheduler_trigger`). Direct construction of the
occurrence recomputes the expected UTC instant and identity and rejects a trigger with a wrong instant, a non-UTC representation, a wrong hash, an `EVENT_DRIVEN` kind or other authority.

## Not decided here

```text
no recurrence (daily, weekly, monthly, cron, RRULE, next / previous occurrence, date iteration)
no market / trading calendar, holiday, TEFAS publication day or source-release calendar; a valid wall time is not proof that a market or source should run that day
no hardcoded methodology clock times; no ambient clock and no past / future validation (historical and future occurrences may be materialized, as replay needs)
no network, provider call, dispatch, run state, retry, lease or persistence; the legacy backend/infrastructure/scheduler.py is not used
```
