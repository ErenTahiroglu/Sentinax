# Private Scheduler Runtime (Phase 24D3A)

Module: `backend/engine/private/scheduler_runtime.py` (orchestration, not a PURE module). It executes exactly ONE persisted READY run through an injected
repository, an injected typed dispatcher and an injected clock.

## Flow

1. Input checks (before any clock, repository or dispatcher call): exact `PrivateSchedulerPersistedRun`, state READY, canonical claim key, UTC lease expiry.
2. `clock()` once, then `repository.claim_run` once. The database CAS (migration 024 via the repository) chooses the winner. Claim before dispatch: only an
   APPLIED claim continues; not found / version conflict / transition conflict map to `CLAIM_*` statuses and return immediately (no dispatch, no terminal
   call, one clock call, no retry).
3. The dispatcher is called once with `PrivateSchedulerDispatchRequest(admission, claim_key)` using the persisted claimed admission. The runtime does no
   work-kind branching; the dispatcher owns the mapping.
4. `clock()` once more, then one terminal call: `succeed_run` after SUCCEEDED, `fail_run` otherwise.

## Dispatch outcomes

- `PrivateSchedulerDispatchResult(status, failure_code)`: SUCCEEDED has no code; FAILED carries a canonical code (`^[a-z0-9][a-z0-9._-]{0,127}$`).
- A dispatcher `Exception` is mapped to `dispatcher_exception`; a non-exact return value (None, object, dict, subclass) to `dispatcher_contract_error`. No
  exception class, message, repr or stack trace is persisted. Base exceptions (KeyboardInterrupt, SystemExit, GeneratorExit) are not caught.

## Terminal results

APPLIED terminal gives runtime SUCCEEDED or FAILED. A non-applied terminal (not found / version conflict / transition conflict) is reported as
`TERMINAL_*` with the dispatch result preserved: the work may have run, so it is never converted into a work success or failure. The repository already
reconciles immutable history; the runtime performs no extra reads.

## Authorities

Claim key, lease expiry and the clock are explicit inputs; no identity generation, no lease-duration policy, no ambient clock. Clock output must be an exact
aware `datetime` and is canonicalized to UTC. Exactly two clock calls when a claim is applied, one otherwise.

## Limits

- At most one dispatch per invocation after winning the persisted claim. Exactly-once external side effects are NOT guaranteed: a run whose dispatch ran but
  whose terminal persistence conflicts is never dispatched again automatically; side-effect idempotency belongs to the concrete handlers (24D3B).
- No renewal, takeover, heartbeat or recovery scan. Long work must fit inside one lease; if the terminal clock is at or after the lease expiry the closed
  domain rejects locally and the error propagates (not converted to FAILED). Repository exceptions propagate with no hidden recovery.
- GAME_CHANGER_REVIEW is a review intent only, never an automatic trade, sell, rebalance or quarantine.
- No queue, loop, async, legacy scheduler, source/provider selection or concrete engine binding (24D3B).
