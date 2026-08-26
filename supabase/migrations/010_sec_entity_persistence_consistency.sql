-- 010_sec_entity_persistence_consistency.sql
-- Sentinax Private Personal Investment Decision Engine
-- SEC EDGAR 8A.6: Entity-Level Storage, Acceptance Local DateTime & Link Integrity Trigger

-- ============================================================================
-- 1. Deprecate instrument_id on Entity-Level Tables (sec_filings & sec_raw_facts)
-- ============================================================================
-- SEC filings and XBRL facts belong strictly to the issuer (CIK).
-- Security-level resolution occurs at query time via `instruments.cik`.

COMMENT ON COLUMN public.sec_filings.instrument_id IS
    'DEPRECATED: SEC filings are issuer-level entity records. Security associations resolve via instruments.cik at query time.';

COMMENT ON COLUMN public.sec_raw_facts.instrument_id IS
    'DEPRECATED: SEC raw facts are issuer-level entity records. Security associations resolve via instruments.cik at query time.';


-- ============================================================================
-- 2. Add acceptance_local_datetime to sec_filings
-- ============================================================================
-- Distinguishes timezone-aware acceptance timestamps (TIMESTAMPTZ) from
-- SEC local/unspecified timestamps (TIMESTAMP WITHOUT TIME ZONE e.g. YYYYMMDDHHMMSS).

ALTER TABLE public.sec_filings
    ADD COLUMN IF NOT EXISTS acceptance_local_datetime TIMESTAMP WITHOUT TIME ZONE;

CREATE INDEX IF NOT EXISTS idx_sec_filings_acceptance_local
    ON public.sec_filings (acceptance_local_datetime)
    WHERE acceptance_local_datetime IS NOT NULL;


-- ============================================================================
-- 3. Align sec_raw_facts.accession_number Nullability & Format Check
-- ============================================================================
-- SEC raw facts may rarely lack accession numbers in raw payloads; allow NULL
-- to prevent fabricating fake accession identifiers.

ALTER TABLE public.sec_raw_facts ALTER COLUMN accession_number DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sec_raw_facts_accession_format'
    ) THEN
        ALTER TABLE public.sec_raw_facts
            ADD CONSTRAINT chk_sec_raw_facts_accession_format
            CHECK (accession_number IS NULL OR accession_number ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$');
    END IF;
END $$;


-- ============================================================================
-- 4. Database-Level Fact-Filing Link Integrity Validation Trigger
-- ============================================================================
-- Enforces that sec_fact_filing_links can only be created between a fact and a filing
-- that share the exact same CIK and accession_number. Rejects facts with NULL accession.

CREATE OR REPLACE FUNCTION public.validate_sec_fact_filing_link_integrity()
RETURNS TRIGGER AS $$
DECLARE
    v_fact_cik VARCHAR(10);
    v_fact_accn VARCHAR(25);
    v_filing_cik VARCHAR(10);
    v_filing_accn VARCHAR(25);
BEGIN
    -- 1. Fetch referenced fact
    SELECT cik, accession_number INTO v_fact_cik, v_fact_accn
    FROM public.sec_raw_facts
    WHERE id = NEW.fact_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Referenced fact_id % does not exist in sec_raw_facts.', NEW.fact_id;
    END IF;

    -- 2. Reject linkage if fact has no accession number
    IF v_fact_accn IS NULL THEN
        RAISE EXCEPTION 'Cannot link fact_id % because it has a NULL accession_number.', NEW.fact_id;
    END IF;

    -- 3. Fetch referenced filing
    SELECT cik, accession_number INTO v_filing_cik, v_filing_accn
    FROM public.sec_filings
    WHERE id = NEW.filing_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Referenced filing_id % does not exist in sec_filings.', NEW.filing_id;
    END IF;

    -- 4. Enforce Accession consistency
    IF NEW.accession_number != v_fact_accn OR NEW.accession_number != v_filing_accn THEN
        RAISE EXCEPTION 'Accession mismatch in fact-filing link: link=%, fact=%, filing=%.',
            NEW.accession_number, v_fact_accn, v_filing_accn;
    END IF;

    -- 5. Enforce CIK consistency
    IF NEW.cik != v_fact_cik OR NEW.cik != v_filing_cik THEN
        RAISE EXCEPTION 'CIK mismatch in fact-filing link: link=%, fact=%, filing=%.',
            NEW.cik, v_fact_cik, v_filing_cik;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validate_sec_fact_filing_link_integrity ON public.sec_fact_filing_links;
CREATE TRIGGER trg_validate_sec_fact_filing_link_integrity
    BEFORE INSERT ON public.sec_fact_filing_links
    FOR EACH ROW
    EXECUTE FUNCTION public.validate_sec_fact_filing_link_integrity();
