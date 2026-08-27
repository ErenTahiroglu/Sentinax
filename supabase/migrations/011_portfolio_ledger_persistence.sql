-- 011_portfolio_ledger_persistence.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 12B.1: Portfolio Ledger Supabase Persistence Schema & DB Invariants
--
-- Persistent Tables:
--   1. public.portfolios (Root portfolio aggregate, MY_PORTFOLIO vs SANDBOX boundary)
--   2. public.portfolio_accounts (Custody / brokerage accounts)
--   3. public.cash_buckets (Liquidity & purpose buckets)
--   4. public.investment_goals (Financial targets)
--   5. public.planned_contributions (Forward-looking inflows; NOT cash authority)
--   6. public.portfolio_transactions (Append-only immutable transaction ledger)
--
-- Core Invariants Enforced at DB Level:
--   - Strict owner isolation referencing auth.users(id)
--   - Exact NUMERIC precision for all financial quantities (NO REAL / FLOAT / DOUBLE)
--   - Mutually exclusive transaction field families (fail-closed CHECK constraints)
--   - Strictly reference-only REVERSAL events with zero independent economics
--   - Single-reversal and anti-reversal-of-reversal protection
--   - Normalized partial unique index on external source + reference
--   - Immutability trigger preventing UPDATE and DELETE on portfolio_transactions
--   - Foreign key integrity referencing public.instruments(id) ON DELETE RESTRICT

-- ============================================================================
-- 1. public.portfolios (Root Aggregate)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.portfolios (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    mode VARCHAR(32) NOT NULL CHECK (mode IN ('my_portfolio', 'sandbox')),
    name VARCHAR(255) NOT NULL CHECK (length(trim(name)) > 0),
    base_currency VARCHAR(10) NOT NULL CHECK (length(trim(base_currency)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    archived_at TIMESTAMPTZ,
    source_portfolio_id UUID,
    source_snapshot_time TIMESTAMPTZ,

    -- Composite candidate key to enable composite foreign keys scoped by owner
    CONSTRAINT uq_portfolios_id_owner UNIQUE (id, owner_id),

    -- Provenance Integrity Rules:
    -- 1. MY_PORTFOLIO cannot have source_portfolio_id or source_snapshot_time
    -- 2. SANDBOX source_snapshot_time requires source_portfolio_id
    -- 3. SANDBOX cannot self-clone (source_portfolio_id != id)
    CONSTRAINT chk_portfolio_provenance CHECK (
        (mode = 'my_portfolio' AND source_portfolio_id IS NULL AND source_snapshot_time IS NULL)
        OR
        (
            mode = 'sandbox'
            AND (source_portfolio_id IS NULL OR source_portfolio_id != id)
            AND (source_snapshot_time IS NULL OR source_portfolio_id IS NOT NULL)
        )
    ),

    -- Sandbox origin provenance must reference a portfolio belonging to the SAME owner
    CONSTRAINT fk_portfolios_source_portfolio
        FOREIGN KEY (source_portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_portfolios_owner_mode
    ON public.portfolios (owner_id, mode);

CREATE INDEX IF NOT EXISTS idx_portfolios_source_portfolio
    ON public.portfolios (source_portfolio_id)
    WHERE source_portfolio_id IS NOT NULL;


-- ============================================================================
-- 2. public.portfolio_accounts (Brokerage / Custody Accounts)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.portfolio_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL CHECK (length(trim(name)) > 0),
    base_currency VARCHAR(10) NOT NULL CHECK (length(trim(base_currency)) > 0),
    broker_label VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    archived_at TIMESTAMPTZ,

    -- Enforce portfolio and owner consistency
    CONSTRAINT fk_portfolio_accounts_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    -- Candidate keys for child entity referential integrity
    CONSTRAINT uq_portfolio_accounts_id_portfolio UNIQUE (id, portfolio_id),
    CONSTRAINT uq_portfolio_accounts_id_owner UNIQUE (id, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_accounts_portfolio
    ON public.portfolio_accounts (portfolio_id);

CREATE INDEX IF NOT EXISTS idx_portfolio_accounts_owner
    ON public.portfolio_accounts (owner_id);


-- ============================================================================
-- 3. public.cash_buckets (Liquidity & Purpose Categorization)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.cash_buckets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    account_id UUID, -- Optional: NULL for portfolio-wide cash buckets
    name VARCHAR(255) NOT NULL CHECK (length(trim(name)) > 0),
    currency VARCHAR(10) NOT NULL CHECK (length(trim(currency)) > 0),
    purpose VARCHAR(32) NOT NULL CHECK (
        purpose IN ('investable', 'emergency_reserve', 'near_term', 'restricted_other')
    ),
    included_in_investable_assets BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    archived_at TIMESTAMPTZ,

    -- Enforce portfolio and owner consistency
    CONSTRAINT fk_cash_buckets_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    -- Account, if present, must belong to the same portfolio
    CONSTRAINT fk_cash_buckets_account
        FOREIGN KEY (account_id, portfolio_id)
        REFERENCES public.portfolio_accounts(id, portfolio_id)
        ON DELETE RESTRICT,

    -- Candidate keys for transaction reference validation
    CONSTRAINT uq_cash_buckets_id_portfolio UNIQUE (id, portfolio_id),
    CONSTRAINT uq_cash_buckets_id_portfolio_currency UNIQUE (id, portfolio_id, currency)
);

CREATE INDEX IF NOT EXISTS idx_cash_buckets_portfolio_purpose
    ON public.cash_buckets (portfolio_id, purpose);

CREATE INDEX IF NOT EXISTS idx_cash_buckets_account
    ON public.cash_buckets (account_id)
    WHERE account_id IS NOT NULL;


-- ============================================================================
-- 4. public.investment_goals (Financial Target Aggregate)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.investment_goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL CHECK (length(trim(name)) > 0),
    target_amount NUMERIC NOT NULL CHECK (target_amount > 0),
    target_currency VARCHAR(10) NOT NULL CHECK (length(trim(target_currency)) > 0),
    target_date DATE,
    priority VARCHAR(20) NOT NULL DEFAULT 'medium' CHECK (
        priority IN ('low', 'medium', 'high', 'critical')
    ),
    status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'paused', 'completed', 'cancelled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    archived_at TIMESTAMPTZ,

    -- Enforce portfolio and owner consistency
    CONSTRAINT fk_investment_goals_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    CONSTRAINT uq_investment_goals_id_portfolio UNIQUE (id, portfolio_id)
);

CREATE INDEX IF NOT EXISTS idx_investment_goals_portfolio_status
    ON public.investment_goals (portfolio_id, status);


-- ============================================================================
-- 5. public.planned_contributions (Forward-Looking Inflows; NOT Cash Authority)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.planned_contributions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    goal_id UUID,
    cash_bucket_id UUID,
    expected_date DATE NOT NULL,
    amount NUMERIC NOT NULL CHECK (amount > 0),
    currency VARCHAR(10) NOT NULL CHECK (length(trim(currency)) > 0),
    status VARCHAR(20) NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned', 'received', 'cancelled', 'deferred')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    -- Enforce portfolio and owner consistency
    CONSTRAINT fk_planned_contributions_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    -- Goal, if present, must belong to the same portfolio
    CONSTRAINT fk_planned_contributions_goal
        FOREIGN KEY (goal_id, portfolio_id)
        REFERENCES public.investment_goals(id, portfolio_id)
        ON DELETE RESTRICT,

    -- Cash bucket, if present, must belong to the same portfolio
    CONSTRAINT fk_planned_contributions_cash_bucket
        FOREIGN KEY (cash_bucket_id, portfolio_id)
        REFERENCES public.cash_buckets(id, portfolio_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_planned_contributions_portfolio_expected
    ON public.planned_contributions (portfolio_id, expected_date);


-- ============================================================================
-- 6. public.portfolio_transactions (Append-Only Immutable Ledger Authority)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.portfolio_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL,
    account_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    transaction_type VARCHAR(32) NOT NULL CHECK (
        transaction_type IN (
            'buy', 'sell', 'cash_deposit', 'cash_withdrawal',
            'dividend', 'interest', 'fx_conversion', 'fee',
            'tax_withholding', 'reversal'
        )
    ),

    -- Time Axes (Decoupled Economic vs System Ingestion Time)
    effective_date DATE NOT NULL,
    executed_at TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),

    -- Trade Security Fields (BUY, SELL)
    instrument_id UUID REFERENCES public.instruments(id) ON DELETE RESTRICT,
    quantity NUMERIC CHECK (quantity IS NULL OR quantity > 0),
    unit_price NUMERIC CHECK (unit_price IS NULL OR unit_price > 0),
    trade_currency VARCHAR(10),

    -- Cash Movement Fields (CASH_DEPOSIT, CASH_WITHDRAWAL, DIVIDEND, INTEREST, FEE, TAX_WITHHOLDING)
    cash_amount NUMERIC CHECK (cash_amount IS NULL OR cash_amount > 0),
    cash_currency VARCHAR(10),
    cash_bucket_id UUID,

    -- FX Conversion Fields (Two-leg Economics)
    from_currency VARCHAR(10),
    from_amount NUMERIC CHECK (from_amount IS NULL OR from_amount > 0),
    to_currency VARCHAR(10),
    to_amount NUMERIC CHECK (to_amount IS NULL OR to_amount > 0),

    -- External Source & Idempotency Metadata
    external_source VARCHAR(64),
    external_reference VARCHAR(255),

    -- Reversal Reference Link (Self-reference on transactions)
    reverses_transaction_id UUID REFERENCES public.portfolio_transactions(id) ON DELETE RESTRICT,

    -- Audit Metadata & Deterministic Economic Fingerprint
    notes TEXT,
    economic_fingerprint VARCHAR(64) NOT NULL,

    -- Referential Integrity Constraints
    CONSTRAINT fk_portfolio_transactions_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_portfolio_transactions_account
        FOREIGN KEY (account_id, portfolio_id)
        REFERENCES public.portfolio_accounts(id, portfolio_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_portfolio_transactions_cash_bucket
        FOREIGN KEY (cash_bucket_id, portfolio_id)
        REFERENCES public.cash_buckets(id, portfolio_id)
        ON DELETE RESTRICT,

    -- Candidate key for reversal validation
    CONSTRAINT uq_portfolio_transactions_id_portfolio_account
        UNIQUE (id, portfolio_id, account_id),

    -- ─────────────────────────────────────────────────────────────────────────
    -- External Identity All-or-None CHECK Constraint (Phase 12A.6)
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT chk_tx_external_identity CHECK (
        (external_source IS NULL AND external_reference IS NULL)
        OR
        (
            external_source IS NOT NULL AND external_reference IS NOT NULL
            AND length(trim(external_source)) > 0
            AND length(trim(external_reference)) > 0
        )
    ),

    -- ─────────────────────────────────────────────────────────────────────────
    -- Mutually Exclusive Field-Family CHECK Constraints (Phase 12A.5 & 12A.6)
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT chk_tx_field_families CHECK (
        -- BUY / SELL: Requires security fields, rejects cash amounts/currencies, FX legs, and reversal ID
        (
            transaction_type IN ('buy', 'sell')
            AND instrument_id IS NOT NULL
            AND quantity IS NOT NULL AND quantity > 0
            AND unit_price IS NOT NULL AND unit_price > 0
            AND trade_currency IS NOT NULL
            AND cash_amount IS NULL
            AND cash_currency IS NULL
            AND from_currency IS NULL AND from_amount IS NULL
            AND to_currency IS NULL AND to_amount IS NULL
            AND reverses_transaction_id IS NULL
        )
        OR
        -- CASH_DEPOSIT / CASH_WITHDRAWAL: Requires cash_amount > 0 and cash_currency, rejects security and FX fields
        (
            transaction_type IN ('cash_deposit', 'cash_withdrawal')
            AND cash_amount IS NOT NULL AND cash_amount > 0
            AND cash_currency IS NOT NULL
            AND instrument_id IS NULL
            AND quantity IS NULL AND unit_price IS NULL AND trade_currency IS NULL
            AND from_currency IS NULL AND from_amount IS NULL
            AND to_currency IS NULL AND to_amount IS NULL
            AND reverses_transaction_id IS NULL
        )
        OR
        -- DIVIDEND / INTEREST / FEE / TAX_WITHHOLDING: Requires cash_amount > 0 and cash_currency, optional instrument_id
        (
            transaction_type IN ('dividend', 'interest', 'fee', 'tax_withholding')
            AND cash_amount IS NOT NULL AND cash_amount > 0
            AND cash_currency IS NOT NULL
            AND quantity IS NULL AND unit_price IS NULL AND trade_currency IS NULL
            AND from_currency IS NULL AND from_amount IS NULL
            AND to_currency IS NULL AND to_amount IS NULL
            AND reverses_transaction_id IS NULL
        )
        OR
        -- FX_CONVERSION: Requires distinct from/to currencies and positive amounts, rejects all security and simple cash fields
        (
            transaction_type = 'fx_conversion'
            AND from_currency IS NOT NULL
            AND from_amount IS NOT NULL AND from_amount > 0
            AND to_currency IS NOT NULL
            AND to_amount IS NOT NULL AND to_amount > 0
            AND from_currency != to_currency
            AND instrument_id IS NULL
            AND quantity IS NULL AND unit_price IS NULL AND trade_currency IS NULL
            AND cash_amount IS NULL AND cash_currency IS NULL AND cash_bucket_id IS NULL
            AND reverses_transaction_id IS NULL
        )
        OR
        -- REVERSAL: Strictly reference-only. All independent economic fields MUST be NULL.
        (
            transaction_type = 'reversal'
            AND reverses_transaction_id IS NOT NULL
            AND reverses_transaction_id != id
            AND instrument_id IS NULL
            AND quantity IS NULL AND unit_price IS NULL AND trade_currency IS NULL
            AND cash_amount IS NULL AND cash_currency IS NULL AND cash_bucket_id IS NULL
            AND from_currency IS NULL AND from_amount IS NULL
            AND to_currency IS NULL AND to_amount IS NULL
        )
    )
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Indexes for Portfolio Transactions
-- ─────────────────────────────────────────────────────────────────────────────

-- Deterministic Audit Timeline Index
CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_audit_order
    ON public.portfolio_transactions (portfolio_id, effective_date, executed_at, recorded_at, id);

-- Account Lookup Index
CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_account
    ON public.portfolio_transactions (account_id, effective_date);

-- Instrument Lookup Index
CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_instrument
    ON public.portfolio_transactions (instrument_id, effective_date)
    WHERE instrument_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- External Idempotency Partial Unique Index (DB Race Safety)
-- ─────────────────────────────────────────────────────────────────────────────
-- Normalized upper(trim(source)) and trim(reference) enforced for external transactions.
-- Manual transactions (source IS NULL) are NOT covered by this uniqueness constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_transactions_external_idempotency
    ON public.portfolio_transactions (
        portfolio_id,
        account_id,
        upper(trim(external_source)),
        trim(external_reference)
    )
    WHERE external_source IS NOT NULL AND external_reference IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Single Reversal Partial Unique Index
-- ─────────────────────────────────────────────────────────────────────────────
-- Guarantees at database level that a transaction may be reversed at most once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_transactions_unique_reversal
    ON public.portfolio_transactions (reverses_transaction_id)
    WHERE transaction_type = 'reversal' AND reverses_transaction_id IS NOT NULL;


-- ============================================================================
-- 7. Cross-Row Reversal & Cash-Bucket Consistency Trigger
-- ============================================================================

CREATE OR REPLACE FUNCTION public.validate_portfolio_transaction_integrity()
RETURNS TRIGGER AS $$
DECLARE
    v_target RECORD;
    v_bucket RECORD;
BEGIN
    -- 1. Reversal Integrity Validation
    IF NEW.transaction_type = 'reversal' THEN
        SELECT id, portfolio_id, account_id, transaction_type
        INTO v_target
        FROM public.portfolio_transactions
        WHERE id = NEW.reverses_transaction_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Target transaction % for reversal not found in portfolio_transactions.', NEW.reverses_transaction_id;
        END IF;

        IF v_target.portfolio_id != NEW.portfolio_id THEN
            RAISE EXCEPTION 'Cross-portfolio reversal rejected: target portfolio % != reversal portfolio %.',
                v_target.portfolio_id, NEW.portfolio_id;
        END IF;

        IF v_target.account_id != NEW.account_id THEN
            RAISE EXCEPTION 'Cross-account reversal rejected: target account % != reversal account %.',
                v_target.account_id, NEW.account_id;
        END IF;

        IF v_target.transaction_type = 'reversal' THEN
            RAISE EXCEPTION 'Reversal of a reversal transaction is strictly forbidden.';
        END IF;
    END IF;

    -- 2. Cash Bucket Reference & Currency Validation
    IF NEW.cash_bucket_id IS NOT NULL THEN
        SELECT id, portfolio_id, account_id, currency
        INTO v_bucket
        FROM public.cash_buckets
        WHERE id = NEW.cash_bucket_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Referenced cash_bucket_id % does not exist in cash_buckets.', NEW.cash_bucket_id;
        END IF;

        IF v_bucket.portfolio_id != NEW.portfolio_id THEN
            RAISE EXCEPTION 'CashBucket portfolio % does not match transaction portfolio %.',
                v_bucket.portfolio_id, NEW.portfolio_id;
        END IF;

        IF v_bucket.account_id IS NOT NULL AND v_bucket.account_id != NEW.account_id THEN
            RAISE EXCEPTION 'CashBucket account % does not match transaction account %.',
                v_bucket.account_id, NEW.account_id;
        END IF;

        -- Currency consistency check
        IF NEW.transaction_type IN ('cash_deposit', 'cash_withdrawal', 'dividend', 'interest', 'fee', 'tax_withholding') THEN
            IF v_bucket.currency != NEW.cash_currency THEN
                RAISE EXCEPTION 'Referenced CashBucket currency % does not match transaction cash_currency %.',
                    v_bucket.currency, NEW.cash_currency;
            END IF;
        ELSIF NEW.transaction_type IN ('buy', 'sell') THEN
            IF v_bucket.currency != NEW.trade_currency THEN
                RAISE EXCEPTION 'Referenced funding CashBucket currency % does not match transaction trade_currency %.',
                    v_bucket.currency, NEW.trade_currency;
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validate_portfolio_transaction_integrity ON public.portfolio_transactions;
CREATE TRIGGER trg_validate_portfolio_transaction_integrity
    BEFORE INSERT ON public.portfolio_transactions
    FOR EACH ROW
    EXECUTE FUNCTION public.validate_portfolio_transaction_integrity();


-- ============================================================================
-- 8. Anti-Tamper Immutability Trigger on Portfolio Transactions
-- ============================================================================
-- Enforces that portfolio_transactions is STRICTLY append-only.
-- No UPDATE or DELETE is allowed under any circumstances. Corrections require REVERSAL.

CREATE OR REPLACE FUNCTION public.prevent_portfolio_transaction_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Immutability violation: portfolio_transactions records cannot be updated. Append a REVERSAL transaction instead.';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Immutability violation: portfolio_transactions records cannot be deleted.';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_portfolio_transaction_tamper ON public.portfolio_transactions;
CREATE TRIGGER trg_prevent_portfolio_transaction_tamper
    BEFORE UPDATE OR DELETE ON public.portfolio_transactions
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_portfolio_transaction_tamper();


-- ============================================================================
-- 9. Row Level Security (RLS) & User Isolation
-- ============================================================================

ALTER TABLE public.portfolios ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portfolio_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cash_buckets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.investment_goals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.planned_contributions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portfolio_transactions ENABLE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- Portfolios RLS
-- ─────────────────────────────────────────────────────────────────────────────
CREATE POLICY "Users can view and manage own portfolios"
    ON public.portfolios
    FOR ALL
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id)
    WITH CHECK ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Service role full access on portfolios"
    ON public.portfolios
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- Portfolio Accounts RLS
-- ─────────────────────────────────────────────────────────────────────────────
CREATE POLICY "Users can view and manage own portfolio accounts"
    ON public.portfolio_accounts
    FOR ALL
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id)
    WITH CHECK ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Service role full access on portfolio_accounts"
    ON public.portfolio_accounts
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- Cash Buckets RLS
-- ─────────────────────────────────────────────────────────────────────────────
CREATE POLICY "Users can view and manage own cash buckets"
    ON public.cash_buckets
    FOR ALL
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id)
    WITH CHECK ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Service role full access on cash_buckets"
    ON public.cash_buckets
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- Investment Goals RLS
-- ─────────────────────────────────────────────────────────────────────────────
CREATE POLICY "Users can view and manage own investment goals"
    ON public.investment_goals
    FOR ALL
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id)
    WITH CHECK ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Service role full access on investment_goals"
    ON public.investment_goals
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- Planned Contributions RLS
-- ─────────────────────────────────────────────────────────────────────────────
CREATE POLICY "Users can view and manage own planned contributions"
    ON public.planned_contributions
    FOR ALL
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id)
    WITH CHECK ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Service role full access on planned_contributions"
    ON public.planned_contributions
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────────────────────
-- Portfolio Transactions RLS (Append & View Only for Authenticated Users)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE POLICY "Users can view own portfolio transactions"
    ON public.portfolio_transactions
    FOR SELECT
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Users can insert own portfolio transactions"
    ON public.portfolio_transactions
    FOR INSERT
    TO authenticated
    WITH CHECK ((SELECT auth.uid()) = owner_id);

CREATE POLICY "Service role full access on portfolio_transactions"
    ON public.portfolio_transactions
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
