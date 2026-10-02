# Portfolio Transaction History Coverage Snapshot (Phase 26C2B2B1)

Migration: `supabase/migrations/026_portfolio_transaction_history_coverage_snapshot.sql` (database contract only; no Python in this checkpoint).

## What it answers

`public.get_portfolio_transaction_history_coverage_snapshot(p_owner_id, p_portfolio_id, p_as_of_recorded_at)` returns exactly one row saying which complete set of
persisted `portfolio_transactions` has `recorded_at <= cutoff` for that explicit owner and portfolio, observed atomically.

## Why not pagination

The repository list read pages with repeated range reads (several statements), so different pages can see different database states; adding a `recorded_at`
filter to that loop would not make it atomic coverage. This RPC uses no offset, limit, cursor or loop.

## Why a SHARE lock

`LOCK TABLE public.portfolio_transactions IN SHARE MODE` conflicts with the ROW EXCLUSIVE lock of every writer, so writers in flight finish first and no new row
can appear while the manifest is selected; the single aggregate then sees one stable committed state. ACCESS SHARE, `FOR SHARE`, advisory and application locks are
not used because existing writers do not participate in them. The lock is taken before `observed_at` is captured and before any read of the table.

## Why SECURITY DEFINER, service-role-only EXECUTE and a pinned search_path

Migration 025 keeps service_role at SELECT and INSERT only; the function is SECURITY DEFINER solely so its owner can take the lock (an authorized exception to
the usual SECURITY INVOKER preference), with `SET search_path = pg_catalog, pg_temp`, fully schema-qualified relations, no dynamic SQL and no caller-named
relation. EXECUTE is revoked from PUBLIC, anon and authenticated and granted to service_role only: the RPC takes a table-level lock and is backend authority.

## Semantics

- Owner and portfolio are explicit inputs (no `auth.uid()` inference); after the lock the portfolio must exist for that owner. NULL inputs fail closed.
- Cutoff: `t.recorded_at <= p_as_of_recorded_at` and nothing else (no effective-date, executed-at, type, account or reversal filter): every ledger event known by
  the cutoff, a reversal being just another row. A future cutoff (later than the database `observed_at`) raises, because the database cannot prove completeness
  through a future knowledge time.
- `observed_at` is `clock_timestamp()` after the lock: proof observation time, never the transaction recorded_at, the cutoff or an economic date; the caller supplies none.
- Manifest: three parallel arrays in one canonical order (`recorded_at ASC, id ASC`): `transaction_ids` (physical id), `recorded_at_epoch_micros` (exact epoch
  microseconds as BIGINT, no float and no PostgREST timestamp formatting) and `economic_fingerprints` (the persisted economic fingerprint excludes id and recorded_at, so all three
  dimensions are needed; notes are excluded). Count and arrays come from one aggregate over one filtered relation. Eligible fingerprints must be 64 lowercase hex
  (syntax only; the Python fingerprint is not reproduced and malformed data is not repaired).
- Empty history returns one row with count 0 and typed empty arrays (never NULL).
- No data writes, no digest and no pgcrypto: the ordered tuple is the evidence authority. A dedicated index on `(owner_id, portfolio_id, recorded_at, id)` supports it.

## Limits

This is not a permanent finality claim. It proves the committed rows visible after the SHARE lock through the cutoff and relies on the trusted service-role
write boundary of migration 025; it does not cryptographically prevent a trusted or misconfigured service-role writer from inserting a backdated row after the
proof finished. The Python RPC transport and reconciliation against the supplied history are deferred to C2B2B2; real concurrent-writer behavioral verification
is deferred to a later checkpoint. Supabase Preview proves the migration applies, not concurrency behavior; remote Backend CI does not run the schema test yet.
