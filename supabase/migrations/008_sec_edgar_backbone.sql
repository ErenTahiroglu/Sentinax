-- 008_sec_edgar_backbone.sql
-- Sentinax Private Personal Investment Decision Engine
-- SEC EDGAR Filing & Raw XBRL CompanyFacts Backbone Schema (Phase 8A)

-- ============================================================================
-- 1. sec_filings (Master SEC Submission & Filing Index)
-- ============================================================================
-- Represents individual SEC filings ingested from official EDGAR submissions API.
-- accession_number is the unique filing identifier (hyphenated e.g. 0000320193-24-000123).
-- acceptance_datetime is the official point-in-time knowledge boundary.
-- Append-only / immutable storage.

CREATE TABLE IF NOT EXISTS public.sec_filings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id UUID REFERENCES public.instruments(id) ON DELETE RESTRICT,
    cik VARCHAR(10) NOT NULL,
    accession_number VARCHAR(25) NOT NULL UNIQUE,
    form VARCHAR(32) NOT NULL,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    filing_date DATE,
    report_date DATE,
    acceptance_datetime TIMESTAMPTZ,
    acceptance_precision VARCHAR(32),
    act VARCHAR(32),
    file_number VARCHAR(64),
    film_number VARCHAR(64),
    items JSONB,
    size BIGINT,
    is_xbrl BOOLEAN,
    is_inline_xbrl BOOLEAN,
    primary_document VARCHAR(255),
    primary_doc_description TEXT,
    source_url TEXT,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    snapshot_id UUID REFERENCES public.raw_provider_snapshots(id) ON DELETE SET NULL,
    raw_metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_sec_filings_cik
    ON public.sec_filings (cik);

CREATE INDEX IF NOT EXISTS idx_sec_filings_instrument_id
    ON public.sec_filings (instrument_id) WHERE instrument_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sec_filings_form
    ON public.sec_filings (form);

CREATE INDEX IF NOT EXISTS idx_sec_filings_acceptance_datetime
    ON public.sec_filings (acceptance_datetime);

CREATE INDEX IF NOT EXISTS idx_sec_filings_report_date
    ON public.sec_filings (report_date);


-- ============================================================================
-- 2. sec_raw_facts (Raw XBRL Fact Entries from CompanyFacts API)
-- ============================================================================
-- Stores raw numerical and dimensional facts from non-custom taxonomies
-- (us-gaap, dei, ifrs-full, srt).
-- Period type is strictly instant or duration.
-- Numerical values stored with arbitrary precision (NUMERIC).
-- Append-only / immutable storage.

CREATE TABLE IF NOT EXISTS public.sec_raw_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id UUID REFERENCES public.instruments(id) ON DELETE RESTRICT,
    filing_id UUID REFERENCES public.sec_filings(id) ON DELETE RESTRICT,
    cik VARCHAR(10) NOT NULL,
    accession_number VARCHAR(25) NOT NULL,
    taxonomy VARCHAR(32) NOT NULL,
    concept VARCHAR(128) NOT NULL,
    label TEXT,
    description TEXT,
    unit VARCHAR(32) NOT NULL,
    value NUMERIC,
    start_date DATE,
    end_date DATE,
    period_type VARCHAR(16) NOT NULL CHECK (period_type IN ('instant', 'duration')),
    fiscal_year INTEGER,
    fiscal_period VARCHAR(16),
    form VARCHAR(32),
    filed_date DATE,
    frame VARCHAR(32),
    snapshot_id UUID REFERENCES public.raw_provider_snapshots(id) ON DELETE SET NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    raw_fact JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_sec_raw_facts_cik_concept
    ON public.sec_raw_facts (cik, taxonomy, concept);

CREATE INDEX IF NOT EXISTS idx_sec_raw_facts_accession
    ON public.sec_raw_facts (accession_number);

CREATE INDEX IF NOT EXISTS idx_sec_raw_facts_filing_id
    ON public.sec_raw_facts (filing_id) WHERE filing_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sec_raw_facts_end_date
    ON public.sec_raw_facts (end_date);

CREATE INDEX IF NOT EXISTS idx_sec_raw_facts_frame
    ON public.sec_raw_facts (frame) WHERE frame IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sec_raw_facts_instrument_id
    ON public.sec_raw_facts (instrument_id) WHERE instrument_id IS NOT NULL;


-- ============================================================================
-- 3. Immutability Guards for SEC Backbone
-- ============================================================================
-- Disallows destructive updates and deletes on raw SEC filings and facts.

CREATE OR REPLACE FUNCTION public.prevent_sec_immutability_violation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Sentinax SEC records are strictly immutable. Deletions and updates are prohibited.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sec_filings_immutable ON public.sec_filings;
CREATE TRIGGER trg_sec_filings_immutable
    BEFORE UPDATE OR DELETE ON public.sec_filings
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_sec_immutability_violation();

DROP TRIGGER IF EXISTS trg_sec_raw_facts_immutable ON public.sec_raw_facts;
CREATE TRIGGER trg_sec_raw_facts_immutable
    BEFORE UPDATE OR DELETE ON public.sec_raw_facts
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_sec_immutability_violation();
