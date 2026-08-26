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
    
    -- Instrument Identification
    instrument_id VARCHAR(64) NOT NULL, -- e.g. 'THYAO.IS', 'AAPL', 'TP2', 'ALTIN.S1'
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
    currency VARCHAR(10) NOT NULL DEFAULT 'TRY',
    
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
-- 4. Point-In-Time (PIT) Query Helper (RPC)
-- ============================================================================
-- Returns the valid observation as known at a specific point in time (as_of_time),
-- strictly excluding any data ingested or published after that timestamp.

CREATE OR REPLACE FUNCTION public.get_pit_observation(
    p_instrument_id VARCHAR(64),
    p_observation_type VARCHAR(64),
    p_effective_date DATE,
    p_as_of_time TIMESTAMPTZ DEFAULT timezone('utc'::text, now())
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
      AND ingested_at <= p_as_of_time
      AND (published_at IS NULL OR published_at <= p_as_of_time)
      AND (superseded_at IS NULL OR superseded_at > p_as_of_time)
    ORDER BY ingested_at DESC
    LIMIT 1;
$$;


-- ============================================================================
-- 5. Row Level Security (RLS)
-- ============================================================================
ALTER TABLE public.raw_provider_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.normalized_observations ENABLE ROW LEVEL SECURITY;

-- Service role has full read/write
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

-- Authenticated users have read-only access to non-superseded observations
CREATE POLICY "Authenticated users can read active observations"
    ON public.normalized_observations
    FOR SELECT
    TO authenticated
    USING (is_superseded = false);
