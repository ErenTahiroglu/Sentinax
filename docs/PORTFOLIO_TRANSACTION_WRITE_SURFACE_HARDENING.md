# Portfolio Transaction Write-Surface Hardening (Phase 26C2B2A)

Migration: `supabase/migrations/025_portfolio_transaction_write_surface_hardening.sql`.

## Why

`public.portfolio_transactions.recorded_at` is a system-knowledge authority: historical replay (Phase 26) treats a transaction as known only from its recorded_at.
Migration 011 made the table append-only with a BEFORE UPDATE OR DELETE trigger, but it also defined an authenticated direct INSERT policy
("Users can insert own portfolio transactions"). Any caller able to INSERT into the table directly could backdate recorded_at outside the canonical backend
repository (canonical serialization, system-recorded time, owner-scoped validation). That bypass is incompatible with replay trust.

## What changed

- The legacy authenticated INSERT policy is dropped. The owner-scoped select policy ("Users can view own portfolio transactions") is untouched.
- Table privileges are reset explicitly (not relying on RLS alone): all privileges are revoked from PUBLIC, anon, authenticated and service_role, then granted
  back as the final matrix: PUBLIC none, anon none, authenticated SELECT only, service_role SELECT and INSERT only.
- No role has UPDATE, DELETE or TRUNCATE. TRUNCATE matters because the row-level immutability trigger does not protect against it. The existing trigger remains
  as defense in depth. The "Service role full access" RLS policy name does not define table privileges; the grants do.

## Preserved

The canonical backend write path (`PortfolioRepository.append_transaction`, `.table("portfolio_transactions").insert(...)`) keeps working through the service_role
INSERT grant; the backend client uses the `SUPABASE_SERVICE_ROLE_KEY` environment variable (name only here, never its value). The import commit RPCs (migrations
015 to 017) are unchanged. No frontend or browser code writes this table (searched). The recorded_at contract is unchanged (no database clock trigger).

## Limit

This does not prove historical coverage or completeness. service_role is still trusted to submit canonical inserts; migration 025 only removes the
authenticated/raw-table bypass that would undermine ingestion-time authority. A persistent-history coverage proof is the next owner (C2B2B). Local tests are
SQL-text structural checks; Supabase Preview is the real migration-execution check, and remote Backend CI does not run this schema test yet.

## Follow-up (Phase 27 FIX A)

Because service_role has no UPDATE, the SECURITY INVOKER Phase 14G.2 trigger locks (`FOR UPDATE`) failed with `42501` after this migration. Migration 027 makes those two trigger
functions SECURITY DEFINER with a pinned search_path and grants nothing; see `docs/PORTFOLIO_TRIGGER_PRIVILEGE_HARDENING.md`.
