-- ============================================================================
-- 🏛️ Sentinax Migration 006: Macroeconomic Series & PIT Observations (Hardened)
-- ============================================================================
-- Bounded context: Macroeconomic data layer (TCMB, TÜİK, ENAG).
-- Decouples macroeconomic series from equity/fund instruments.
--
-- Tables created:
--   1. public.macro_series       — Canonical macro series registry (FX, Inflation, Rates)
--   2. public.macro_observations — Point-in-Time (PIT) observations with anti-tamper triggers
-- ============================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 1. macro_series
-- ============================================================================
-- Canonical registry of macroeconomic indicators.

CREATE TABLE IF NOT EXISTS public.macro_series (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_key VARCHAR(64) UNIQUE NOT NULL,       -- e.g. 'TR_FX_USDTRY', 'TR_CPI_TUIK_YOY'
    provider VARCHAR(32) NOT NULL,                    -- e.g. 'TCMB_EVDS', 'TUIK_SDMX', 'ENAG_MANUAL'
    provider_series_code VARCHAR(128) NOT NULL,       -- e.g. 'TP.DK.USD.A.YTL', 'CPI_INDEX_2003'
    category VARCHAR(32) NOT NULL,                    -- fx, interest_rate, inflation_cpi, inflation_ppi
    description TEXT NOT NULL,
    unit VARCHAR(32) NOT NULL,                        -- TRY, PERCENT, INDEX_POINTS
    frequency VARCHAR(16) NOT NULL,                   -- daily, monthly, quarterly, annual
    freshness_basis VARCHAR(32) NOT NULL DEFAULT 'effective_date',
    source_tier VARCHAR(32) NOT NULL,                 -- Explicit NOT NULL without silent tier_1 default
    contract_status VARCHAR(32) NOT NULL DEFAULT 'verified',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    CONSTRAINT chk_macro_series_source_tier CHECK (source_tier IN ('tier_1', 'tier_2', 'tier_3', 'tier_4', 'tier_5')),
    CONSTRAINT chk_macro_series_contract_status CHECK (contract_status IN ('verified', 'unverified', 'disabled'))
);

CREATE INDEX IF NOT EXISTS idx_macro_series_provider
    ON public.macro_series (provider, provider_series_code);

-- ============================================================================
-- 2. macro_observations
-- ============================================================================
-- Point-in-Time observation facts for macroeconomic indicators.

CREATE TABLE IF NOT EXISTS public.macro_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    macro_series_id UUID NOT NULL REFERENCES public.macro_series(id) ON DELETE RESTRICT,
    snapshot_id UUID REFERENCES public.raw_provider_snapshots(id) ON DELETE SET NULL,
    effective_date DATE NOT NULL,
    value NUMERIC(18, 6),                             -- Nullable: missing observation is NULL (NOT 0.0)
    unit VARCHAR(32) NOT NULL,
    frequency VARCHAR(16) NOT NULL,
    data_status VARCHAR(32) NOT NULL,                 -- Explicit NOT NULL without silent complete default
    confidence_level VARCHAR(16) NOT NULL,            -- Explicit NOT NULL without silent high default
    source_tier VARCHAR(32) NOT NULL,                 -- Explicit NOT NULL without silent tier_1 default
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    supersedes_record_id UUID REFERENCES public.macro_observations(id) ON DELETE SET NULL,
    is_superseded BOOLEAN NOT NULL DEFAULT false,
    superseded_at TIMESTAMPTZ,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    CONSTRAINT chk_macro_obs_complete_has_value CHECK (data_status != 'complete' OR value IS NOT NULL),
    CONSTRAINT chk_macro_obs_data_status CHECK (data_status IN ('complete', 'partial', 'degraded', 'stale', 'unavailable')),
    CONSTRAINT chk_macro_obs_confidence CHECK (confidence_level IN ('high', 'medium', 'low', 'none')),
    CONSTRAINT chk_macro_obs_source_tier CHECK (source_tier IN ('tier_1', 'tier_2', 'tier_3', 'tier_4', 'tier_5'))
);

CREATE INDEX IF NOT EXISTS idx_macro_obs_series_effective
    ON public.macro_observations (macro_series_id, effective_date DESC);

CREATE INDEX IF NOT EXISTS idx_macro_obs_pit_published
    ON public.macro_observations (macro_series_id, published_at DESC)
    WHERE published_at IS NOT NULL;

-- ============================================================================
-- 3. Anti-Tamper Allow-List Immutability Trigger for macro_observations
-- ============================================================================

CREATE OR REPLACE FUNCTION public.prevent_macro_observation_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Hard delete prohibited on macro_observations (id=%).', OLD.id;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        -- Allow-list: ONLY is_superseded and superseded_at may change. All other columns must remain strictly identical.
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

CREATE TRIGGER trg_protect_macro_observation_immutability
    BEFORE UPDATE OR DELETE ON public.macro_observations
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_macro_observation_tamper();

-- ============================================================================
-- 4. Automatic Supersession Trigger on Revision Insert
-- ============================================================================

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

CREATE TRIGGER trg_auto_supersede_macro_observation
    AFTER INSERT ON public.macro_observations
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_macro_observation_supersession();

-- ============================================================================
-- 5. Point-in-Time (PIT) RPC for Macro Observations
-- ============================================================================

CREATE OR REPLACE FUNCTION public.get_pit_macro_observation(
    p_canonical_key VARCHAR(64),
    p_effective_date DATE,
    p_as_of TIMESTAMPTZ,
    p_as_of_mode VARCHAR(16) DEFAULT 'SYSTEM_AS_OF'
)
RETURNS SETOF public.macro_observations AS $$
BEGIN
    IF p_as_of_mode = 'SYSTEM_AS_OF' THEN
        RETURN QUERY
        SELECT o.*
        FROM public.macro_observations o
        JOIN public.macro_series s ON o.macro_series_id = s.id
        WHERE s.canonical_key = p_canonical_key
          AND o.effective_date = p_effective_date
          AND o.ingested_at <= p_as_of
          AND (o.published_at IS NULL OR o.published_at <= p_as_of)
          AND (o.superseded_at IS NULL OR o.superseded_at > p_as_of)
        ORDER BY o.ingested_at DESC
        LIMIT 1;
    ELSIF p_as_of_mode = 'SOURCE_AS_OF' THEN
        RETURN QUERY
        SELECT o.*
        FROM public.macro_observations o
        JOIN public.macro_series s ON o.macro_series_id = s.id
        WHERE s.canonical_key = p_canonical_key
          AND o.effective_date = p_effective_date
          AND COALESCE(o.published_at, o.observed_at) <= p_as_of
          AND (o.superseded_at IS NULL OR o.superseded_at > p_as_of)
        ORDER BY COALESCE(o.published_at, o.observed_at) DESC
        LIMIT 1;
    ELSE
        RAISE EXCEPTION 'Invalid as_of_mode: %. Must be SYSTEM_AS_OF or SOURCE_AS_OF.', p_as_of_mode;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- ============================================================================
-- 6. Row-Level Security (RLS)
-- ============================================================================
ALTER TABLE public.macro_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.macro_observations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read access on macro_series"
    ON public.macro_series FOR SELECT TO authenticated, anon USING (true);

CREATE POLICY "Allow public read access on macro_observations"
    ON public.macro_observations FOR SELECT TO authenticated, anon USING (true);
