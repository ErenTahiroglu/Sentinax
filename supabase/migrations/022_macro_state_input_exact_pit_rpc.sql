-- ============================================================================
-- Migration 022: Exact-transport PIT RPC for macro state inputs (Phase 17B)
-- ============================================================================
-- PostgREST serialises NUMERIC as a JSON number, which client libraries may decode into a binary float or int.
-- That is not acceptable for exact analytical inputs (Phase 17A `MacroStateInputFact.value` is an exact Decimal).
--
-- This migration adds ONE read-only wrapper that:
--   * delegates PIT winner selection to the existing migration-006 authority
--     `public.get_pit_macro_observation` (no second copy of the SYSTEM_AS_OF / SOURCE_AS_OF algorithm), and
--   * casts the selected observation value to TEXT in the database (`o.value::text`) so the client parses the
--     exact decimal spelling directly into a Decimal.
--
-- Defense in depth: the macro series must be the requested canonical key, active and VERIFIED.
-- No data is modified. SECURITY INVOKER: the caller's own privileges apply to the wrapper.
-- ============================================================================

CREATE OR REPLACE FUNCTION public.get_pit_macro_state_input(
    p_canonical_key VARCHAR(64),
    p_effective_date DATE,
    p_as_of TIMESTAMPTZ,
    p_as_of_mode VARCHAR(16)
)
RETURNS TABLE (
    canonical_key VARCHAR(64),
    observation_id UUID,
    snapshot_id UUID,
    effective_date DATE,
    value_text TEXT,
    data_status VARCHAR(32),
    confidence_level VARCHAR(16),
    source_tier VARCHAR(32),
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT
        s.canonical_key,
        o.id,
        o.snapshot_id,
        o.effective_date,
        o.value::text AS value_text,
        o.data_status,
        o.confidence_level,
        o.source_tier,
        o.published_at,
        o.observed_at,
        o.ingested_at,
        o.superseded_at
    FROM public.get_pit_macro_observation(p_canonical_key, p_effective_date, p_as_of, p_as_of_mode) AS o
    JOIN public.macro_series s ON s.id = o.macro_series_id
    WHERE s.canonical_key = p_canonical_key
      AND s.is_active IS TRUE
      AND s.contract_status = 'verified';
$$;

REVOKE EXECUTE ON FUNCTION public.get_pit_macro_state_input(VARCHAR(64), DATE, TIMESTAMPTZ, VARCHAR(16)) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_pit_macro_state_input(VARCHAR(64), DATE, TIMESTAMPTZ, VARCHAR(16)) FROM anon;
GRANT EXECUTE ON FUNCTION public.get_pit_macro_state_input(VARCHAR(64), DATE, TIMESTAMPTZ, VARCHAR(16)) TO authenticated, service_role;
