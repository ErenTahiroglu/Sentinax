# Private Backtest User-View History (Phase 26C2D1)

Module: `backend/engine/private/backtest_user_view_history.py` (PURE, registered in the PURE manifest).

## Why

Phase 20 `UserReturnView` objects are source-neutral mathematical objects. They carry exactly `kind`, `loadings`, `target_return` and `confidence`: no timestamp, no stable
lifecycle/revision identity, no owner identity, and no persistence. A current view must never be replayed into an earlier historical analysis merely because its
economic contents are valid. C2D1 adds the immutable historical revision/provenance primitive a later point-in-time resolver needs. No Phase 20 contract is changed.

## Revision envelope (`PrivateBacktestUserViewRevision`)

Exactly six stored fields, no defaults, frozen:

| Field | Contract |
|---|---|
| `view_id` | Exact `UUID`: a stable, caller-supplied lifecycle identity for one logical view across revisions. Never generated, never derived from contents, no hash. The origin of the UUID is not proven here. |
| `revision` | Exact `int` (bool and subclasses rejected), `>= 1`. Validated per envelope only. |
| `action` | `UPSERT` or `WITHDRAW`: lifecycle provenance only, not a recommendation, scheduler or posterior state. |
| `instrument_ids` | The instrument basis of the positional loadings (see below). |
| `view` | The exact `UserReturnView` for UPSERT (stored by identity, never copied, reconstructed or normalized); `None` for WITHDRAW. |
| `available_at` | Exact aware `datetime` with a UTC offset strictly inside +-24h and a valid UTC conversion. Preserved exactly as supplied, never normalized. |

`available_at` is knowledge time: the earliest instant Sentinax may treat this exact revision as known. It is not an economic or effective time, and it is not compared
to any replay cutoff here. No ambient clock is read.

## Instrument basis

`UserReturnView.loadings` are positional. An UPSERT therefore carries an exact tuple of exact UUIDs, non-empty, unique, in strictly ascending UUID-string order (the
canonical `AllocationReturnPanel` order), with length equal to the number of loadings. The basis is never inferred and the constructor never reorders caller input;
malformed or noncanonical input fails closed. This prevents an old loading vector from being applied to a different instrument ordering of the same dimension.

## WITHDRAW

A WITHDRAW carries `view=None` and `instrument_ids=()`: it identifies only the logical `view_id`, the revision and its availability time, with no stale economic payload.
An UPSERT with no view or an empty basis, and a WITHDRAW with a view or a basis, are rejected.

## Not in this checkpoint

- No point-in-time selection: no cutoff filtering, latest-revision or active-view logic, withdrawal resolution, revision-family grouping, sorting or deduplication.
  C2D2 will resolve revision history against the replay knowledge cutoff, including cross-revision uniqueness, monotonicity and gaps.
- No `UserReturnViewSet` and no Bayesian posterior construction.
- No completeness or readiness claim: zero user views may be valid because the overlay is not automatically mandatory. C2E owns requirement and readiness policy.
- No persistence claim, no repository or database access, no generated identity and no hash.

## Deferred gates

- C2B2B3 real PostgreSQL concurrent-writer verification remains mandatory before C2E and before Phase 26 closure.
- C2C2 typed market-data reconstruction remains deferred unless a real consumer requires it.
