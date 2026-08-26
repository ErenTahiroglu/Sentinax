-- 009_sec_edgar_hardening.sql
-- Sentinax Private Personal Investment Decision Engine
-- SEC EDGAR 8A Hardening: Lineage Link Table, PIT Availability, Decimal & Integrity Constraints

-- ============================================================================
-- 1. Integrity Constraints & Defaults Cleanup on sec_filings
-- ============================================================================

-- Enforce 10-digit CIK regex
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sec_filings_cik_format'
    ) THEN
        ALTER TABLE public.sec_filings
            ADD CONSTRAINT chk_sec_filings_cik_format CHECK (cik ~ '^[0-9]{10}$');
    END IF;
END $$;

-- Enforce hyphenated accession number format (10-2-6 digits)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sec_filings_accession_format'
    ) THEN
        ALTER TABLE public.sec_filings
            ADD CONSTRAINT chk_sec_filings_accession_format CHECK (accession_number ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$');
    END IF;
END $$;

-- Remove silent application defaults from sec_filings
ALTER TABLE public.sec_filings ALTER COLUMN is_amendment DROP DEFAULT;
ALTER TABLE public.sec_filings ALTER COLUMN retrieved_at DROP DEFAULT;

-- Add explicit acceptance metadata and public availability columns
ALTER TABLE public.sec_filings
    ADD COLUMN IF NOT EXISTS public_available_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS public_availability_basis VARCHAR(32),
    ADD COLUMN IF NOT EXISTS acceptance_raw TEXT,
    ADD COLUMN IF NOT EXISTS acceptance_timezone_semantics VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_sec_filings_public_available_at
    ON public.sec_filings (public_available_at) WHERE public_available_at IS NOT NULL;


-- ============================================================================
-- 2. Integrity Constraints & Defaults Cleanup on sec_raw_facts
-- ============================================================================

-- Enforce 10-digit CIK regex on sec_raw_facts
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sec_raw_facts_cik_format'
    ) THEN
        ALTER TABLE public.sec_raw_facts
            ADD CONSTRAINT chk_sec_raw_facts_cik_format CHECK (cik ~ '^[0-9]{10}$');
    END IF;
END $$;

-- Remove silent application defaults from sec_raw_facts
ALTER TABLE public.sec_raw_facts ALTER COLUMN retrieved_at DROP DEFAULT;


-- ============================================================================
-- 3. Append-Only Fact-Filing Linkage Table (sec_fact_filing_links)
-- ============================================================================
-- Allows resolving fact-to-filing lineage asynchronously when historical filings
-- are ingested, without violating the strict immutability trigger of sec_raw_facts.

CREATE TABLE IF NOT EXISTS public.sec_fact_filing_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id UUID NOT NULL REFERENCES public.sec_raw_facts(id) ON DELETE CASCADE,
    filing_id UUID NOT NULL REFERENCES public.sec_filings(id) ON DELETE RESTRICT,
    accession_number VARCHAR(25) NOT NULL,
    cik VARCHAR(10) NOT NULL,
    resolution_method VARCHAR(32) NOT NULL DEFAULT 'ACCESSION_MATCH',
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    CONSTRAINT chk_sec_links_cik_format CHECK (cik ~ '^[0-9]{10}$'),
    CONSTRAINT uq_sec_fact_filing_link UNIQUE (fact_id)
);

CREATE INDEX IF NOT EXISTS idx_sec_fact_filing_links_filing
    ON public.sec_fact_filing_links (filing_id);

CREATE INDEX IF NOT EXISTS idx_sec_fact_filing_links_accession
    ON public.sec_fact_filing_links (accession_number);

CREATE INDEX IF NOT EXISTS idx_sec_fact_filing_links_cik
    ON public.sec_fact_filing_links (cik);


-- ============================================================================
-- 4. Immutability Trigger on sec_fact_filing_links
-- ============================================================================

DROP TRIGGER IF EXISTS trg_sec_fact_filing_links_immutable ON public.sec_fact_filing_links;
CREATE TRIGGER trg_sec_fact_filing_links_immutable
    BEFORE UPDATE OR DELETE ON public.sec_fact_filing_links
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_sec_immutability_violation();


-- ============================================================================
-- 5. Row Level Security (RLS) Configuration
-- ============================================================================

ALTER TABLE public.sec_filings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sec_raw_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sec_fact_filing_links ENABLE ROW LEVEL SECURITY;

-- Service role full access policies
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'service_role_manage_sec_filings' AND tablename = 'sec_filings'
    ) THEN
        CREATE POLICY service_role_manage_sec_filings ON public.sec_filings
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'service_role_manage_sec_raw_facts' AND tablename = 'sec_raw_facts'
    ) THEN
        CREATE POLICY service_role_manage_sec_raw_facts ON public.sec_raw_facts
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'service_role_manage_sec_fact_filing_links' AND tablename = 'sec_fact_filing_links'
    ) THEN
        CREATE POLICY service_role_manage_sec_fact_filing_links ON public.sec_fact_filing_links
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END $$;

-- Authenticated read-only access policies
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'authenticated_read_sec_filings' AND tablename = 'sec_filings'
    ) THEN
        CREATE POLICY authenticated_read_sec_filings ON public.sec_filings
            FOR SELECT TO authenticated USING (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'authenticated_read_sec_raw_facts' AND tablename = 'sec_raw_facts'
    ) THEN
        CREATE POLICY authenticated_read_sec_raw_facts ON public.sec_raw_facts
            FOR SELECT TO authenticated USING (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'authenticated_read_sec_fact_filing_links' AND tablename = 'sec_fact_filing_links'
    ) THEN
        CREATE POLICY authenticated_read_sec_fact_filing_links ON public.sec_fact_filing_links
            FOR SELECT TO authenticated USING (true);
    END IF;
END $$;
