-- 014_portfolio_import_claim_bindings.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 13P: Import Claim-Binding Persistence Schema & DB Invariants
--
-- Persistent Table:
--   public.portfolio_import_claim_bindings
--
-- Core Invariants Enforced at DB Level:
--   - Authoritative Composite Primary Key matching Phase 13O claim identity:
--       (portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256)
--   - expected_plan_sha256 is explicitly stored as the interpretation snapshot but EXCLUDED from PK uniqueness.
--   - Foreign key integrity referencing public.portfolio_transactions(id, portfolio_id, account_id) ON DELETE RESTRICT.
--   - Foreign key integrity referencing public.portfolios(id, owner_id) ON DELETE RESTRICT.
--   - Foreign key integrity referencing public.portfolio_accounts(id, portfolio_id) ON DELETE RESTRICT.
--   - Strict check constraints on source_key grammar (^[a-z0-9][a-z0-9._-]{0,63}$) and record_ordinal (>= 1).
--   - Strict lowercase 64-char hex check constraints on file_content_sha256, record_sha256, and expected_plan_sha256.
--   - Non-unique index on transaction_id (allowing many-to-one claim binding).
--   - Strict append-only immutability trigger preventing UPDATE and DELETE.
--   - Row Level Security (RLS) with auth.uid() owner isolation.

-- ============================================================================
-- 1. public.portfolio_import_claim_bindings
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.portfolio_import_claim_bindings (
    owner_id UUID NOT NULL,
    portfolio_id UUID NOT NULL,
    account_id UUID NOT NULL,

    source_key VARCHAR(64) NOT NULL,
    file_content_sha256 VARCHAR(64) NOT NULL,
    record_ordinal BIGINT NOT NULL,
    record_sha256 VARCHAR(64) NOT NULL,

    expected_plan_sha256 VARCHAR(64) NOT NULL,

    transaction_id UUID NOT NULL,

    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ─────────────────────────────────────────────────────────────────────────
    -- Primary Key: Authoritative Composite Raw Claim Identity (Phase 13O)
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT pk_portfolio_import_claim_bindings PRIMARY KEY (
        portfolio_id,
        account_id,
        source_key,
        file_content_sha256,
        record_ordinal,
        record_sha256
    ),

    -- ─────────────────────────────────────────────────────────────────────────
    -- Referential Integrity Constraints
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT fk_import_claim_bindings_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_import_claim_bindings_account
        FOREIGN KEY (account_id, portfolio_id)
        REFERENCES public.portfolio_accounts(id, portfolio_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_import_claim_bindings_transaction
        FOREIGN KEY (transaction_id, portfolio_id, account_id)
        REFERENCES public.portfolio_transactions(id, portfolio_id, account_id)
        ON DELETE RESTRICT,

    -- ─────────────────────────────────────────────────────────────────────────
    -- Check Constraints (Domain Grammar & Range)
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT chk_import_claim_bindings_source_key
        CHECK (source_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),

    CONSTRAINT chk_import_claim_bindings_file_content_sha256
        CHECK (file_content_sha256 ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_import_claim_bindings_record_ordinal
        CHECK (record_ordinal >= 1),

    CONSTRAINT chk_import_claim_bindings_record_sha256
        CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_import_claim_bindings_expected_plan_sha256
        CHECK (expected_plan_sha256 ~ '^[0-9a-f]{64}$')
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Index on transaction_id (Non-Unique to allow many claims -> 1 transaction)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_import_claim_bindings_transaction_id
    ON public.portfolio_import_claim_bindings (transaction_id);


-- ============================================================================
-- 2. Anti-Tamper Immutability Trigger
-- ============================================================================
-- Enforces that portfolio_import_claim_bindings is STRICTLY append-only.
-- No UPDATE or DELETE is allowed under any circumstances.

CREATE OR REPLACE FUNCTION public.prevent_import_claim_binding_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Immutability violation: portfolio_import_claim_bindings records cannot be updated.';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Immutability violation: portfolio_import_claim_bindings records cannot be deleted.';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_import_claim_binding_tamper ON public.portfolio_import_claim_bindings;
CREATE TRIGGER trg_prevent_import_claim_binding_tamper
    BEFORE UPDATE OR DELETE ON public.portfolio_import_claim_bindings
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_import_claim_binding_tamper();


-- ============================================================================
-- 3. Row Level Security (RLS) & User Isolation
-- ============================================================================

ALTER TABLE public.portfolio_import_claim_bindings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own import claim bindings"
    ON public.portfolio_import_claim_bindings
    FOR SELECT
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Users can insert own import claim bindings"
    ON public.portfolio_import_claim_bindings
    FOR INSERT
    TO authenticated
    WITH CHECK ((SELECT auth.uid()) = owner_id);
