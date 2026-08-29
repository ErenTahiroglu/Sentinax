-- 018_fee_tax_attribution_events.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 14F: Fee/Tax Attribution Event Persistence Schema & DB Invariants
--
-- Persistent Table:
--   public.portfolio_fee_tax_attribution_events
--
-- Core Invariants Enforced at DB Level:
--   - Separate immutable append-only evidence stream for fee/tax charge-to-transaction attributions.
--   - Explicit primary key (id) with NO database default (ID generation owned by backend).
--   - Explicit recorded_at timestamp with NO database default (system-knowledge clock owned by backend).
--   - Foreign key integrity referencing public.portfolios(id, owner_id) ON DELETE RESTRICT.
--   - Foreign key integrity referencing public.portfolio_accounts(id, portfolio_id) ON DELETE RESTRICT.
--   - Composite candidate key (id, portfolio_id, account_id) enabling scope-safe self-referential reversals.
--   - Composite FK for charge_transaction_id referencing public.portfolio_transactions(id, portfolio_id, account_id) ON DELETE RESTRICT.
--   - Composite FK for target_transaction_id referencing public.portfolio_transactions(id, portfolio_id, account_id) ON DELETE RESTRICT.
--   - Composite FK for reverses_attribution_event_id referencing public.portfolio_fee_tax_attribution_events(id, portfolio_id, account_id) ON DELETE RESTRICT.
--   - Strict event-type check constraint: IN ('allocation', 'reversal').
--   - Unconstrained exact NUMERIC for allocated_amount; rejects NaN, Infinity, -Infinity; requires > 0 for allocations.
--   - Mutually exclusive field-family CHECK constraint for ALLOCATION vs REVERSAL.
--   - Self-attribution rejection: charge_transaction_id <> target_transaction_id.
--   - Self-reversal rejection: id <> reverses_attribution_event_id.
--   - Partial unique index on reverses_attribution_event_id enforcing single reversal per attribution event.
--   - Relational validation trigger enforcing:
--       * ALLOCATION charge transaction type is strictly ('fee', 'tax_withholding').
--       * ALLOCATION target transaction type is strictly ('buy', 'sell', 'dividend', 'interest', 'cash_deposit', 'cash_withdrawal', 'fx_conversion').
--       * REVERSAL target event is strictly of type 'allocation' (anti-reversal-of-reversal).
--   - Dedicated anti-tamper immutability trigger preventing UPDATE and DELETE.
--   - Owner-scoped Row Level Security (RLS) with authenticated SELECT only.
--   - Defense-in-depth privilege revocation: no direct writes for PUBLIC, anon, or authenticated; service_role granted SELECT and INSERT only.

-- ============================================================================
-- 1. public.portfolio_fee_tax_attribution_events
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.portfolio_fee_tax_attribution_events (
    id UUID NOT NULL,
    portfolio_id UUID NOT NULL,
    account_id UUID NOT NULL,
    owner_id UUID NOT NULL,

    event_type VARCHAR(32) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,

    charge_transaction_id UUID,
    target_transaction_id UUID,
    allocated_amount NUMERIC,
    reverses_attribution_event_id UUID,

    -- ─────────────────────────────────────────────────────────────────────────
    -- Primary Key & Candidate Keys
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT pk_portfolio_fee_tax_attribution_events
        PRIMARY KEY (id),

    CONSTRAINT uq_fee_tax_attribution_event_id_portfolio_account
        UNIQUE (id, portfolio_id, account_id),

    -- ─────────────────────────────────────────────────────────────────────────
    -- Referential Integrity Constraints (ON DELETE RESTRICT)
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT fk_fee_tax_attribution_events_portfolio
        FOREIGN KEY (portfolio_id, owner_id)
        REFERENCES public.portfolios(id, owner_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_fee_tax_attribution_events_account
        FOREIGN KEY (account_id, portfolio_id)
        REFERENCES public.portfolio_accounts(id, portfolio_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_fee_tax_attribution_events_charge_tx
        FOREIGN KEY (charge_transaction_id, portfolio_id, account_id)
        REFERENCES public.portfolio_transactions(id, portfolio_id, account_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_fee_tax_attribution_events_target_tx
        FOREIGN KEY (target_transaction_id, portfolio_id, account_id)
        REFERENCES public.portfolio_transactions(id, portfolio_id, account_id)
        ON DELETE RESTRICT,

    CONSTRAINT fk_fee_tax_attribution_events_reversal
        FOREIGN KEY (reverses_attribution_event_id, portfolio_id, account_id)
        REFERENCES public.portfolio_fee_tax_attribution_events(id, portfolio_id, account_id)
        ON DELETE RESTRICT,

    -- ─────────────────────────────────────────────────────────────────────────
    -- Domain Value & Check Constraints
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT chk_fee_tax_attribution_event_type
        CHECK (event_type IN ('allocation', 'reversal')),

    CONSTRAINT chk_fee_tax_attribution_allocated_amount
        CHECK (
            allocated_amount IS NULL
            OR (
                allocated_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
                AND allocated_amount > 0
            )
        ),

    -- ─────────────────────────────────────────────────────────────────────────
    -- Mutually Exclusive Field-Family CHECK Constraints (Phase 14E & 14F)
    -- ─────────────────────────────────────────────────────────────────────────
    CONSTRAINT chk_fee_tax_attribution_field_families CHECK (
        -- ALLOCATION: Requires charge_tx, target_tx, positive finite allocated_amount; prohibits reversal_id & self-link
        (
            event_type = 'allocation'
            AND charge_transaction_id IS NOT NULL
            AND target_transaction_id IS NOT NULL
            AND allocated_amount IS NOT NULL
            AND allocated_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
            AND allocated_amount > 0
            AND reverses_attribution_event_id IS NULL
            AND charge_transaction_id <> target_transaction_id
        )
        OR
        -- REVERSAL: Strictly reference-only. Requires reversal_id; prohibits all independent economics & self-reversal
        (
            event_type = 'reversal'
            AND charge_transaction_id IS NULL
            AND target_transaction_id IS NULL
            AND allocated_amount IS NULL
            AND reverses_attribution_event_id IS NOT NULL
            AND reverses_attribution_event_id <> id
        )
    )
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Indexes
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Single Reversal Uniqueness: An attribution event may be reversed at most once
CREATE UNIQUE INDEX IF NOT EXISTS uq_fee_tax_attribution_single_reversal
    ON public.portfolio_fee_tax_attribution_events (reverses_attribution_event_id)
    WHERE event_type = 'reversal';

-- 2. Deterministic Portfolio PIT Query Index
CREATE INDEX IF NOT EXISTS idx_fee_tax_attribution_events_pit
    ON public.portfolio_fee_tax_attribution_events (portfolio_id, recorded_at, id);

-- 3. Account-Scoped PIT Query Index
CREATE INDEX IF NOT EXISTS idx_fee_tax_attribution_events_account_pit
    ON public.portfolio_fee_tax_attribution_events (portfolio_id, account_id, recorded_at, id);

-- 4. Charge Transaction Lookup Index
CREATE INDEX IF NOT EXISTS idx_fee_tax_attribution_events_charge_tx
    ON public.portfolio_fee_tax_attribution_events (charge_transaction_id)
    WHERE charge_transaction_id IS NOT NULL;

-- 5. Target Transaction Lookup Index
CREATE INDEX IF NOT EXISTS idx_fee_tax_attribution_events_target_tx
    ON public.portfolio_fee_tax_attribution_events (target_transaction_id)
    WHERE target_transaction_id IS NOT NULL;


-- ============================================================================
-- 2. Relational Semantic & Anti-Reversal-of-Reversal Validation Trigger
-- ============================================================================

CREATE OR REPLACE FUNCTION public.validate_fee_tax_attribution_event_integrity()
RETURNS TRIGGER AS $$
DECLARE
    v_charge_type VARCHAR(32);
    v_target_type VARCHAR(32);
    v_reversed_event_type VARCHAR(32);
BEGIN
    IF NEW.event_type = 'allocation' THEN
        -- 1. Validate charge transaction type
        SELECT transaction_type INTO v_charge_type
        FROM public.portfolio_transactions
        WHERE id = NEW.charge_transaction_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id;

        IF v_charge_type IS NULL THEN
            RAISE EXCEPTION 'Referenced charge transaction % not found in portfolio % account %.',
                NEW.charge_transaction_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        IF v_charge_type NOT IN ('fee', 'tax_withholding') THEN
            RAISE EXCEPTION 'Invalid charge transaction type % for attribution charge %. Must be fee or tax_withholding.',
                v_charge_type, NEW.charge_transaction_id;
        END IF;

        -- 2. Validate target transaction type
        SELECT transaction_type INTO v_target_type
        FROM public.portfolio_transactions
        WHERE id = NEW.target_transaction_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id;

        IF v_target_type IS NULL THEN
            RAISE EXCEPTION 'Referenced target transaction % not found in portfolio % account %.',
                NEW.target_transaction_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        IF v_target_type NOT IN (
            'buy',
            'sell',
            'dividend',
            'interest',
            'cash_deposit',
            'cash_withdrawal',
            'fx_conversion'
        ) THEN
            RAISE EXCEPTION 'Invalid target transaction type % for attribution target %. Prohibited target type.',
                v_target_type, NEW.target_transaction_id;
        END IF;

    ELSIF NEW.event_type = 'reversal' THEN
        -- Validate referenced attribution event exists and is ALLOCATION (reversal of reversal rejected)
        SELECT event_type INTO v_reversed_event_type
        FROM public.portfolio_fee_tax_attribution_events
        WHERE id = NEW.reverses_attribution_event_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id;

        IF v_reversed_event_type IS NULL THEN
            RAISE EXCEPTION 'Referenced attribution event % not found in portfolio % account %.',
                NEW.reverses_attribution_event_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        IF v_reversed_event_type <> 'allocation' THEN
            RAISE EXCEPTION 'Reversal of reversal is prohibited: referenced event % has event_type %.',
                NEW.reverses_attribution_event_id, v_reversed_event_type;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validate_fee_tax_attribution_event_integrity ON public.portfolio_fee_tax_attribution_events;
CREATE TRIGGER trg_validate_fee_tax_attribution_event_integrity
    BEFORE INSERT ON public.portfolio_fee_tax_attribution_events
    FOR EACH ROW
    EXECUTE FUNCTION public.validate_fee_tax_attribution_event_integrity();


-- ============================================================================
-- 3. Anti-Tamper Immutability Trigger
-- ============================================================================
-- Enforces that portfolio_fee_tax_attribution_events is STRICTLY append-only.
-- No UPDATE or DELETE is allowed under any circumstances.

CREATE OR REPLACE FUNCTION public.prevent_fee_tax_attribution_event_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Immutability violation: portfolio_fee_tax_attribution_events records cannot be updated.';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Immutability violation: portfolio_fee_tax_attribution_events records cannot be deleted.';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_fee_tax_attribution_event_tamper ON public.portfolio_fee_tax_attribution_events;
CREATE TRIGGER trg_prevent_fee_tax_attribution_event_tamper
    BEFORE UPDATE OR DELETE ON public.portfolio_fee_tax_attribution_events
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_fee_tax_attribution_event_tamper();


-- ============================================================================
-- 4. Row Level Security (RLS) & User Isolation
-- ============================================================================

ALTER TABLE public.portfolio_fee_tax_attribution_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own fee tax attribution events"
    ON public.portfolio_fee_tax_attribution_events
    FOR SELECT
    TO authenticated
    USING ((SELECT auth.uid()) = owner_id);


-- ============================================================================
-- 5. Privilege Hardening & Write-Surface Exclusivity
-- ============================================================================

-- Defense-in-depth: revoke all direct write privileges from PUBLIC, anon, and authenticated
REVOKE INSERT, UPDATE, DELETE ON TABLE public.portfolio_fee_tax_attribution_events FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON TABLE public.portfolio_fee_tax_attribution_events FROM anon;
REVOKE INSERT, UPDATE, DELETE ON TABLE public.portfolio_fee_tax_attribution_events FROM authenticated;

-- Grant owner-scoped SELECT to authenticated
GRANT SELECT ON TABLE public.portfolio_fee_tax_attribution_events TO authenticated;

-- Service role has SELECT and INSERT authority only (no UPDATE/DELETE)
REVOKE ALL ON TABLE public.portfolio_fee_tax_attribution_events FROM service_role;
GRANT SELECT, INSERT ON TABLE public.portfolio_fee_tax_attribution_events TO service_role;
