# Private Scheduler Run Persistence Codec (Phase 24D2A)

| Layer | Authority | Status |
|---|---|---|
| 24C2A | Closed run admission | closed |
| 24D1 | Migration 024: runs, CAS RPC, history (`docs/PRIVATE_SCHEDULER_RUN_PERSISTENCE.md`) | closed |
| 24D2A | Canonical `admission_payload` codec (this document) | implemented |
| 24D2B | RPC-only repository, current-row / lifecycle hydration | deferred |
| 24D3 | Runtime worker / dispatch | deferred |

Module: `backend/engine/private/scheduler_run_persistence_codec.py` (pure, in the static-guard `PURE_MANIFEST`; standard library plus the closed Phase 24 public surfaces; no JSON transport, hashing, database, network, clock or randomness).

```python
serialize_private_scheduler_run_admission(*, admission: PrivateSchedulerRunAdmission) -> dict[str, object]
hydrate_private_scheduler_run_admission(*, payload: dict[str, object]) -> PrivateSchedulerRunAdmission
```

## Migration 024 `admission_payload` boundary

The column is deliberately schema-opaque at SQL level (a JSONB object, immutable after initialization). This codec defines what it means. The payload is audit provenance, **not** run identity: the logical identity stays
`admission.run_idempotency_sha256`; the codec stores and verifies it, never hashes the payload and adds no `payload_sha256`, row id or UUID.

## Top-level protocol: exactly four keys

```text
protocol                 "sentinax.private.scheduler.run-admission-persistence.v1"   (fixed; any other value is rejected, no best-effort compatibility)
source                   scheduled_direct | scheduled_calendar_applicable | event_driven
run_idempotency_sha256   the closed admission run hash
provenance               the exact source-specific object
```

## Three source-specific payloads

- `scheduled_direct`: `{scheduled_occurrence: {schedule_key, schedule_revision, timezone_key, local_date, local_time, ambiguous_time_policy, work_kind, scope, owner_id, portfolio_id, policy_key, policy_revision, expected_scheduled_for, expected_idempotency_sha256}}`.
- `event_driven`: `{event_occurrence: {work_kind, scope, owner_id, portfolio_id, event_cause_kind, cause_key, cause_available_at, policy_key, policy_revision, expected_idempotency_sha256}}`.
- `scheduled_calendar_applicable`: `{civil_decision: {recurrence (all 14 closed 24B2 fields), local_date, expected_eligibility}, calendar_binding: {evidence (all 12 closed 24B3A fields), knowledge_cutoff}, calendar_constraint (5 fields), expected_applicability_status, expected_applicability_reason, expected_candidate_idempotency_sha256}`.
  The candidate occurrence is never serialized independently; it is re-derived.

## JSON-native values, canonical scalars

Only `dict`, `list`, `str`, `int`, `bool` and `None`. Enums by exact `.value` (no names, no case normalization); UUIDs as canonical lowercase strings; dates `YYYY-MM-DD`; local times `HH:MM:SS.ffffff`; every datetime an ISO-8601 string with microseconds and an
explicit offset. **Offset-preserving audit timestamps**: a non-UTC aware instant keeps its supplied offset (`2026-10-02T10:00:00.250000+03:00`); canonical-UTC fields stay `+00:00`. Tuples become lists in the supplied order (exchange sessions and source releases are
never sorted or deduplicated); empty lists and nulls are always written. `COMPLETE_FOR_DATE` with no entries and `UNAVAILABLE` with no entries therefore stay distinct, and a `DATE_ONLY` release keeps `planned_for = null`.

## Closed-builder hydration, derived-result assertions, run-hash revalidation

Hydration rebuilds only through the closed public builders, evaluators and admission builders (24B1 / 24B2 / 24B3A / 24B3B / 24C1 / 24C2A); it never constructs a raw trigger, occurrence, decision, applicability or admission. The only direct constructors are the two leaf evidence
types without a public builder (exchange session window, source release entry). Persisted `expected_*` values (scheduled instant, eligibility, applicability status and reason, candidate / occurrence hashes) are **derived-result assertions**, not builder inputs: historical
adjudication must not silently change under future code drift, so hydration fails closed if the current closed logic no longer derives them. The reconstructed run hash must equal the stored top-level hash. A calendar proof must still derive `APPLICABLE`.

## Exact key sets, exact types, strict parsing

Every nested object must have exactly its expected key set (missing, extra or renamed keys fail; explicit `null` is not missing; no `.get()` defaults). Primitives are exact: `type(value) is int` (a bool is not an int), `str`, `list`, `dict` (no subclasses, no tuples); the
top-level input must be an exact `dict` (no JSON string). UUIDs must satisfy `str(UUID(value)) == value`; dates, times and datetimes must match the canonical regex and round-trip to the identical string (naive, date-only, `Z`, missing microseconds or trailing junk are rejected). Integer
domain ranges are left to the closed builders.

## Limits (stated plainly)

With no payload hash the codec cannot authenticate audit fields that influence neither the derivation nor the run identity: e.g. the calendar `source_content_sha256`, an earlier-but-valid knowledge cutoff, or the same instant written with another offset hydrate to a different but valid
admission with the same run hash. Their integrity rests on the immutable database column. **tzdb-version provenance remains unresolved / deferred**: the payload carries no tz database version, so replay under changed IANA rules is not addressed here.

## Not here

```text
no lifecycle / transition codec (24D2B hydrates the current row from this payload plus the authoritative lifecycle columns)
no database, repository, RPC, network, clock, randomness or hashing; migration 024 is unchanged
```
