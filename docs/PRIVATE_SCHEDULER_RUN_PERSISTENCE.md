# Private Scheduler Run Persistence (Phase 24D1)

| Layer | Authority | Status |
|---|---|---|
| 24C2A | Run admission (`docs/PRIVATE_SCHEDULER_RUN_ADMISSION.md`) | closed |
| 24C2B | Lifecycle / claim-lease transitions (`docs/PRIVATE_SCHEDULER_RUN_LIFECYCLE.md`) | closed |
| 24D1 | Database persistence authority: `supabase/migrations/024_private_scheduler_run_persistence.sql` (this document) | implemented |
| 24D2 | Canonical Python persistence codec + RPC repository | deferred |
| 24D3 | Runtime worker + dispatch integration | deferred |

24C2A and 24C2B stay the **semantic** authorities in Python. PostgreSQL adds what pure code cannot: durable uniqueness, transactional atomicity, an optimistic compare-and-swap, immutable history and
defense-in-depth structural constraints. Migration 024 follows 023 and edits no earlier migration.

## Tables

- `public.private_scheduler_runs`: the current snapshot. **The run hash is the primary key**: `run_idempotency_sha256 VARCHAR(64)`, `^[0-9a-f]{64}$`, constraint `pk_private_scheduler_runs`; there is no random row id. Immutable
  admission columns (source, trigger kind, work kind, scope, owner, portfolio, scheduled_for, cause kind / key / availability, policy key / revision) and an immutable `admission_payload JSONB` object (the complete 24C2A audit provenance;
  its canonical JSON codec is 24D2) are followed by the lifecycle columns (`state`, `state_version`, `claim_key`, `claimed_at`, `lease_expires_at`, `terminal_at`, `failure_code`) and the metadata clocks `created_at` / `updated_at`.
- `public.private_scheduler_run_transitions`: the append-only history, `PRIMARY KEY (run_idempotency_sha256, after_state_version)` with a `FOREIGN KEY ... ON DELETE RESTRICT` to the run.

Constraints mirror the closed Python universes (admission sources, trigger kinds, work kinds, scopes, event causes, states, transition kinds), the source / kind matrix, the system-vs-portfolio owner isolation, the work / scope matrix, the scheduled
vs event trigger shapes, the policy and claim / failure code grammars and the exact READY / CLAIMED / SUCCEEDED / FAILED shapes (including `claimed_at < lease_expires_at` and the active-window terminal rule). No legacy states, no retry columns,
no result payload.

## Current snapshot vs append-only history; immutability and finality

A `BEFORE UPDATE` guard rejects any change to the immutable admission columns, any update of a `succeeded` / `failed` row (terminal rows are final) and any update that is not exactly `state_version + 1`. A `BEFORE DELETE` guard and the
revoked `DELETE` privilege make runs durable. History rows can never be updated, deleted or truncated (triggers + no grant). Database clocks are used only for `created_at`, `updated_at` and `recorded_at` (persistence metadata, never logical
identity or a domain instant).

## transition_at persistence (the renewal gap)

The pure 24C2B transition record cannot reconstruct renewed_at, because it does not store it. 24D1 therefore persists one `transition_at` per history row: `claim` = `claimed_at`, `renew_claim` = `renewed_at`, `take_over_expired_claim` = the new
`claimed_at`, `succeed` / `fail` = `terminal_at`, `initialize` = `NULL`. The current snapshot keeps the original `claimed_at` after a renewal while the history preserves the exact renewal instant; nothing is inferred later.

## Idempotent initialization

`initialize_private_scheduler_run` receives only the immutable admission fields and creates `ready` / version 1 plus the `initialize` history row in one subtransaction. Results: `initialized`, `idempotent_duplicate` (same run hash and
identical immutable identity and payload; the lifecycle is never reset) or `conflict` (same run hash, different identity or payload; nothing is mutated). It follows the migration-015 race-safe pattern: only a violation of
`pk_private_scheduler_runs` may enter the duplicate / conflict path after re-reading the authoritative row; any other unique violation is re-raised.

## state_version CAS RPC

`apply_private_scheduler_run_transition(p_run_idempotency_sha256, p_expected_version, p_transition_kind, p_claim_key, p_transition_at, p_lease_expires_at, p_failure_code)` takes seven explicit inputs and never an after-state, after-version,
`claimed_at` or `terminal_at`: it derives the new snapshot from the persisted row, the kind and the explicit inputs. Every UPDATE carries the compare-and-swap predicate (`run_idempotency_sha256` and `state_version = p_expected_version`)
plus the transition-specific state and claim predicates, and the history INSERT is in the same transaction (a history failure rolls the update back). Results: `applied`, `not_found`, `version_conflict` (the run exists at another version)
and `transition_conflict` (version matches but the transition preconditions do not). The SQL transition semantics mirror the closed 24C2B rules while Python stays the semantic authority:

```text
claim                    ready only; p_transition_at >= scheduled_for (scheduled) | cause_available_at (event); p_transition_at < p_lease_expires_at
renew_claim              claimed, same claim key, claimed_at <= p_transition_at < lease_expires_at, new expiry strictly later; original claimed_at kept
take_over_expired_claim  claimed, different claim key, p_transition_at >= current lease_expires_at (equality allowed), new expiry > p_transition_at
succeed / fail           claimed, exact current claim key, claimed_at <= p_transition_at < lease_expires_at; lease parameter NULL; fail needs a failure code
```

Terminal rows never satisfy a branch (`transition_conflict`); after a takeover the old claim key fails renew, succeed and fail. A valid Python transition does not prove a race was won: the SQL compare-and-swap does.

## RLS, privileges and the service-role-only write surface

RLS is enabled on both tables with **no policies**; all privileges are revoked from `PUBLIC`, `anon` and `authenticated`; `service_role` receives only `SELECT, INSERT, UPDATE` on runs and `SELECT, INSERT` on history (no `DELETE`).
Both functions are `VOLATILE`, `SECURITY INVOKER`, `SET search_path = public, pg_temp`, fully qualify tables, and `EXECUTE` is revoked from `PUBLIC`, `anon` and `authenticated` and granted to `service_role` only: browsers cannot initialize, claim or transition runs.
service_role permission is not semantic permission to fabricate a lifecycle; 24D2 will expose only canonical Python serialization and repository methods.

## Not here

```text
no Python codec or repository, no Supabase client calls (24D2); no worker, queue, dispatch or runtime (24D3)
no retry (FAILED is terminal), no result payload, no exception text, no delete path, no generated or random identity
the legacy scheduler / job-queue tables and concepts are neither modified nor reused
```
