# Portfolio Ledger Trigger Privilege Hardening (Phase 27 FIX A)

## Defect

Migration 025 deliberately leaves `service_role` with SELECT and INSERT only on `public.portfolio_transactions` (no UPDATE, DELETE or TRUNCATE). The Phase 14G.2 trigger
functions `lock_portfolio_transaction_reversal_target()` and `validate_fee_tax_attribution_event_integrity()` were SECURITY INVOKER and take `SELECT ... FOR UPDATE` row
locks on that table. PostgreSQL authorizes `FOR UPDATE` as UPDATE, so after 025 a service-role ledger REVERSAL insert and every fee/tax attribution insert (allocation and
attribution reversal) failed with SQLSTATE `42501`. The failure was closed (nothing was corrupted), but the only correction path of the append-only ledger and all attribution
persistence were unusable. Real PostgreSQL 16 reproduction: `backend/tests/test_portfolio_trigger_privilege_postgres.py`, case `test_red_migration_025_state_breaks_...`.

## Repair (migration 027, additive)

- The two functions are re-declared with their closed migration-021 bodies unchanged (a schema test asserts byte equality) and are now `SECURITY DEFINER` with
  `SET search_path = pg_catalog, pg_temp`. Application relations stay schema-qualified (`public.<table>`); there is no dynamic SQL and no caller-selected name.
- `service_role` receives no UPDATE (no table, column or temporary grant). Its table privileges remain SELECT + INSERT; authenticated remains SELECT only; PUBLIC and anon none.
- SECURITY DEFINER does not give callers any UPDATE: the functions only lock and read rows and `RETURN NEW`. They are argument-less `RETURNS TRIGGER` functions, so they are
  not an RPC and cannot mutate arbitrary rows. EXECUTE is revoked from PUBLIC, anon, authenticated and service_role (trigger firing does not require caller EXECUTE).
- Trust assumption: `CREATE OR REPLACE` keeps the existing owner, the trusted migration/database owner that ran migrations 020/021. No application role owns the functions.
- Trigger bindings (`trg_lock_portfolio_transaction_reversal_target`, `trg_validate_fee_tax_attribution_event_integrity`), data and schema are unchanged.

Reversal and attribution inserts remain append-only commands; direct ledger mutation (UPDATE, DELETE, TRUNCATE, `SELECT ... FOR UPDATE` by an API role) remains denied.

## Verification

Real PostgreSQL (CI service `postgres:16`) executes the real migrations 011, 018 to 021, 025 and 027 with a minimal fixture (roles, auth stand-ins, a `public.instruments`
stand-in for migration 005, Supabase-style default grants). It proves the RED state, the repaired path, preserved Phase 14 rules (capacity, cross-stream non-backdating),
direct-mutation denial, authenticated/anon denial, function properties, and two cross-connection lock races (an open attribution insert and an open ledger reversal each make the
other wait under `lock_timeout`, SQLSTATE `55P03`). Schema-text tests in `test_portfolio_trigger_privilege_hardening_schema.py` are supplemental.

## Limit

Not covered here: the C2C1/C2C2 temporal frontier finding (separate checkpoint) and CI permanence of the other Phase 8 to 14 tests.
