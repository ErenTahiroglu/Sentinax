# Private Scheduler Work Dispatcher (Phase 24D3B)

Module: `backend/engine/private/scheduler_work_dispatcher.py` (orchestration, not PURE). It implements the 24D3A dispatcher port by exact route selection.

## Why routing closes now, business execution does not

The scheduler trigger is declarative intent only. It lacks the business inputs the existing engines need: provider/series/source policy for source refresh;
analysis configuration, market snapshot and objective for analysis refresh; portfolio projection, bands and friction inputs for health checks; revision-family
resolution and PIT evidence for Game Changer review. D3B therefore invents none of them and imports no provider, allocation or Game Changer module. The four
handlers are injected application workflows; concrete handlers are deliberately not created here.

## Routes (exact enum identity, no aliases, no caller-supplied mapping)

| WorkKind | Scope | Handler |
|---|---|---|
| SOURCE_DATA_REFRESH | SYSTEM (no owner, no portfolio) | `source_data_refresh_handler` |
| PORTFOLIO_ANALYSIS_REFRESH | PORTFOLIO | `portfolio_analysis_refresh_handler` |
| PORTFOLIO_HEALTH_CHECK | PORTFOLIO | `portfolio_health_check_handler` |
| GAME_CHANGER_REVIEW | PORTFOLIO | `game_changer_review_handler` |

All four handlers are required. A defensive scope check runs before any handler (ValueError, no repair). Owner and portfolio are never looked up.

## Context and idempotency

`PrivateSchedulerWorkContext(admission, claim_key, work_idempotency_key)`: the admission is the same object as in the request; `work_idempotency_key` equals
`admission.run_idempotency_sha256` (an alias, not a second identity). It is the stable logical occurrence key for handlers with external effects and is
distinct from the claim key: the same run keeps its work key across a lease takeover while the claim key changes. D3A guarantees at most one dispatch per
invocation, not exactly-once effects; no idempotency store exists here.

## Results and errors

The handler is called once; its exact `PrivateSchedulerDispatchResult` is returned by identity (success or controlled failure, code unchanged, no fallback to
another handler). A wrong result type raises TypeError, which the D3A runtime catches as an ordinary Exception and persists as `dispatcher_exception`
(`dispatcher_contract_error` stays reserved for a dispatcher returning a wrong top-level object). Handler exceptions propagate uncaught (the runtime owns
sanitization); base exceptions are never caught.

## Responsibilities

D3A owns the lifecycle and persistence. D3B owns route selection. Handlers own business workflows. GAME_CHANGER_REVIEW remains a review intent: nothing here
sells, trades, rebalances or quarantines, and no Game Changer gate is invoked without a revision-family resolution that scheduler admission does not carry.
No database, clock, randomness, async, queue or loop.
