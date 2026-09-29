-- ============================================================================
-- Migration 023: PIT-safe exact macro state input history window (Phase 17C)
-- ============================================================================
-- Returns, for ONE canonical macro series, every eligible persisted observation whose economic effective_date lies in
-- the inclusive range [p_start_effective_date, p_end_effective_date], evaluated against ONE shared knowledge cutoff
-- (p_as_of) and ONE shared PIT mode (p_as_of_mode). Ordered by effective_date ASC, at most one row per date.
--
-- Authority chain:  023 -> 022 -> 006
--   * The PIT winner for each date is chosen ONLY by the migration-022 wrapper `get_pit_macro_state_input`
--     (which delegates to migration 006 and emits the exact value as TEXT). This migration copies no PIT predicate.
--   * Candidate effective dates are enumerated from macro_observations for the requested active + verified series.
--     Enumeration reads only the date; it makes no analytical selection. A date that exists only because of a row
--     ingested after p_as_of yields no output row, because the 022 call applies the real cutoff.
--   * No calendar gaps are synthesised. The mode token is forwarded to 022 unchanged.
--
-- No data is modified. SECURITY INVOKER: the caller's own privileges apply.
-- ============================================================================

CREATE OR REPLACE FUNCTION public.get_pit_macro_state_input_history(
    p_canonical_key VARCHAR(64),
    p_start_effective_date DATE,
    p_end_effective_date DATE,
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
        h.canonical_key,
        h.observation_id,
        h.snapshot_id,
        h.effective_date,
        h.value_text,
        h.data_status,
        h.confidence_level,
        h.source_tier,
        h.published_at,
        h.observed_at,
        h.ingested_at,
        h.superseded_at
    FROM (
        SELECT DISTINCT o.effective_date
        FROM public.macro_observations o
        JOIN public.macro_series s ON s.id = o.macro_series_id
        WHERE s.canonical_key = p_canonical_key
          AND s.is_active IS TRUE
          AND s.contract_status = 'verified'
          AND o.effective_date >= p_start_effective_date
          AND o.effective_date <= p_end_effective_date
    ) AS c
    CROSS JOIN LATERAL public.get_pit_macro_state_input(p_canonical_key, c.effective_date, p_as_of, p_as_of_mode) AS h
    ORDER BY c.effective_date ASC;
$$;

REVOKE EXECUTE ON FUNCTION public.get_pit_macro_state_input_history(VARCHAR(64), DATE, DATE, TIMESTAMPTZ, VARCHAR(16)) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_pit_macro_state_input_history(VARCHAR(64), DATE, DATE, TIMESTAMPTZ, VARCHAR(16)) FROM anon;
GRANT EXECUTE ON FUNCTION public.get_pit_macro_state_input_history(VARCHAR(64), DATE, DATE, TIMESTAMPTZ, VARCHAR(16)) TO authenticated, service_role;
