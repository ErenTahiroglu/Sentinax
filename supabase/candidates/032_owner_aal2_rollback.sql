-- Phase 25D1E0 — rollback of the Migration 032 candidate (OPERATOR ONLY, never run by the agent).
--
-- Removing a RESTRICTIVE policy BROADENS access back to the pre-032 permissive policies, so this is an emergency control, not a casual undo.
-- It refuses to run unless the operator confirms a verified pre-032 grant snapshot exists:
--     SET sentinax.rollback_confirm = 'I_HAVE_A_VERIFIED_PRE_032_GRANT_SNAPSHOT';
-- It drops only the policies and the helper. It does NOT re-grant the privileges 032 revoked (anon table rights, authenticated
-- TRUNCATE/REFERENCES/TRIGGER, anon EXECUTE on the owner RPCs): restoring any of them requires the operator's own snapshot and a conscious
-- decision, never a blind replay. The private_config schema and the operator-provided owner_identity row are kept (data is never destroyed).
DO $guard$
BEGIN
    IF COALESCE(current_setting('sentinax.rollback_confirm', true), '') <> 'I_HAVE_A_VERIFIED_PRE_032_GRANT_SNAPSHOT' THEN
        RAISE EXCEPTION 'rollback_confirm: refusing to remove the owner+AAL2 policies without a confirmed pre-032 grant snapshot';
    END IF;
END
$guard$;

DO $drop$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'portfolios', 'portfolio_accounts', 'cash_buckets', 'investment_goals', 'planned_contributions',
        'portfolio_transactions', 'portfolio_fee_tax_attribution_events', 'portfolio_import_claim_bindings'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS owner_aal2_required ON public.%I', t);
    END LOOP;
END
$drop$;

DROP FUNCTION IF EXISTS private_config.owner_aal2();
