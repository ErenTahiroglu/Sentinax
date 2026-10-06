-- 029_live_function_search_path_drift_hardening.sql
-- Sentinax: Phase 27 FIX D-R1, live function search_path drift closure
--
-- Cause: migration 028 pinned the search_path of public.resolve_instrument_to_provider_symbol and public.get_pit_observation through FIXED repository signatures guarded by
-- to_regprocedure(). The live database has historical function-signature drift, so those signatures did not identify the live objects, the guard skipped them (by design: Supabase Preview had
-- rejected unconditional ALTERs) and the live Security Advisor still reported both as "Function Search Path Mutable".
--
-- Repair: identify each function by CATALOG identity (pg_proc / pg_namespace, schema public, the exact name), require exactly ONE match (zero or several is ambiguous and fails closed: no
-- silent skip, no first-match guess, no overload sweep) and change ONLY proconfig through ALTER FUNCTION on the catalog-derived regprocedure. Bodies, arguments, return types, volatility,
-- security mode and ACLs are untouched. The path includes public because the historical live bodies may reference unqualified public relations; pg_catalog is listed first explicitly.
DO $$
DECLARE
    v_name TEXT;
    v_count INTEGER;
    v_oid pg_catalog.oid;
BEGIN
    FOREACH v_name IN ARRAY ARRAY['resolve_instrument_to_provider_symbol', 'get_pit_observation'] LOOP
        SELECT pg_catalog.count(*)
        INTO v_count
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = v_name;

        IF v_count <> 1 THEN
            RAISE EXCEPTION 'search_path hardening needs exactly one public.% function, found %', v_name, v_count;
        END IF;

        SELECT p.oid
        INTO STRICT v_oid
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = v_name;

        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s SET search_path = pg_catalog, public, pg_temp',
            v_oid::pg_catalog.regprocedure::pg_catalog.text
        );
    END LOOP;
END
$$;
