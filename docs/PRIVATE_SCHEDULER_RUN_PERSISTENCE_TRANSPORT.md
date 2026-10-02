# Private Scheduler Run Persistence Transport (Phase 24D2B1)

Module: `backend/engine/private/scheduler_run_persistence_transport.py` (pure; registered in the static-guard PURE manifests).
It defines what a raw PostgREST row of migration 024 means. It performs no database call, no RPC, no repository work, no clock read, no randomness and
no hashing. The repository (Phase 24D2B2) is not part of this checkpoint.

## Projections

`PRIVATE_SCHEDULER_RUN_SELECT` and `PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT` are explicit comma-joined column lists (no `*`) equal, in order, to the
columns of `private_scheduler_runs` and `private_scheduler_run_transitions`. A row must be a `Mapping` with exactly those `str` keys.

## Current-run hydration

`hydrate_private_scheduler_persisted_run(*, row)`:

1. `admission_payload` (exact `dict`) is rebuilt through the closed 24D2A codec; no admission is ever forged.
2. Every immutable denormalized column (run hash, source, trigger kind, work kind, scope, owner, portfolio, scheduled_for, event cause kind, cause key,
   cause_available_at, policy key, policy revision) is reconciled with the reconstructed admission. Payload and columns are double validation; any
   disagreement raises.
3. Lifecycle columns are exact-typed, canonicalized to UTC and passed to the closed `PrivateSchedulerRunLifecycle`, whose shape rules are the only authority.
4. `created_at <= updated_at` (database metadata clocks, never identity).

Result: `PrivateSchedulerPersistedRun(lifecycle, created_at, updated_at)`.

## Two timestamp policies

- Payload JSONB keeps exact audit offset strings (24D2A), including non-UTC and second-level offsets. The hydrated admission keeps that audit offset.
- PostgreSQL `TIMESTAMPTZ` columns return as the equivalent instant (`Z`, `+00:00`, other offsets, variable fractional precision). They are parsed as
  aware ISO-8601 instants, canonicalized to UTC and compared by instant, never lexically. Naive, malformed and non-string values are rejected.

## History hydration

`hydrate_private_scheduler_persisted_transition(*, row, admission)` requires a closed admission whose run hash equals the row's. The result keeps
`transition_at` separately from the closed lifecycle snapshot, because a renewal instant is not representable in a lifecycle.

- INITIALIZE: `before_state_version` None, `transition_at` None, `after.state_version == 1`.
- Other kinds: `after.state_version == before_state_version + 1` and a non-null `transition_at`; CLAIM and TAKE_OVER equal `after.claimed_at`; SUCCEED
  and FAIL equal `after.terminal_at`; a renew must satisfy `after.claimed_at <= transition_at < after.lease_expires_at`. Only CLAIM is a version-2 step (before 1, after 2); renew,
  takeover, succeed and fail need `before_state_version >= 2` because a CLAIMED predecessor is required.
- `recorded_at` is metadata, parsed to UTC, never inferred or compared.

## Limitations

- renew_claim: a persisted row proves `claimed_at <= transition_at < new lease expiry`, but not `transition_at < prior lease expiry` (and not that the
  lease only extends); both need the prior snapshot and are deferred to D2B2 predicted-domain/RPC reconciliation. The migration's append-only triggers bound it.
- There is no payload hash: audit-only payload fields that influence neither derivation nor run identity are not authenticated (codec limit unchanged).
- tzdb-version provenance, repository access, worker and real-database tests remain deferred (24D2B2, 24D3).
