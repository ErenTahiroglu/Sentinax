-- 004_private_engine_pit_storage.sql
-- Sentinax Private Personal Investment Decision Engine
-- Canonical Point-In-Time (PIT) & Immutable Data Storage Foundation

-- ============================================================================
-- 1. raw_provider_snapshots
-- ============================================================================
-- Immutable store of raw API responses, scraping payloads, and provider metadata.
-- Raw data is NEVER overwritten. Revisions create new records linking back.

CREATE TABLE IF NOT EXISTS public.raw_provider_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider VARCHAR(64) NOT NULL,
    endpoint VARCHAR(255) NOT NULL,
    request_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    http_status INTEGER,
    response_metadata JSONB NOT NULL DEFAULT '{}'::jsonb, -- headers, latency_ms, rate_limit info
    content_type VARCHAR(128) NOT NULL DEFAULT 'application/json',
    raw_payload JSONB,                                    -- Structured JSON payload
    storage_ref TEXT,                                     -- Optional external storage URI for large/blob payloads
    payload_hash VARCHAR(64) NOT NULL,                    -- SHA-256 hex digest of the raw content
    schema_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
    parser_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
    license_profile VARCHAR(64) NOT NULL DEFAULT 'PROPRIETARY', -- e.g. PUBLIC, PROPRIETARY, CC-BY, FAIR_USE
    
    -- Revision & Supersession tracking
    supersedes_record_id UUID REFERENCES public.raw_provider_snapshots(id) ON DELETE SET NULL,
    is_superseded BOOLEAN NOT NULL DEFAULT false,
    superseded_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

-- Indexes for raw provider snapshots
CREATE INDEX IF NOT EXISTS idx_raw_snapshots_provider_endpoint_retrieved
    ON public.raw_provider_snapshots (provider, endpoint, retrieved_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_snapshots_payload_hash
    ON public.raw_provider_snapshots (payload_hash);

CREATE INDEX IF NOT EXISTS idx_raw_snapshots_supersedes
    ON public.raw_provider_snapshots (supersedes_record_id)
    WHERE supersedes_record_id IS NOT NULL;


-- ============================================================================
-- 2. normalized_observations
-- ============================================================================
-- Point-in-Time (PIT) normalized facts derived from raw snapshots.
-- Enforces strict timestamp semantics to prevent look-ahead bias:
--   effective_date: The calendar date the economic truth applies to.
--   published_at:   When the data provider officially published it.
--   observed_at:    When Sentinax provider client captured it.
--   ingested_at:    When Sentinax database stored this record.
--   revised_at:     When a revised fact was issued by source.

CREATE TABLE IF NOT EXISTS public.normalized_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id UUID REFERENCES public.raw_provider_snapshots(id) ON DELETE RESTRICT,
    
    -- Instrument Identification (Canonical UUID reference to instruments.id)
    instrument_id UUID NOT NULL,
    asset_class VARCHAR(32) NOT NULL CHECK (
        asset_class IN ('equity', 'fund', 'commodity', 'fx', 'fixed_income', 'etf')
    ),
    instrument_type VARCHAR(32) NOT NULL,
    
    -- Observation Category & Payload
    observation_type VARCHAR(64) NOT NULL, -- e.g. 'PRICE_OHLCV', 'FINANCIAL_STATEMENT', 'FUND_METRIC', 'MACRO_SERIES'
    observation_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    
    -- Quality & Confidence Signals
    data_status VARCHAR(20) NOT NULL CHECK (
        data_status IN ('complete', 'partial', 'degraded', 'stale', 'unavailable')
    ),
    confidence_level VARCHAR(20) NOT NULL CHECK (
        confidence_level IN ('high', 'medium', 'low', 'none')
    ),
    source_tier VARCHAR(20) NOT NULL CHECK (
        source_tier IN ('tier_1', 'tier_2', 'tier_3', 'tier_4', 'tier_5')
    ),
    currency VARCHAR(10) NOT NULL, -- Explicit currency (No silent default)
    
    -- Timestamp Semantics (Point-In-Time)
    effective_date DATE NOT NULL,
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    revised_at TIMESTAMPTZ,
    
    -- Supersession & Audit
    supersedes_record_id UUID REFERENCES public.normalized_observations(id) ON DELETE SET NULL,
    is_superseded BOOLEAN NOT NULL DEFAULT false,
    superseded_at TIMESTAMPTZ,
    
    -- Missing Data & Quality Diagnostics
    missing_inputs TEXT[] NOT NULL DEFAULT '{}'::text[],
    warnings TEXT[] NOT NULL DEFAULT '{}'::text[],
    source_refs TEXT[] NOT NULL DEFAULT '{}'::text[],
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

-- Indexes for Point-in-Time querying & lookahead protection
CREATE INDEX IF NOT EXISTS idx_norm_obs_pit_lookup
    ON public.normalized_observations (instrument_id, observation_type, effective_date DESC, ingested_at DESC);

CREATE INDEX IF NOT EXISTS idx_norm_obs_active_pit
    ON public.normalized_observations (instrument_id, observation_type, effective_date)
    WHERE is_superseded = false;

CREATE INDEX IF NOT EXISTS idx_norm_obs_status_confidence
    ON public.normalized_observations (data_status, confidence_level);

CREATE INDEX IF NOT EXISTS idx_norm_obs_snapshot_ref
    ON public.normalized_observations (snapshot_id);

CREATE INDEX IF NOT EXISTS idx_norm_obs_supersedes_ref
    ON public.normalized_observations (supersedes_record_id)
    WHERE supersedes_record_id IS NOT NULL;


-- ============================================================================
-- 3. Automatic Supersession Trigger Function
-- ============================================================================
-- When a record specifies supersedes_record_id, automatically marks the target
-- record as is_superseded = true with superseded_at timestamp.

CREATE OR REPLACE FUNCTION public.handle_record_supersession()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.supersedes_record_id IS NOT NULL THEN
        -- Mark previous record in normalized_observations if table matches
        IF TG_TABLE_NAME = 'normalized_observations' THEN
            UPDATE public.normalized_observations
            SET is_superseded = true,
                superseded_at = timezone('utc'::text, now())
            WHERE id = NEW.supersedes_record_id
              AND is_superseded = false;
        ELSIF TG_TABLE_NAME = 'raw_provider_snapshots' THEN
            UPDATE public.raw_provider_snapshots
            SET is_superseded = true,
                superseded_at = timezone('utc'::text, now())
            WHERE id = NEW.supersedes_record_id
              AND is_superseded = false;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_supersede_raw_snapshot ON public.raw_provider_snapshots;
CREATE TRIGGER trg_supersede_raw_snapshot
    AFTER INSERT ON public.raw_provider_snapshots
    FOR EACH ROW
    WHEN (NEW.supersedes_record_id IS NOT NULL)
    EXECUTE FUNCTION public.handle_record_supersession();

DROP TRIGGER IF EXISTS trg_supersede_norm_observation ON public.normalized_observations;
CREATE TRIGGER trg_supersede_norm_observation
    AFTER INSERT ON public.normalized_observations
    FOR EACH ROW
    WHEN (NEW.supersedes_record_id IS NOT NULL)
    EXECUTE FUNCTION public.handle_record_supersession();


-- ============================================================================
-- 4. DB-Level Full-Row Immutability & Anti-Tamper Protection
-- ============================================================================
-- Prohibits application DELETE or destructive UPDATE on raw snapshots and observations.
-- Uses strict allow-list: ONLY is_superseded and superseded_at can change during supersession.
-- All other columns (including metadata, timestamps, supersedes_record_id) are strictly immutable.

CREATE OR REPLACE FUNCTION public.prevent_raw_snapshot_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'raw_provider_snapshots is append-only. Deleting records is prohibited.';
    ELSIF TG_OP = 'UPDATE' THEN
        -- Allow ONLY is_superseded and superseded_at state transitions
        IF (OLD.id IS DISTINCT FROM NEW.id) OR
           (OLD.provider IS DISTINCT FROM NEW.provider) OR
           (OLD.endpoint IS DISTINCT FROM NEW.endpoint) OR
           (OLD.request_params IS DISTINCT FROM NEW.request_params) OR
           (OLD.retrieved_at IS DISTINCT FROM NEW.retrieved_at) OR
           (OLD.http_status IS DISTINCT FROM NEW.http_status) OR
           (OLD.response_metadata IS DISTINCT FROM NEW.response_metadata) OR
           (OLD.content_type IS DISTINCT FROM NEW.content_type) OR
           (OLD.raw_payload IS DISTINCT FROM NEW.raw_payload) OR
           (OLD.storage_ref IS DISTINCT FROM NEW.storage_ref) OR
           (OLD.payload_hash IS DISTINCT FROM NEW.payload_hash) OR
           (OLD.schema_version IS DISTINCT FROM NEW.schema_version) OR
           (OLD.parser_version IS DISTINCT FROM NEW.parser_version) OR
           (OLD.license_profile IS DISTINCT FROM NEW.license_profile) OR
           (OLD.supersedes_record_id IS DISTINCT FROM NEW.supersedes_record_id) OR
           (OLD.created_at IS DISTINCT FROM NEW.created_at) THEN
            RAISE EXCEPTION 'raw_provider_snapshots is immutable. Only is_superseded and superseded_at may be updated by system supersession.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_protect_raw_snapshot_immutability ON public.raw_provider_snapshots;
CREATE TRIGGER trg_protect_raw_snapshot_immutability
    BEFORE UPDATE OR DELETE ON public.raw_provider_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_raw_snapshot_tamper();


CREATE OR REPLACE FUNCTION public.prevent_observation_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'normalized_observations is append-only. Deleting records is prohibited.';
    ELSIF TG_OP = 'UPDATE' THEN
        -- Allow ONLY is_superseded and superseded_at state transitions
        IF (OLD.id IS DISTINCT FROM NEW.id) OR
           (OLD.snapshot_id IS DISTINCT FROM NEW.snapshot_id) OR
           (OLD.instrument_id IS DISTINCT FROM NEW.instrument_id) OR
           (OLD.asset_class IS DISTINCT FROM NEW.asset_class) OR
           (OLD.instrument_type IS DISTINCT FROM NEW.instrument_type) OR
           (OLD.observation_type IS DISTINCT FROM NEW.observation_type) OR
           (OLD.observation_data IS DISTINCT FROM NEW.observation_data) OR
           (OLD.data_status IS DISTINCT FROM NEW.data_status) OR
           (OLD.confidence_level IS DISTINCT FROM NEW.confidence_level) OR
           (OLD.source_tier IS DISTINCT FROM NEW.source_tier) OR
           (OLD.currency IS DISTINCT FROM NEW.currency) OR
           (OLD.effective_date IS DISTINCT FROM NEW.effective_date) OR
           (OLD.published_at IS DISTINCT FROM NEW.published_at) OR
           (OLD.observed_at IS DISTINCT FROM NEW.observed_at) OR
           (OLD.ingested_at IS DISTINCT FROM NEW.ingested_at) OR
           (OLD.revised_at IS DISTINCT FROM NEW.revised_at) OR
           (OLD.supersedes_record_id IS DISTINCT FROM NEW.supersedes_record_id) OR
           (OLD.missing_inputs IS DISTINCT FROM NEW.missing_inputs) OR
           (OLD.warnings IS DISTINCT FROM NEW.warnings) OR
           (OLD.source_refs IS DISTINCT FROM NEW.source_refs) OR
           (OLD.created_at IS DISTINCT FROM NEW.created_at) THEN
            RAISE EXCEPTION 'normalized_observations is immutable. Only is_superseded and superseded_at may be updated by system supersession.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_protect_observation_immutability ON public.normalized_observations;
CREATE TRIGGER trg_protect_observation_immutability
    BEFORE UPDATE OR DELETE ON public.normalized_observations
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_observation_tamper();


-- ============================================================================
-- 5. Point-In-Time (PIT) Query Helper (RPC)
-- ============================================================================
-- Supports two distinct Point-in-Time querying modes:
--   'SYSTEM_AS_OF': Returns what Sentinax database had ingested and published by as_of_time.
--   'SOURCE_AS_OF': Returns what was publicly available in the world by as_of_time
--                   (uses published_at, with deterministic fallback to observed_at if published_at is NULL).
-- In both modes, any supersession that occurred AFTER as_of_time is ignored (record remains active at as_of_time).

CREATE OR REPLACE FUNCTION public.get_pit_observation(
    p_instrument_id UUID,
    p_observation_type VARCHAR(64),
    p_effective_date DATE,
    p_as_of_time TIMESTAMPTZ DEFAULT timezone('utc'::text, now()),
    p_as_of_mode VARCHAR(20) DEFAULT 'SYSTEM_AS_OF'
)
RETURNS SETOF public.normalized_observations
LANGUAGE sql
STABLE
AS $$
    SELECT *
    FROM public.normalized_observations
    WHERE instrument_id = p_instrument_id
      AND observation_type = p_observation_type
      AND effective_date = p_effective_date
      AND (
          -- SYSTEM_AS_OF mode: must be ingested AND published by as_of_time
          (p_as_of_mode = 'SYSTEM_AS_OF' 
           AND ingested_at <= p_as_of_time 
           AND (published_at IS NULL OR published_at <= p_as_of_time))
          OR
          -- SOURCE_AS_OF mode: published by as_of_time (fallback to observed_at if published_at is NULL)
          (p_as_of_mode = 'SOURCE_AS_OF' 
           AND COALESCE(published_at, observed_at) <= p_as_of_time)
      )
      -- Supersession condition: record must NOT have been superseded before or at as_of_time
      AND (superseded_at IS NULL OR superseded_at > p_as_of_time)
    ORDER BY 
        CASE WHEN p_as_of_mode = 'SYSTEM_AS_OF' THEN ingested_at ELSE COALESCE(published_at, observed_at) END DESC
    LIMIT 1;
$$;


-- ============================================================================
-- 6. Row Level Security (RLS)
-- ============================================================================
ALTER TABLE public.raw_provider_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.normalized_observations ENABLE ROW LEVEL SECURITY;

-- Service role has read/insert/limited update access
CREATE POLICY "Service role full access on raw_provider_snapshots"
    ON public.raw_provider_snapshots
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on normalized_observations"
    ON public.normalized_observations
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Authenticated users have read-only access to active observations
CREATE POLICY "Authenticated users can read active observations"
    ON public.normalized_observations
    FOR SELECT
    TO authenticated
    USING (is_superseded = false);
