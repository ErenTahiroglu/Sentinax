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
-- Section A below is a deployment-compatibility prelude (legacy macro schema reconciliation); section B is the RPC.
-- ============================================================================

-- ============================================================================
-- A. Deployment compatibility: legacy macro schema reconciliation (Phase 17 DB chain repair R2)
-- ============================================================================
-- Migrations 006/007 were applied to the linked database in an OLDER revision (no contract_status, silent tier_1 / TR /
-- complete / high / DATE defaults, no CHECK constraints, no auto-supersession trigger, no vintage index). Because those
-- versions are recorded as applied, the hardened 006/007 definitions are never replayed. The RPC below requires
-- macro_series.contract_status, so the physical schema is reconciled to the CURRENT 006/007 contract first.
--
-- Fail-closed rules (never guess, never backfill):
--   * either macro table missing / not a table / not exactly the known column+type family -> RAISE EXCEPTION
--   * schema already carries contract_status                                                -> must be fully hardened
--                                                                                              (rows allowed, no-op) or RAISE
--   * known legacy variant (no contract_status)                                             -> BOTH tables must be empty,
--                                                                                              otherwise RAISE; then repair
-- With zero rows no value is invented: contract_status takes the migration-006 column contract
-- (NOT NULL DEFAULT 'verified') for FUTURE rows only. No row is rewritten and no existing row is ever marked verified.
DO $macro_schema_reconciliation$
DECLARE
    v_series oid := to_regclass('public.macro_series');
    v_obs oid := to_regclass('public.macro_observations');
    v_series_legacy CONSTANT text[] := ARRAY[
        'id uuid',
        'canonical_key character varying(64)',
        'provider character varying(32)',
        'provider_series_code character varying(128)',
        'category character varying(32)',
        'description text',
        'unit character varying(32)',
        'frequency character varying(16)',
        'freshness_basis character varying(32)',
        'source_tier character varying(32)',
        'is_active boolean',
        'created_at timestamp with time zone',
        'geography character varying(8)',
        'provider_native_units text',
        'seasonal_adjustment text',
        'origin_source text',
        'release_name text'
    ];
    v_series_current CONSTANT text[] := ARRAY[
        'id uuid',
        'canonical_key character varying(64)',
        'provider character varying(32)',
        'provider_series_code character varying(128)',
        'category character varying(32)',
        'description text',
        'unit character varying(32)',
        'frequency character varying(16)',
        'freshness_basis character varying(32)',
        'source_tier character varying(32)',
        'is_active boolean',
        'created_at timestamp with time zone',
        'geography character varying(8)',
        'provider_native_units text',
        'seasonal_adjustment text',
        'origin_source text',
        'release_name text',
        'contract_status character varying(32)'
    ];
    v_obs_expected CONSTANT text[] := ARRAY[
        'id uuid',
        'macro_series_id uuid',
        'snapshot_id uuid',
        'effective_date date',
        'value numeric(18,6)',
        'unit character varying(32)',
        'frequency character varying(16)',
        'data_status character varying(32)',
        'confidence_level character varying(16)',
        'source_tier character varying(32)',
        'published_at timestamp with time zone',
        'observed_at timestamp with time zone',
        'ingested_at timestamp with time zone',
        'supersedes_record_id uuid',
        'is_superseded boolean',
        'superseded_at timestamp with time zone',
        'warnings jsonb',
        'source_ref text',
        'created_at timestamp with time zone',
        'source_available_date date',
        'availability_precision character varying(16)',
        'realtime_end date',
        'vintage_date date',
        'origin_source text',
        'release_name text'
    ];
    v_series_actual text[];
    v_obs_actual text[];
    v_hardening_constraints integer;
    v_series_rows bigint;
    v_obs_rows bigint;
BEGIN
    IF v_series IS NULL OR v_obs IS NULL THEN
        RAISE EXCEPTION 'migration 022 macro schema reconciliation requires existing public.macro_series and public.macro_observations tables (unsupported shape)';
    END IF;
    IF (SELECT relkind FROM pg_class WHERE oid = v_series) NOT IN ('r', 'p')
       OR (SELECT relkind FROM pg_class WHERE oid = v_obs) NOT IN ('r', 'p') THEN
        RAISE EXCEPTION 'migration 022 macro schema reconciliation found a macro relation that is not a table (unsupported shape)';
    END IF;

    SELECT array_agg(attname::text || ' ' || format_type(atttypid, atttypmod)) INTO v_series_actual
    FROM pg_attribute WHERE attrelid = v_series AND attnum > 0 AND NOT attisdropped;
    SELECT array_agg(attname::text || ' ' || format_type(atttypid, atttypmod)) INTO v_obs_actual
    FROM pg_attribute WHERE attrelid = v_obs AND attnum > 0 AND NOT attisdropped;

    IF NOT (v_obs_actual @> v_obs_expected AND v_obs_actual <@ v_obs_expected) THEN
        RAISE EXCEPTION 'migration 022 macro schema reconciliation found an unsupported public.macro_observations column shape';
    END IF;

    SELECT count(*) INTO v_hardening_constraints
    FROM pg_constraint
    WHERE (conrelid = v_series AND conname IN ('chk_macro_series_source_tier', 'chk_macro_series_contract_status'))
       OR (conrelid = v_obs AND conname IN ('chk_macro_obs_complete_has_value', 'chk_macro_obs_data_status',
                                            'chk_macro_obs_confidence', 'chk_macro_obs_source_tier', 'chk_macro_obs_precision'));

    IF v_series_actual @> v_series_current AND v_series_actual <@ v_series_current THEN
        -- Already carries contract_status: only an already-hardened schema is accepted (no-op; rows are allowed).
        IF v_hardening_constraints <> 7
           OR EXISTS (
                SELECT 1 FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
                WHERE (d.adrelid = v_series AND a.attname IN ('source_tier', 'geography'))
                   OR (d.adrelid = v_obs AND a.attname IN ('data_status', 'confidence_level', 'source_tier', 'availability_precision'))
           )
           OR EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = v_obs AND attname = 'availability_precision' AND attnotnull)
           OR NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = v_series AND attname = 'contract_status' AND attnotnull) THEN
            RAISE EXCEPTION 'migration 022 macro schema reconciliation found a partially hardened macro schema (unsupported hybrid shape)';
        END IF;
        RETURN;
    END IF;

    IF NOT (v_series_actual @> v_series_legacy AND v_series_actual <@ v_series_legacy) THEN
        RAISE EXCEPTION 'migration 022 macro schema reconciliation found an unsupported public.macro_series column shape';
    END IF;
    IF v_hardening_constraints <> 0 THEN
        RAISE EXCEPTION 'migration 022 macro schema reconciliation found a legacy macro schema that already has some hardening constraints (unsupported hybrid shape)';
    END IF;

    SELECT count(*) INTO v_series_rows FROM public.macro_series;
    SELECT count(*) INTO v_obs_rows FROM public.macro_observations;
    IF v_series_rows <> 0 OR v_obs_rows <> 0 THEN
        RAISE EXCEPTION 'migration 022 macro schema reconciliation requires empty legacy macro tables (macro_series and macro_observations must both have zero rows)';
    END IF;

    -- macro_series: current migration-006/007 contract
    ALTER TABLE public.macro_series ADD COLUMN contract_status VARCHAR(32) NOT NULL DEFAULT 'verified';
    ALTER TABLE public.macro_series ALTER COLUMN source_tier DROP DEFAULT;
    ALTER TABLE public.macro_series ALTER COLUMN geography DROP DEFAULT;
    ALTER TABLE public.macro_series ADD CONSTRAINT chk_macro_series_source_tier
        CHECK (source_tier IN ('tier_1', 'tier_2', 'tier_3', 'tier_4', 'tier_5'));
    ALTER TABLE public.macro_series ADD CONSTRAINT chk_macro_series_contract_status
        CHECK (contract_status IN ('verified', 'unverified', 'disabled'));

    -- macro_observations: no silent defaults, current migration-006/007 constraints
    ALTER TABLE public.macro_observations ALTER COLUMN data_status DROP DEFAULT;
    ALTER TABLE public.macro_observations ALTER COLUMN confidence_level DROP DEFAULT;
    ALTER TABLE public.macro_observations ALTER COLUMN source_tier DROP DEFAULT;
    ALTER TABLE public.macro_observations ALTER COLUMN availability_precision DROP NOT NULL;
    ALTER TABLE public.macro_observations ALTER COLUMN availability_precision DROP DEFAULT;
    ALTER TABLE public.macro_observations ADD CONSTRAINT chk_macro_obs_complete_has_value
        CHECK (data_status != 'complete' OR value IS NOT NULL);
    ALTER TABLE public.macro_observations ADD CONSTRAINT chk_macro_obs_data_status
        CHECK (data_status IN ('complete', 'partial', 'degraded', 'stale', 'unavailable'));
    ALTER TABLE public.macro_observations ADD CONSTRAINT chk_macro_obs_confidence
        CHECK (confidence_level IN ('high', 'medium', 'low', 'none'));
    ALTER TABLE public.macro_observations ADD CONSTRAINT chk_macro_obs_source_tier
        CHECK (source_tier IN ('tier_1', 'tier_2', 'tier_3', 'tier_4', 'tier_5'));
    ALTER TABLE public.macro_observations ADD CONSTRAINT chk_macro_obs_precision
        CHECK (availability_precision IS NULL OR availability_precision IN ('DATE', 'TIMESTAMP'));
END
$macro_schema_reconciliation$;

-- Current migration-007 vintage index (idempotent).
CREATE INDEX IF NOT EXISTS idx_macro_obs_vintage_date
    ON public.macro_observations (macro_series_id, vintage_date DESC)
    WHERE vintage_date IS NOT NULL;

-- Current migration-007 immutability authority (identical to the repository definition; supersession fields stay mutable).
CREATE OR REPLACE FUNCTION public.prevent_macro_observation_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Hard delete prohibited on macro_observations (id=%).', OLD.id;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        -- Strict allow-list: ONLY is_superseded and superseded_at may change
        IF (
            OLD.id != NEW.id OR
            OLD.macro_series_id != NEW.macro_series_id OR
            (OLD.snapshot_id IS DISTINCT FROM NEW.snapshot_id) OR
            OLD.effective_date != NEW.effective_date OR
            (OLD.value IS DISTINCT FROM NEW.value) OR
            OLD.unit != NEW.unit OR
            OLD.frequency != NEW.frequency OR
            OLD.data_status != NEW.data_status OR
            OLD.confidence_level != NEW.confidence_level OR
            OLD.source_tier != NEW.source_tier OR
            (OLD.published_at IS DISTINCT FROM NEW.published_at) OR
            OLD.observed_at != NEW.observed_at OR
            OLD.ingested_at != NEW.ingested_at OR
            (OLD.source_available_date IS DISTINCT FROM NEW.source_available_date) OR
            (OLD.availability_precision IS DISTINCT FROM NEW.availability_precision) OR
            (OLD.realtime_end IS DISTINCT FROM NEW.realtime_end) OR
            (OLD.vintage_date IS DISTINCT FROM NEW.vintage_date) OR
            (OLD.origin_source IS DISTINCT FROM NEW.origin_source) OR
            (OLD.release_name IS DISTINCT FROM NEW.release_name) OR
            (OLD.supersedes_record_id IS DISTINCT FROM NEW.supersedes_record_id) OR
            (OLD.warnings::text != NEW.warnings::text) OR
            (OLD.source_ref IS DISTINCT FROM NEW.source_ref) OR
            OLD.created_at != NEW.created_at
        ) THEN
            RAISE EXCEPTION 'Full-row immutability violation on macro_observations (id=%). Only supersession fields may be updated.', OLD.id;
        END IF;

        IF OLD.is_superseded = false AND NEW.is_superseded = true THEN
            IF NEW.superseded_at IS NULL THEN
                NEW.superseded_at := timezone('utc'::text, now());
            END IF;
            RETURN NEW;
        END IF;

        IF OLD.is_superseded = true AND NEW.is_superseded = false THEN
            RAISE EXCEPTION 'Cannot un-supersede a macro observation (id=%).', OLD.id;
        END IF;

        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_protect_macro_observation_immutability ON public.macro_observations;
CREATE TRIGGER trg_protect_macro_observation_immutability
    BEFORE UPDATE OR DELETE ON public.macro_observations
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_macro_observation_tamper();

-- Current migration-006 automatic supersession authority (identical to the repository definition).
CREATE OR REPLACE FUNCTION public.handle_macro_observation_supersession()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.supersedes_record_id IS NOT NULL THEN
        UPDATE public.macro_observations
        SET is_superseded = true,
            superseded_at = NEW.ingested_at
        WHERE id = NEW.supersedes_record_id
          AND is_superseded = false;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_auto_supersede_macro_observation ON public.macro_observations;
CREATE TRIGGER trg_auto_supersede_macro_observation
    AFTER INSERT ON public.macro_observations
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_macro_observation_supersession();

-- ============================================================================
-- B. Phase 17B read-only PIT RPC (analytically unchanged)
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
