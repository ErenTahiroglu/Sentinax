# Private Backtest User-View Resolution (Phase 26C2D2)

Module: `backend/engine/private/backtest_user_view_resolution.py` (PURE, registered in the PURE manifest).

## Source primitive

The C2D1 `PrivateBacktestUserViewRevision` envelope (stable `view_id`, explicit `revision`, UPSERT/WITHDRAW action, positional instrument basis, exact `UserReturnView`,
`available_at` knowledge time) is the only input. C2D2 resolves a tuple of those envelopes at exactly one analysis knowledge frontier.

## Knowledge cutoff is the only eligibility frontier

The knowledge cutoff of the analysis context is the single point-in-time authority.

A revision is eligible iff its `available_at`, converted to UTC, is not after `analysis_context.replay_point.knowledge_cutoff_utc`. Exact equality is eligible; one
microsecond later is future. User views are internal/system-known state, so SOURCE_AS_OF and SYSTEM_AS_OF apply the identical rule. The evaluation date, horizon, view
economics, revision number, action and instrument basis never decide temporal eligibility.

## Future isolation (filter BEFORE validate)

Order is mandatory: exact-type validation of the supplied envelopes, derive the cutoff, filter out future revisions, discard them completely, and only then perform
duplicate, gap, start and availability-order validation on the eligible subset. Future revisions therefore have zero historical influence: a future duplicate of an
eligible revision number, a future huge revision number, a future WITHDRAW, a future UPSERT or one with another instrument basis cannot cause a failure, a gap, a
withdrawal, a new active view or a different order. Future revisions are not retained in the result.

## Canonical order and duplicates

Caller input order has no authority. Eligible revisions are ordered by `str(view_id)` ascending, then `revision` ascending, preserving the exact envelope objects by
identity (nothing is copied or rebuilt). Within the eligible subset `(view_id, revision)` must be unique; two eligible envelopes with the same identity fail closed, even
with equal contents and even if the very same object is repeated.

## Coverage assertion

`COMPLETE_AT_CUTOFF` is an explicit caller assertion that the supplied input holds the complete user-view revision history that was knowable at the cutoff. It is
never inferred. It is source-local user-view history coverage only, not overall decision readiness.

Under COMPLETE_AT_CUTOFF every eligible family must start at revision 1 with an UPSERT, have contiguous revision numbers, and have non-decreasing `available_at` UTC
instants by revision (equal instants are valid). Revision number, never a timestamp, confidence or economic value, is the lifecycle authority; the terminal revision is
the highest contiguous revision.

Under `INCOMPLETE_AT_CUTOFF` the eligible revisions are preserved canonically for audit, duplicate identities remain invalid, no contiguity or start rule applies, and
`terminal_revisions` and `active_revisions` are `()`. Status is `INCOMPLETE_COVERAGE`; no best-effort latest or partial active view is ever exposed.

## Terminal versus active

`terminal_revisions` holds exactly one terminal revision per `view_id`, in ascending `str(view_id)` order, including a terminal WITHDRAW. `active_revisions` holds only
terminal UPSERTs. A terminal WITHDRAW leaves the view inactive and never revives an earlier UPSERT; a later UPSERT may reactivate the same `view_id`. No economic contents
are merged across revisions.

## Zero views

A COMPLETE_AT_CUTOFF history may resolve to empty eligible, terminal and active tuples with status `RESOLVED`: under the caller's complete-history assertion, no user views
were known. This is not a failure; whether any decision configuration requires user views is decided later.

## Result self-validation

`PrivateBacktestUserViewResolution` has exactly six stored fields. Direct construction runs the same single derivation as the resolver and rejects a forged status,
forged terminal or active tuples, a future or unordered or duplicated eligible history, and an equal-valued clone instead of the selected object. It validates the supplied
eligible-history graph and the resolution consistency; it cannot independently prove that a caller's COMPLETE_AT_CUTOFF assertion is objectively exhaustive.

## Positional instrument basis

Each active revision keeps its D1 `instrument_ids` and `view` by object identity. Nothing is bound to an expected-return prior or return panel here; a later consumer must
prove compatibility with the prior's instrument order before Phase 20 view-set construction.

## Not in this checkpoint

No `UserReturnViewSet`, no posterior, no prior binding, no persistence, no generated identity or hash, no clock, randomness or I/O, and no decision readiness. The C2E
requirement/completeness bundle owns whether user views are required and the overall completeness policy.

## Deferred gates

- C2B2B3 real PostgreSQL concurrent-writer verification is closed (verified by a real PostgreSQL integration test in CI).
- C2C2 typed market-data reconstruction remains deferred unless a real consumer requires it.
