# Portfolio transaction history coverage — real PostgreSQL concurrency verification (Phase 26C2B2B3)

C2B2B3 verifies the locking premise of migration 026 against a real PostgreSQL server with independent connections.
No mocks, no fake PostgREST client, no SQLite. Test: `backend/tests/test_portfolio_transaction_history_coverage_postgres.py`.

## Environment

- CI: `Backend Correctness Gate` runs a `postgres:16` service container (disposable database `sentinax_concurrency`, explicit throwaway credentials) and the
  dedicated step `Phase 26 PostgreSQL history-coverage concurrency`. The exact `server_version` is printed by the test (`EVIDENCE postgres_server_version=...`) in the CI log.
- Test-only driver `psycopg[binary]==3.3.6` is installed inside that step; `backend/requirements.txt` is unchanged.
- Without `SENTINAX_TEST_POSTGRES_URL` the module skips. With it set, a missing driver or unreachable server FAILS.
- The fixture refuses to run unless the database name starts with `sentinax_` and contains `concurrency`, and a Supabase `auth.users` table is absent.
  No production or linked Supabase database is used. Supabase Preview proves only that the migrations apply; it is not a substitute for this concurrency evidence.

## Fidelity and fixture limits

Migrations `025` and `026` are read from the repository and executed **unmodified**; the coverage function is not copied into the test.
The fixture bootstraps only: roles `anon`/`authenticated`/`service_role`, a minimal `auth.uid()`, `public.portfolios (id, owner_id)`,
`public.portfolio_transactions (id, portfolio_id, owner_id, recorded_at, economic_fingerprint)` with the composite FK, Supabase-style default grants,
and the legacy authenticated INSERT policy that 025 drops. It does **not** prove migration 011 invariants (covered by their own tests). Each test rebuilds the
`public` and `auth` schemas, so no state leaks and order is irrelevant.

## Behavior proven (lock_timeout + SQLSTATE, no sleep-based proof)

Contention is established by holding a transaction open and bounding the other side with `lock_timeout`; the expected failure is SQLSTATE `55P03`
(`lock_not_available`), asserted by code, not message. `pg_locks` is also inspected to show the holder's mode.

| Scenario | Result asserted |
|---|---|
| Writer first | Open writer holds `RowExclusiveLock`; the coverage RPC fails `55P03`. After the writer commits, the RPC succeeds and the manifest contains the row. |
| Snapshot first | Open snapshot transaction holds `ShareLock`; a new `service_role` INSERT fails `55P03`. After the snapshot commits the INSERT succeeds. |
| Rollback visibility | A blocked-then-rolled-back writer's row is absent from the next manifest. |
| Non-finality | Snapshot S1 through cutoff C commits; a trusted writer then inserts a row with `recorded_at <= C`; S2 through the same C contains it, S1's manifest is unchanged. |
| Manifest | `transaction_count`, ids, `recorded_at_epoch_micros` and fingerprints are aligned, ordered `recorded_at ASC, id ASC` (including a same-microsecond tie), with exact integer microseconds. |
| Empty history | Exactly one row, count 0, empty (non-NULL) arrays. |
| Future cutoff | Fails closed (`RAISE EXCEPTION` from the function). |
| Isolation | Other owners' and other portfolios' rows never appear; a mismatched owner/portfolio pair fails closed. |
| Privileges | `service_role` has SELECT, INSERT (no UPDATE/DELETE/TRUNCATE); `authenticated` direct INSERT is denied (`42501`); `anon` has none. |

## Limits

Verifying both lock directions permits C2E to proceed toward closure. It does **not** make coverage permanent finality: a trusted writer may still insert a
backdated row after the proof ends (the non-finality scenario shows this), and the proof remains valid only as observed at `observed_at`.
