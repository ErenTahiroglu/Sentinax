# Private Scheduler Trigger Envelope (Phase 24A)

Phase 24 builds the private scheduler in separated checkpoints:

| Checkpoint | Scope | Status |
|---|---|---|
| 24A | Immutable trigger envelope (this document) | implemented |
| 24B | Timezone-aware schedule definitions and deterministic scheduled-occurrence planning (recurrence / time planning) | deferred |
| 24C | Event-driven dispatch and idempotent run authority / concurrency semantics (dispatch / run authority) | deferred |
| 24D | Runtime and persistence integration | deferred |

## Role: the occurrence envelope

Phase 24A does not run a scheduler. `backend/engine/private/scheduler_trigger.py` (pure, stdlib only, no other Sentinax import, in the static-guard
`PURE_MANIFEST`) defines the immutable domain boundary that represents what work is requested, why it was triggered, which system or portfolio-owner
scope it belongs to, which scheduler-policy revision authorized it and which logical occurrence / idempotency identity it carries. It never decides
whether a trigger is due, when it should fire, whether a market is open, whether the work should execute or whether it succeeded.

```python
build_private_scheduler_trigger(*, trigger_kind, work_kind, scope, owner_id, portfolio_id, scheduled_for, event_cause_kind, cause_key,
                                cause_available_at, policy_key, policy_revision, idempotency_sha256) -> PrivateSchedulerTrigger
```

Twelve stored fields, no defaults, frozen; direct construction re-validates the complete contract.

## Scheduled vs event-driven

```text
SCHEDULED     scheduled_for = exact aware datetime; event_cause_kind, cause_key, cause_available_at all None
EVENT_DRIVEN  scheduled_for = None; event_cause_kind (exact enum), cause_key, cause_available_at all present
```

No trigger may be both. `scheduled_for` is the caller-supplied occurrence instant, preserved exactly and not checked against any named market slot.
`cause_available_at` is when the event cause was available, a different notion from a scheduled occurrence instant.

## Event cause provenance

`PrivateSchedulerEventCauseKind` (disclosure ingested, macro release ingested, policy configuration changed, portfolio changed, new cash confirmed, user
view changed, risk limit breach) is caller-supplied causal provenance. It does not prove the underlying event occurred and implies no work kind, scope or
investment action. `cause_key` is an exact printable string of 1..128 characters, already trimmed, case preserved, never normalized, derived or interpreted.

## System vs portfolio scope and owner isolation

`SYSTEM` (global / source-level) requires no owner and no portfolio. `PORTFOLIO` requires an exact owner UUID and an exact portfolio UUID (subclasses rejected;
no owner inference from the portfolio id, no storage lookup, no anonymous or cross-owner scope).

## Work-kind vocabulary

`SOURCE_DATA_REFRESH` is SYSTEM work only. `PORTFOLIO_ANALYSIS_REFRESH`, `PORTFOLIO_HEALTH_CHECK` and `GAME_CHANGER_REVIEW` are PORTFOLIO work only, so a portfolio
analysis can never become an unscoped global task (`ValueError` on any other pairing; all 4 x 2 x 2 combinations are tested). Work kinds are declarative intent: they
invoke no engine.

## Policy key / revision and idempotency provenance

`policy_key` is a strict lowercase identifier (`^[a-z0-9][a-z0-9._-]{0,127}$`, no trimming or normalization, no value hardcoded) and `policy_revision` an exact `int >= 1`.
`idempotency_sha256` is an opaque caller-supplied 64-lowercase-hex logical occurrence identity: nothing is hashed or generated, no previous run is queried, uniqueness is
not claimed and it is not authentication. Later persistence / runtime layers may use it as an idempotency authority.

## No ambient clock, no schedule calculation

There is no `now` or `today`, and no validation of past or future instants against a clock: any valid aware datetime is accepted. There is no cron, next / previous run,
weekday, business-day, holiday, exchange-calendar or DST logic, no hardcoded clock slots (the methodology slots become versioned configuration in 24B) and no polling
interval. There is no dispatch, no execution state (pending, running, success, failure), no retry, backoff, lease or lock, no persistence, no notification and no trade
consequence: scheduling an analysis never authorizes an investment action.

## The legacy scheduler is non-authoritative

`backend/infrastructure/scheduler.py` is legacy / public infrastructure and stays frozen. Its 14-minute polling loop, legacy watchlists, chat orchestrator, radar score, MACD,
BEARISH logic, Telegram notification, derived portfolio snapshots and float portfolio-return arithmetic are not imported, referenced or reproduced.
