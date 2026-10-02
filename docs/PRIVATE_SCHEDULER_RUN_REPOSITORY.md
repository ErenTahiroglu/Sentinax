# Private Scheduler Run Repository (Phase 24D2B2)

Module: `backend/engine/private/scheduler_run_repository.py` (not a PURE module; it calls an injected client). It connects the closed scheduler domain to
migration 024. It is not a lifecycle authority.

## Layers

- 24D1: PostgreSQL owns run uniqueness, the row lock, the `state_version` CAS, the atomic current-row update and the append-only history insert.
- 24D2A: canonical `admission_payload` codec.
- 24D2B1: PostgREST current/history row hydration (`PRIVATE_SCHEDULER_RUN_SELECT`, `PRIVATE_SCHEDULER_RUN_TRANSITION_SELECT`).
- 24D2B2: this repository: serialize, call RPC, hydrate, reconcile against the closed 24C2B prediction.

## Trust boundary

`PrivateSchedulerRunRepository(client)` is trusted backend / service-role infrastructure that handles SYSTEM and PORTFOLIO runs. It takes an injected client
(no client creation, no environment, no clock, no randomness, no owner filter). It must not be exposed directly to untrusted, user-supplied run hashes;
future API boundaries apply their own authorization.

## Write discipline

Migration 024 grants service_role direct table DML, so the repository imposes more: the two scheduler tables are only read
(`.table().select(projection).eq().limit(2).execute()`); every write goes through `initialize_private_scheduler_run` or
`apply_private_scheduler_run_transition`. No insert/update/upsert/delete and no dynamic RPC name (source-tested).

## Initialization

`initialize_run(admission=...)` serializes through the closed codec, builds the 14 RPC parameters from the closed admission and calls the RPC once.

- `initialized`: RPC must say READY / version 1.
- `idempotent_duplicate`: the run may already have advanced; the RPC observation is returned as is.
- both: the durable INITIALIZE history row at version 1 is read and verified against the supplied admission; otherwise RuntimeError.
- `conflict`: no history read, no adoption, `initialize_transition = None`.

## Transitions

`claim_run`, `renew_claim`, `take_over_expired_claim`, `succeed_run`, `fail_run`: the closed C2B function predicts first (an invalid command makes zero RPC
calls), then exactly one apply RPC. No automatic retry, no reload-and-retry, no repaired expected_version; conflicts are returned to the caller (24D3 owns
runtime policy). Statuses: `applied`, `not_found` (state/version None), `version_conflict` (version differs from expected), `transition_conflict` (version
equals expected). Non-applied results carry no predicted or persisted transition. Contradictory RPC answers are RuntimeError.

## APPLIED reconciliation

The RPC state/version must equal the prediction, then the immutable history row at `after_state_version` is read and must match kind, before version, the
closed after-snapshot (same admission object) and `transition_at` (the explicit command instant in UTC). The current row is not read or compared: another
valid actor may have advanced it already; the exact-version history row is race-safe.

Renewal: prior lifecycle (C2B proves `renewed_at < old lease expiry`, new expiry greater) plus the explicit `renewed_at` plus the persisted history gives a
full end-to-end reconciliation, which the standalone D2B1 transport could not provide.

## RPC scalar shape

RPC current-state scalars are reconciled against the closed lifecycle version floors: READY = v1, CLAIMED >= v2, SUCCEEDED >= v3, FAILED >= v3. An
impossible state/version pair (for example READY/3, CLAIMED/1, SUCCEEDED/2, FAILED/1) is a malformed database response and raises RuntimeError for every
status that carries a state (initialization duplicate and conflict, apply version_conflict and transition_conflict, applied); not_found carries neither. The
conflict version relations (differs from / equals expected_version) and the stricter INITIALIZED = READY/v1 and APPLIED = prediction checks still apply.
The public result dataclasses also reject forged non-canonical hashes, wrong types and impossible pairs.

## Reads

`get_run(run_idempotency_sha256=...)` (canonical 64 lowercase hex; zero rows None, more than one RuntimeError) and `get_transition(admission=..., after_state_version=...)`.

## Not here

No worker, dispatch, scheduler loop, queue, retry policy, real-database concurrency tests, Phase 24 CI consolidation (see the later 24D3 and boundary phases).
