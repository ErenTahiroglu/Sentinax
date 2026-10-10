-- 031_legacy_pit_store_truncate_hardening.sql
-- Post-28B-1A hardening H1: remove the unnecessary destructive TRUNCATE privilege from the legacy PIT storage tables.
--
-- Finding (disposable PostgreSQL 16, fresh replay with Supabase-style default grants): anon, authenticated and service_role held TRUNCATE on
-- public.raw_provider_snapshots and public.normalized_observations. TRUNCATE bypasses row-level security and does not fire the row-level DELETE anti-tamper triggers.
-- Before migration 030, `TRUNCATE raw_provider_snapshots CASCADE` succeeded for all three roles; since 030 it is blocked only INDIRECTLY (the dependent
-- learning_evidence_bindings table grants nobody TRUNCATE). `TRUNCATE normalized_observations` succeeded for all three roles both before and after 030.
-- Reachability is direct SQL only: the Supabase Data API (PostgREST) exposes no TRUNCATE verb.
--
-- Scope: table-scoped REVOKE of TRUNCATE only. SELECT / INSERT / UPDATE (including system supersession) and every other privilege, ownership, trigger, policy, constraint,
-- RLS setting and global default privilege are unchanged. Forward-only and idempotent (revoking an absent privilege is a no-op). The owner/superuser path is out of scope.

REVOKE TRUNCATE ON TABLE public.raw_provider_snapshots FROM PUBLIC, anon, authenticated, service_role;
REVOKE TRUNCATE ON TABLE public.normalized_observations FROM PUBLIC, anon, authenticated, service_role;
