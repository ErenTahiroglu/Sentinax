-- ============================================================================
-- Migration 025: portfolio_transactions write-surface hardening (Phase 26C2B2A)
-- ============================================================================
-- public.portfolio_transactions is the append-only, system-recorded ledger. Its
-- recorded_at column is the system-knowledge authority for historical replay, so
-- no untrusted role may write it directly. Migration 011 already blocks UPDATE and
-- DELETE with a row trigger (which does not cover TRUNCATE) but still allowed an
-- authenticated direct INSERT. This migration is additive privilege hardening only.
--
-- Final table privileges:
--   PUBLIC         none
--   anon           none
--   authenticated  SELECT   (owner-scoped by the existing RLS select policy)
--   service_role   SELECT, INSERT   (the canonical backend repository append path)
-- Nobody is granted UPDATE, DELETE or TRUNCATE.
-- ============================================================================

DROP POLICY IF EXISTS "Users can insert own portfolio transactions" ON public.portfolio_transactions;

REVOKE ALL PRIVILEGES ON TABLE public.portfolio_transactions FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.portfolio_transactions FROM anon;
REVOKE ALL PRIVILEGES ON TABLE public.portfolio_transactions FROM authenticated;
REVOKE ALL PRIVILEGES ON TABLE public.portfolio_transactions FROM service_role;

GRANT SELECT ON TABLE public.portfolio_transactions TO authenticated;
GRANT SELECT, INSERT ON TABLE public.portfolio_transactions TO service_role;
