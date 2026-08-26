-- 005_instrument_identity.sql
-- Sentinax Private Personal Investment Decision Engine
-- Instrument Identity & Symbology Resolution Layer (Point-in-Time & Corporate Action Aware)

-- ============================================================================
-- 1. instruments (Master Instrument Table)
-- ============================================================================
-- Ticker is NOT the primary key. Every instrument has a synthetic/stable
-- internal_instrument_id and standard identifiers (ISIN, CIK, MIC/Exchange).

CREATE TABLE IF NOT EXISTS public.instruments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    internal_instrument_id VARCHAR(64) UNIQUE NOT NULL,
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
    name VARCHAR(255),
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

CREATE TABLE IF NOT EXISTS public.provider_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id UUID NOT NULL REFERENCES public.instruments(id) ON DELETE CASCADE,
    provider VARCHAR(64) NOT NULL,
    provider_symbol VARCHAR(64) NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    is_primary BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_provider_aliases_lookup
    ON public.provider_aliases (provider, provider_symbol, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_provider_aliases_instrument
    ON public.provider_aliases (instrument_id, provider, valid_from, valid_to);


-- ============================================================================
-- 3. corporate_actions (Corporate Actions & Reference Events)
-- ============================================================================
-- Minimal schema for symbol renames, splits, dividends, mergers, delistings,
-- and fund code changes. Enables historical time series adjustment and lookup continuity.

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
    old_value VARCHAR(255),
    new_value VARCHAR(255),
    factor NUMERIC(18, 8) DEFAULT 1.0, -- Split factor / adjustment multiplier
    amount NUMERIC(18, 6),             -- Dividend cash amount per share
    currency VARCHAR(10),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_instrument_date
    ON public.corporate_actions (instrument_id, effective_date DESC, action_type);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_type_date
    ON public.corporate_actions (action_type, effective_date DESC);


-- ============================================================================
-- 4. Symbology Resolver Functions (RPC)
-- ============================================================================

-- A. Resolve external provider symbol to internal instrument
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
      AND (pa.valid_to IS NULL OR pa.valid_to >= p_as_of_date)
    ORDER BY pa.is_primary DESC, pa.valid_from DESC
    LIMIT 1;
$$;

-- B. Resolve internal instrument ID to provider symbol for a specific date
CREATE OR REPLACE FUNCTION public.resolve_instrument_to_provider_symbol(
    p_internal_instrument_id VARCHAR(64),
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
      AND (pa.valid_to IS NULL OR pa.valid_to >= p_as_of_date)
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
