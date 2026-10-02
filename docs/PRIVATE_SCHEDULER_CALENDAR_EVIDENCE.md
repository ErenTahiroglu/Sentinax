# Private Scheduler Calendar Evidence (Phase 24B3A)

| Checkpoint | Scope | Status |
|---|---|---|
| 24A | Trigger envelope | closed |
| 24B1 | Local wall time to canonical UTC occurrence | closed |
| 24B2 | Civil recurrence and explicit-date eligibility (`docs/PRIVATE_SCHEDULER_RECURRENCE.md`) | closed |
| 24B3A | External calendar evidence boundary (this document) | implemented |
| 24B3B | Calendar applicability: civil decision + calendar evidence | deferred |
| 24C | Dispatch and run authority | deferred |
| 24D | Runtime / persistence | deferred |

**24B2 civil recurrence is not an external calendar.** 24B3A is evidence only; applicability is decided later in 24B3B. Module:
`backend/engine/private/scheduler_calendar_evidence.py` (pure, standard library only, imports nothing from 24A / 24B1 / 24B2, in the static-guard `PURE_MANIFEST`).

```python
build_private_scheduler_calendar_date_evidence(*, calendar_key, calendar_revision, calendar_kind, timezone_key, local_date, coverage, exchange_sessions,
                                               source_releases, source_key, published_at, observed_at, source_content_sha256)     # 12 stored fields
bind_private_scheduler_calendar_pit(*, evidence, knowledge_cutoff) -> PrivateSchedulerCalendarPITBinding                          # 2 stored fields
```

## Exchange session windows, not an open / closed bool

Official calendars contain full days, half days, early closes and complete closures, so the evidence stores exact session windows
(`PrivateSchedulerExchangeSessionWindow(opens_at, closes_at)`): canonical UTC (`tzinfo is timezone.utc`, non-UTC equivalents rejected rather than converted),
`opens_at < closes_at`. A shortened close is simply a different `closes_at`; there is no half-day flag. Every boundary must fall on the evidence local date in the evidence timezone
(same-local-date sessions only; overnight sessions would need a new explicit contract). The supplied tuple must be chronological and non-overlapping (adjacent windows allowed, never sorted).

## Complete vs unavailable

```text
COMPLETE_FOR_DATE   the evidence completely describes this calendar/date under the represented snapshot:
                    exchange sessions == ()  -> explicit full closure;   source releases == () -> explicit "no planned release"
UNAVAILABLE         completeness cannot be proven: tuples must be empty and the meaning is UNKNOWN
                    (never "closed", never "no release")
```

A full closure (COMPLETE + empty sessions) and unavailable evidence (UNAVAILABLE + empty sessions) are distinct values. Likewise explicit no-release differs from unavailable.

## Source release plans

`PrivateSchedulerSourceReleasePlanEntry(release_key, time_precision, planned_for)`: `DATE_ONLY` identifies the release date and carries `planned_for = None` (no midnight, 09:00 or recurrence time is
invented); `EXACT_TIME` carries a canonical UTC planned instant that must map to the evidence local date. `release_key` is an exact printable trimmed string (1..128, case preserved), unique inside one
evidence object; nothing is deduplicated or prioritized. **A planned release is not actual source availability**: `planned_for` is only the instant the calendar planned the release. It is not
`published_at`, not `observed_at` and not ingestion success; actual availability remains provider / PIT evidence elsewhere (the macro release-calendar date, actual source availability and Sentinax
`observed_at` stay three separate things).

## Provenance and SYSTEM-AS-OF binding

`calendar_key` and `source_key` are strict lowercase identifiers (no value hardcoded; no source tier inferred), `calendar_revision` an exact `int >= 1` (a Sentinax contract revision, not provider completeness),
`timezone_key` an exact IANA key resolved through `ZoneInfo` (no offset table or UTC fallback), `local_date` an exact `date`, and `source_content_sha256` an opaque 64-lowercase-hex artifact reference (nothing hashed,
no authenticity claim). `observed_at` is when Sentinax observed the **calendar artifact**, not when any economic data was released; `published_at` is `None` or an aware instant not later than `observed_at`.

`PrivateSchedulerCalendarPITBinding` retains the evidence by identity and requires, by exact UTC instant (equality allowed, one microsecond late rejected), `observed_at <= knowledge_cutoff` and, when present,
`published_at <= knowledge_cutoff`. This is SYSTEM-AS-OF only, with no mode enum: a public calendar that existed earlier does not authorize a replayed Sentinax run to pretend it had already ingested it.

## Non-goals

```text
no provider adapter or network (Borsa Istanbul, NYSE, TUIK, TCMB, KAP, scraping); no hardcoded exchange, holiday, half-day or release table
no weekend inference: Saturday / Sunday imply nothing; only COMPLETE evidence with no sessions means closed, UNAVAILABLE is unknown
no 24B2 decision consumption, no occurrence filtering, no run / skip decision (24B3B); no ambient clock
no dispatch, run state, retry or persistence; the legacy backend/infrastructure/scheduler.py is not used
```
