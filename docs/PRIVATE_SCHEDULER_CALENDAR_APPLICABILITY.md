# Private Scheduler Calendar Applicability (Phase 24B3B)

| Checkpoint | Scope | Status |
|---|---|---|
| 24B2 | Civil recurrence and civil-date eligibility (`docs/PRIVATE_SCHEDULER_RECURRENCE.md`) | closed |
| 24B3A | External calendar evidence + SYSTEM-AS-OF binding (`docs/PRIVATE_SCHEDULER_CALENDAR_EVIDENCE.md`) | closed |
| 24B3B | Calendar applicability policy (this document) | implemented |
| 24C | Dispatch and run authority | deferred |
| 24D | Runtime / persistence | deferred |

The chain is `24B2 civil eligibility -> (materialized through) 24B1 candidate occurrence -> constrained by 24B3A PIT external-calendar evidence -> 24B3B applicability -> (later) 24C`.
There is deliberately no calendar-evidence-to-dispatch shortcut. Module: `backend/engine/private/scheduler_calendar_applicability.py` (pure, in the static-guard `PURE_MANIFEST`; standard library plus the
closed 24B2 / 24B1 / 24B3A public surface, no private helper).

```python
build_private_scheduler_calendar_constraint(*, calendar_key, calendar_revision, calendar_kind, applicability_mode, release_key)          # 5 stored fields
evaluate_private_scheduler_calendar_applicability(*, decision, calendar_binding, constraint) -> PrivateSchedulerCalendarApplicability     # 6 stored fields
require_private_scheduler_applicable_occurrence(*, applicability) -> PrivateSchedulerScheduledOccurrence
```

Statuses: `APPLICABLE`, `NOT_APPLICABLE` (complete evidence and the declared rule positively not satisfied), `UNAVAILABLE` (the rule cannot be proven). **UNAVAILABLE is not NOT_APPLICABLE.**
Eight reasons: `EXCHANGE_SESSION_PRESENT`, `EXCHANGE_NO_SESSION`, `SOURCE_RELEASE_PRESENT`, `SOURCE_RELEASE_NOT_PRESENT`, `SOURCE_RELEASE_TIME_REACHED`, `SOURCE_RELEASE_BEFORE_PLANNED_TIME`,
`SOURCE_RELEASE_DATE_ONLY`, `CALENDAR_COVERAGE_UNAVAILABLE`; only the specified status/reason pairs can occur (the constructor recomputes the canonical result).

## Preconditions (all raise ValueError)

- **Civil INELIGIBLE is rejected before calendar applicability**: 24B2 owns civil ineligibility; it never becomes an external-calendar status.
- The candidate occurrence comes only from `materialize_private_scheduler_civil_occurrence`; a nonexistent or ambiguous-under-REJECT wall time propagates from 24B1 as a failure, not as a calendar status.
- **Exact identity / date / timezone match**: calendar key, revision and kind equal the constraint; evidence `local_date` equals the decision date; evidence `timezone_key` equals the recurrence `timezone_key` exactly
  (no alias or UTC-offset equivalence such as `Etc/GMT-3` vs `Europe/Istanbul`, no nearest-date fallback, no search for another calendar).
- **Anti-lookahead**: `calendar PIT cutoff <= scheduled occurrence` by exact UTC instant (equality allowed; an earlier cutoff is valid because a scheduler may prepare ahead; one microsecond later is rejected).
  Together with the 24B3A guarantee `observed_at <= cutoff` the chain is `observed_at <= cutoff <= scheduled_for`, so a 17:00 calendar frontier cannot adjudicate a 09:55 occurrence.

## Policy

```text
coverage UNAVAILABLE      -> UNAVAILABLE / CALENDAR_COVERAGE_UNAVAILABLE first; empty tuples are not inspected (they mean closure / no release only under COMPLETE_FOR_DATE)
EXCHANGE_SESSION_EXISTS   -> sessions present: APPLICABLE / EXCHANGE_SESSION_PRESENT ; none: NOT_APPLICABLE / EXCHANGE_NO_SESSION
SOURCE_RELEASE_EXISTS     -> exact release key present (DATE_ONLY or EXACT_TIME): APPLICABLE / SOURCE_RELEASE_PRESENT ; absent: NOT_APPLICABLE / SOURCE_RELEASE_NOT_PRESENT
SOURCE_RELEASE_NOT_BEFORE_PLANNED_TIME
                          -> absent: NOT_APPLICABLE / SOURCE_RELEASE_NOT_PRESENT ; DATE_ONLY: UNAVAILABLE / SOURCE_RELEASE_DATE_ONLY ;
                             EXACT_TIME: candidate >= planned_for APPLICABLE / SOURCE_RELEASE_TIME_REACHED, earlier NOT_APPLICABLE / SOURCE_RELEASE_BEFORE_PLANNED_TIME
```

- **Exchange session exists is date-level.** The occurrence is not required to fall inside a session: pre-open analysis (09:55 with a 10:00-18:00 session) and post-close refresh (18:30) are applicable on a session date.
  A **half-day / shortened session is still a session**: no duration or normal-hours requirement.
- **Source release lookup** is by exact, case-sensitive key (`CPI` != `cpi`; no normalization, substring or fuzzy match). `SOURCE_RELEASE_EXISTS` is date presence only and makes no timing statement.
- **Not-before mode**: DATE_ONLY entries are insufficient (no midnight or other time is fabricated) so the result is UNAVAILABLE; EXACT_TIME entries compare canonical UTC instants with no tolerance
  (planned 10:00Z: 09:59:59.999999Z is NOT_APPLICABLE; 10:00:00Z and 10:00:00.000001Z are APPLICABLE). A methodology's `+1` / `+5` minute delay is the recurrence's wall-clock choice; there is no offset arithmetic here.
- **Planned release is not actual availability**: `SOURCE_RELEASE_TIME_REACHED` means only that the configured occurrence is not earlier than the calendar's planned release instant. It does not mean data was published,
  available, fetched or ingested, and no such field exists.

## Result contract and helper

`PrivateSchedulerCalendarApplicability` retains the decision, calendar binding and constraint by identity, derives the candidate through 24B1 and recomputes candidate, status and reason on direct construction through
the same private derivation as the evaluator (a forged status, reason, candidate, constraint or late cutoff is rejected). `require_private_scheduler_applicable_occurrence` returns the already materialized candidate by
identity for `APPLICABLE` and raises `ValueError` for `NOT_APPLICABLE` and `UNAVAILABLE`; it has no dispatch or other side effect.

## Non-goals

```text
no dispatch, run lifecycle, retry, queue or persistence (24C / 24D); no provider adapter or network (BIST, NYSE, TUIK, TCMB)
no weekend or holiday inference, no session-hour derivation, no session-containment check, no fabricated release plan; no ambient clock
```
