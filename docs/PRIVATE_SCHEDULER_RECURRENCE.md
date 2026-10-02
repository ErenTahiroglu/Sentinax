# Private Scheduler Civil Recurrence (Phase 24B2)

| Checkpoint | Scope | Status |
|---|---|---|
| 24A | Immutable trigger envelope (`docs/PRIVATE_SCHEDULER_TRIGGER.md`) | closed |
| 24B1 | Timezone / wall-time to canonical UTC occurrence (`docs/PRIVATE_SCHEDULER_SCHEDULED_OCCURRENCE.md`) | closed |
| 24B2 | Versioned civil recurrence and explicit-date eligibility (this document) | implemented |
| 24B3 | Market / trading / source-release calendar authority | deferred |
| 24C | Dispatch and run authority | deferred |
| 24D | Runtime / persistence integration | deferred |

24B2 answers only: given ONE explicit local calendar date, does this versioned **civil** recurrence consider that date eligible, and if so, materialize the exact scheduled
occurrence through the closed 24B1. Module: `backend/engine/private/scheduler_recurrence.py` (pure, in the static-guard `PURE_MANIFEST`; standard library plus the closed 24A / 24B1 public surface).

```python
build_private_scheduler_recurrence(*, schedule_key, schedule_revision, timezone_key, local_time, ambiguous_time_policy, recurrence_kind, weekdays, day_of_month,
                                   work_kind, scope, owner_id, portfolio_id, policy_key, policy_revision)            # 14 stored fields
evaluate_private_scheduler_civil_date(*, recurrence, local_date) -> PrivateSchedulerCivilDateDecision                 # 3 stored fields
materialize_private_scheduler_civil_occurrence(*, decision) -> PrivateSchedulerScheduledOccurrence
```

## Civil calendar is not a market calendar

Eligibility uses only the Gregorian civil calendar of Python `date`. An eligible date does not mean a market is open, a source is available, trading is allowed or a publication is
guaranteed. A weekend is an ordinary civil date: `DAILY` is eligible on a Sunday and `WEEKLY(SUNDAY)` is eligible on a Sunday; `WEEKLY(MONDAY..FRIDAY)` is ineligible on a Sunday only because Sunday
is not listed, not because markets are closed. There is no holiday logic and no business-day adjustment; 24B3 owns exchange, TEFAS, release-calendar and holiday semantics.

## Kinds and shapes

```text
DAILY                  weekdays == () and day_of_month is None; every civil date is eligible
WEEKLY                 weekdays: exact non-empty tuple of unique PrivateSchedulerWeekday in Monday -> Sunday order (validated, never sorted); day_of_month None
MONTHLY_DAY_OF_MONTH   day_of_month: exact int in [1, 31] (bool rejected); weekdays == ()
```

No cron, RRULE, business-day or market recurrence. `PrivateSchedulerWeekday` is stored as an enum, never as a raw Python weekday integer (an internal tuple maps `date.weekday()` to it).

## Short-month skip semantics

A month lacking the configured day (29, 30 or 31) simply has no eligible date. It is never moved to month end, to the next month, backward or to a business day: April has no 31st, and February 2023 has
no 29th to evaluate (February 29, 2024 is eligible for day 29).

## One explicit local date per decision; no next-run search

`evaluate_private_scheduler_civil_date` evaluates exactly one supplied `date` (a `datetime` or subclass is rejected). There is no next-run or previous-run calculation, date iteration, range scan, search horizon,
active date range (start / end / effective window; revision activation is a later persistence concern) and no ambient clock: historical and future dates are equally valid, as replay needs.
The decision retains the recurrence by object identity and recomputes eligibility on direct construction, rejecting forged values.

## Materialization and DST remain 24B1 authority

An eligible decision is materialized by calling `build_private_scheduler_scheduled_occurrence` with the recurrence fields and the decision date; no UTC, DST or idempotency logic is duplicated. An ineligible
decision raises `ValueError` (no `None`, no skipped occurrence, no alternate date). Civil eligibility is not a valid timezone occurrence: for `America/New_York` 02:30 the date 2024-03-10 is civil-eligible but
24B1 rejects the nonexistent wall time, and an ambiguous 01:30 on 2024-11-03 fails under `REJECT` and materializes the closed EARLIER / LATER instants otherwise. 24B2 never alters the date, time or ambiguity
policy to rescue a failure.

## Construction-time validation (chosen approach)

A recurrence has no real date, and a date-specific probe would wrongly reject valid schedules whose wall time is DST-sensitive on some other date. Therefore:

1. work kind, scope, owner, portfolio, policy key and policy revision are validated by delegating to the closed 24A `build_private_scheduler_trigger` with a fixed aware UTC instant used **only as a validation probe**
   (no timezone or DST involved, never stored, no effect on identity, trigger discarded; it is not a schedule date);
2. the few date-independent 24B1 fields (schedule key / revision grammar, timezone-key availability through `ZoneInfo`, exact naive `time` with fold 0, policy enum) get small local checks that a parity test pins to the
   closed 24B1 behaviour (same exception class for the same input);
3. 24B1 re-validates everything on every actual date at materialization.

Constructing a recurrence does **not** prove that every future wall occurrence is valid.

## Non-goals

```text
no market / source calendar, holiday library, BIST / NYSE / TEFAS / TCMB / TUIK / Fed / SEC calendar, business-day adjustment
no next-run search, hardcoded schedule slots, polling interval or ambient clock
no dispatch, run state, retry, queue or persistence; the legacy backend/infrastructure/scheduler.py is not used
```
