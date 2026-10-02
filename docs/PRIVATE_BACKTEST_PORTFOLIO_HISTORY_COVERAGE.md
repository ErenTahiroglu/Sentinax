# Private Backtest Portfolio History Coverage (Phase 26C2B2B2)

Module: `backend/engine/private/backtest_portfolio_history_coverage.py` (deliberately not in the PURE manifest; infrastructure adapter).

## Layers

- **C2B1**: the canonical projection of the SUPPLIED transaction history at the replay recorded-time cutoff.
- **C2B2B1**: the atomic database manifest (`get_portfolio_transaction_history_coverage_snapshot`, SHARE-locked).
- **C2B2B2** (this module): exact reconciliation between them.

## Flow

`PrivateBacktestPortfolioHistoryCoverageRepository(client=..., owner_id=UUID).verify_projection_history(projection_binding=...)` makes exactly one RPC call with
`p_owner_id`, `p_portfolio_id` and `p_as_of_recorded_at` (the original cutoff, microsecond ISO form). There is no table read, no pagination, no clock, no new hashing
and no retry; an execute error propagates. The owner is the explicit exact-UUID dependency (never inferred); the RPC independently validates owner and portfolio.

## Reconciliation

The comparison authority is `projection.known_transactions` (the closed `recorded_at <= cutoff` view, in its existing canonical order: recorded_at UTC then id,
matching the migration), NOT the full supplied transaction tuple, which may contain rows recorded after the cutoff. Three parallel dimensions must match exactly:
physical id, recorded_at as exact signed epoch microseconds (integer arithmetic, no float, pre-1970 allowed) and the economic fingerprint recomputed now by
`PortfolioTransaction.economic_fingerprint()`. Each alone is insufficient: id proves only identity, the fingerprint excludes id and recorded_at, recorded_at carries no
content. Caller tuple order does not matter; a different manifest order does.

## Errors

- malformed or self-contradictory transport (wrong row shape, non-canonical UUID text, bad timestamp, count not equal to array lengths, duplicate ids, wrong owner or
  portfolio, a cutoff echo that is not the same UTC instant, observed_at before the cutoff, malformed fingerprint, bool or float micros): plain `RuntimeError`.
- a valid manifest that differs from the supplied projection (a history mismatch: count, id, recorded_at or fingerprint): `PrivateBacktestPortfolioHistoryCoverageMismatchError` (no row
  data in the message). A concurrent trusted writer's backdated row appears exactly this way; it is never hidden, retried or rebuilt. The caller owns retry policy.

## Verified coverage

Stores only the exact projection binding, the owner and the database `observed_at` (kept as parsed; UTC only for comparison; the database is the clock authority).
It can be built only with a module-private capability: a Python application trust boundary, not cryptographic provenance (anyone with access to the module internals
could forge it). It is valid as observed at `observed_at`, not permanent finality, and relies on the trusted service-role write boundary (migration 025). Real
concurrent-writer behavioral verification is deferred to C2B2B3; remote CI does not run these tests yet.
