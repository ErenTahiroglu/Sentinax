-- 005_instrument_identity.sql
-- Sentinax Private Personal Investment Decision Engine
-- Instrument Identity & Symbology Resolution Layer (Point-in-Time & Corporate Action Aware)

-- Enable btree_gist extension for interval exclusion constraints
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ============================================================================
-- 1. instruments (Master Instrument Table)
-- ============================================================================
-- Ticker, company name, or provider symbol are NEVER the primary key.
-- internal_instrument_id is a pure, immutable UUID completely independent of tickers.

CREATE TABLE IF NOT EXISTS public.instruments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    internal_instrument_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    canonical_name VARCHAR(255) NOT NULL,
    asset_class VARCHAR(32) NOT NULL CHECK (
        asset_class IN ('equity', 'fund', 'commodity', 'fx', 'fixed_income', 'etf')
    ),
    instrument_type VARCHAR(32) NOT NULL,
    isin VARCHAR(12),
    cik VARCHAR(10),
    mic VARCHAR(10) NOT NULL DEFAULT 'XIST', -- ISO 10383 Market Identifier Code
    currency VARCHAR(10) NOT NULL DEFAULT 'TRY',
    status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'delisted', 'suspended', 'merged')
    ),
    valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_instruments_internal_id
    ON public.instruments (internal_instrument_id);

CREATE INDEX IF NOT EXISTS idx_instruments_isin
    ON public.instruments (isin) WHERE isin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_instruments_cik
    ON public.instruments (cik) WHERE cik IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_instruments_status_mic
    ON public.instruments (status, mic);


-- ============================================================================
-- 2. provider_aliases (Symbology & Provider-Specific Tickers Over Time)
-- ============================================================================
-- Maps external data provider symbols (e.g. YFinance 'THYAO.IS', KAP 'THYAO',
-- TEFAS 'TP2', or historical renames like 'FB' -> 'META') to internal instrument IDs.
-- Uses half-open [valid_from, valid_to) interval semantics.
-- Strict exclusion constraint ensures NO overlapping date intervals for the same (provider, provider_symbol).

CREATE TABLE IF NOT EXISTS public.provider_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id UUID NOT NULL REFERENCES public.instruments(id) ON DELETE CASCADE,
    provider VARCHAR(64) NOT NULL,
    provider_symbol VARCHAR(64) NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    is_primary BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    -- Validate that valid_from < valid_to when valid_to is not null
    CONSTRAINT chk_alias_date_order CHECK (valid_to IS NULL OR valid_from < valid_to),

    -- Exclusion constraint: No overlapping intervals for the same provider + symbol
    CONSTRAINT provider_aliases_no_overlap EXCLUDE USING gist (
        provider WITH =,
        provider_symbol WITH =,
        daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
    )
);

CREATE INDEX IF NOT EXISTS idx_provider_aliases_lookup
    ON public.provider_aliases (provider, provider_symbol, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_provider_aliases_instrument
    ON public.provider_aliases (instrument_id, provider, valid_from, valid_to);


-- ============================================================================
-- 3. corporate_actions (Corporate Actions & Reference Events)
-- ============================================================================
-- Semantic schema with action-specific field isolation:
--   SPLIT: split_factor > 0, cash_amount MUST BE NULL
--   DIVIDEND: cash_amount >= 0, split_factor MUST BE NULL
--   SYMBOL_CHANGE: old_symbol & new_symbol set, split_factor & cash_amount NULL
--   MERGER / DELISTING / FUND_CODE_CHANGE: event-specific metadata

CREATE TABLE IF NOT EXISTS public.corporate_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id UUID NOT NULL REFERENCES public.instruments(id) ON DELETE CASCADE,
    action_type VARCHAR(32) NOT NULL CHECK (
        action_type IN ('symbol_change', 'split', 'dividend', 'merger', 'delisting', 'fund_code_change')
    ),
    effective_date DATE NOT NULL,
    announced_date DATE,
    record_date DATE,
    ex_date DATE,
    
    -- Action-specific isolated fields
    old_symbol VARCHAR(64),
    new_symbol VARCHAR(64),
    split_factor NUMERIC(18, 8), -- Used ONLY for split events
    cash_amount NUMERIC(18, 6),  -- Used ONLY for dividend events
    currency VARCHAR(10),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    -- Semantic validation constraints
    CONSTRAINT chk_ca_split CHECK (
        action_type != 'split' OR (split_factor IS NOT NULL AND split_factor > 0 AND cash_amount IS NULL)
    ),
    CONSTRAINT chk_ca_dividend CHECK (
        action_type != 'dividend' OR (cash_amount IS NOT NULL AND cash_amount >= 0 AND split_factor IS NULL)
    ),
    CONSTRAINT chk_ca_symbol_change CHECK (
        action_type != 'symbol_change' OR (old_symbol IS NOT NULL AND new_symbol IS NOT NULL AND split_factor IS NULL AND cash_amount IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_instrument_date
    ON public.corporate_actions (instrument_id, effective_date DESC, action_type);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_type_date
    ON public.corporate_actions (action_type, effective_date DESC);


-- ============================================================================
-- 4. Symbology Resolver Functions (RPC)
-- ============================================================================

-- A. Resolve external provider symbol to internal instrument using [valid_from, valid_to) range
CREATE OR REPLACE FUNCTION public.resolve_provider_symbol_to_instrument(
    p_provider VARCHAR(64),
    p_provider_symbol VARCHAR(64),
    p_as_of_date DATE DEFAULT CURRENT_DATE
)
RETURNS SETOF public.instruments
LANGUAGE sql
STABLE
AS $$
    SELECT i.*
    FROM public.instruments i
    JOIN public.provider_aliases pa ON pa.instrument_id = i.id
    WHERE pa.provider = p_provider
      AND pa.provider_symbol = p_provider_symbol
      AND pa.valid_from <= p_as_of_date
      AND (pa.valid_to IS NULL OR pa.valid_to > p_as_of_date)
    ORDER BY pa.is_primary DESC, pa.valid_from DESC
    LIMIT 1;
$$;

-- B. Resolve internal instrument UUID to provider symbol for a specific date
CREATE OR REPLACE FUNCTION public.resolve_instrument_to_provider_symbol(
    p_internal_instrument_id UUID,
    p_provider VARCHAR(64),
    p_as_of_date DATE DEFAULT CURRENT_DATE
)
RETURNS TABLE (
    provider_symbol VARCHAR(64),
    valid_from DATE,
    valid_to DATE,
    is_primary BOOLEAN
)
LANGUAGE sql
STABLE
AS $$
    SELECT pa.provider_symbol, pa.valid_from, pa.valid_to, pa.is_primary
    FROM public.provider_aliases pa
    JOIN public.instruments i ON i.id = pa.instrument_id
    WHERE i.internal_instrument_id = p_internal_instrument_id
      AND pa.provider = p_provider
      AND pa.valid_from <= p_as_of_date
      AND (pa.valid_to IS NULL OR pa.valid_to > p_as_of_date)
    ORDER BY pa.is_primary DESC, pa.valid_from DESC
    LIMIT 1;
$$;


-- ============================================================================
-- 5. Row Level Security (RLS)
-- ============================================================================
ALTER TABLE public.instruments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.provider_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corporate_actions ENABLE ROW LEVEL SECURITY;

-- Service role full access
CREATE POLICY "Service role full access on instruments"
    ON public.instruments FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access on provider_aliases"
    ON public.provider_aliases FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access on corporate_actions"
    ON public.corporate_actions FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Authenticated users read access
CREATE POLICY "Authenticated users read instruments"
    ON public.instruments FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users read provider_aliases"
    ON public.provider_aliases FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users read corporate_actions"
    ON public.corporate_actions FOR SELECT TO authenticated USING (true);
