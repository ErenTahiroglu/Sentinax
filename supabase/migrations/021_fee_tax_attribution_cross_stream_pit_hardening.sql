-- 021_fee_tax_attribution_cross_stream_pit_hardening.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 14G.2: Cross-Stream PIT Backdating & Attribution-Reversal Lock-Domain Hardening
--
-- Migration Purpose:
--   1. Hardens public.lock_portfolio_transaction_reversal_target() on
--      public.portfolio_transactions to:
--        - Lock referenced ledger transaction row with FOR UPDATE.
--        - Query already-persisted Phase 14 attribution evidence involving that transaction
--          (as charge or target, including attribution reversals).
--        - Enforce strict cross-stream non-backdating: ledger reversal recorded_at must be
--          STRICTLY GREATER THAN (>) the maximum recorded_at of any existing related attribution evidence.
--   2. Hardens public.validate_fee_tax_attribution_event_integrity() on
--      public.portfolio_fee_tax_attribution_events to:
--        - Ensure attribution-event REVERSAL resolves and locks BOTH the referenced allocation's
--          charge AND target ledger transactions in deterministic order (charge -> target) with FOR UPDATE.
--        - Preserve all Phase 14F/G/G.1 invariants (capacity, duplicate-pair rejection, causality,
--          per-charge monotonic history, active-at-PIT ledger checks).

-- ============================================================================
-- 1. Hardened Ledger REVERSAL Synchronization & Cross-Stream PIT Function
-- ============================================================================

CREATE OR REPLACE FUNCTION public.lock_portfolio_transaction_reversal_target()
RETURNS TRIGGER AS $$
DECLARE
    v_max_related_attribution_recorded_at TIMESTAMPTZ;
BEGIN
    IF NEW.transaction_type = 'reversal' AND NEW.reverses_transaction_id IS NOT NULL THEN
        -- 1. Lock referenced ledger transaction as concurrency mutex
        PERFORM 1
        FROM public.portfolio_transactions
        WHERE id = NEW.reverses_transaction_id
        FOR UPDATE;

        -- 2. Inspect already-persisted attribution evidence involving this transaction
        SELECT MAX(ae.recorded_at)
        INTO v_max_related_attribution_recorded_at
        FROM public.portfolio_fee_tax_attribution_events ae
        WHERE ae.portfolio_id = NEW.portfolio_id
          AND ae.account_id = NEW.account_id
          AND (
              ae.charge_transaction_id = NEW.reverses_transaction_id
              OR ae.target_transaction_id = NEW.reverses_transaction_id
              OR ae.reverses_attribution_event_id IN (
                  SELECT alloc.id
                  FROM public.portfolio_fee_tax_attribution_events alloc
                  WHERE alloc.portfolio_id = NEW.portfolio_id
                    AND alloc.account_id = NEW.account_id
                    AND (
                        alloc.charge_transaction_id = NEW.reverses_transaction_id
                        OR alloc.target_transaction_id = NEW.reverses_transaction_id
                    )
              )
          );

        -- 3. Strict cross-stream non-backdating enforcement
        IF v_max_related_attribution_recorded_at IS NOT NULL AND NEW.recorded_at <= v_max_related_attribution_recorded_at THEN
            RAISE EXCEPTION 'Cross-stream PIT backdating violation: ledger reversal recorded_at (%) must be strictly greater than latest related attribution evidence recorded_at (%) for transaction %.',
                NEW.recorded_at, v_max_related_attribution_recorded_at, NEW.reverses_transaction_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lock_portfolio_transaction_reversal_target ON public.portfolio_transactions;
CREATE TRIGGER trg_lock_portfolio_transaction_reversal_target
    BEFORE INSERT ON public.portfolio_transactions
    FOR EACH ROW
    EXECUTE FUNCTION public.lock_portfolio_transaction_reversal_target();


-- ============================================================================
-- 2. Hardened Fee/Tax Attribution Event Validation Function
-- ============================================================================

CREATE OR REPLACE FUNCTION public.validate_fee_tax_attribution_event_integrity()
RETURNS TRIGGER AS $$
DECLARE
    v_charge_id UUID;
    v_charge_type VARCHAR(32);
    v_charge_cash_amount NUMERIC;
    v_charge_recorded_at TIMESTAMPTZ;

    v_target_id UUID;
    v_target_type VARCHAR(32);
    v_target_recorded_at TIMESTAMPTZ;

    v_reversed_event_type VARCHAR(32);
    v_ref_allocation_recorded_at TIMESTAMPTZ;

    v_active_allocated_total NUMERIC;
    v_max_prior_recorded_at TIMESTAMPTZ;
BEGIN
    IF NEW.event_type = 'allocation' THEN
        v_charge_id := NEW.charge_transaction_id;
        v_target_id := NEW.target_transaction_id;

        -- 1. Lock and validate referenced charge transaction (Lock #1 in deterministic order)
        SELECT transaction_type, cash_amount, recorded_at
        INTO v_charge_type, v_charge_cash_amount, v_charge_recorded_at
        FROM public.portfolio_transactions
        WHERE id = v_charge_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id
        FOR UPDATE;

        IF v_charge_type IS NULL THEN
            RAISE EXCEPTION 'Referenced charge transaction % not found in portfolio % account %.',
                v_charge_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        IF v_charge_type NOT IN ('fee', 'tax_withholding') THEN
            RAISE EXCEPTION 'Invalid charge transaction type % for attribution charge %. Must be fee or tax_withholding.',
                v_charge_type, v_charge_id;
        END IF;

        IF v_charge_cash_amount IS NULL OR v_charge_cash_amount <= 0 THEN
            RAISE EXCEPTION 'Referenced charge transaction % has invalid cash_amount %.',
                v_charge_id, v_charge_cash_amount;
        END IF;

        -- Verify charge was not already reversed in ledger at or before attribution recorded_at
        IF EXISTS (
            SELECT 1
            FROM public.portfolio_transactions rev
            WHERE rev.portfolio_id = NEW.portfolio_id
              AND rev.account_id = NEW.account_id
              AND rev.transaction_type = 'reversal'
              AND rev.reverses_transaction_id = v_charge_id
              AND rev.recorded_at <= NEW.recorded_at
        ) THEN
            RAISE EXCEPTION 'Referenced charge transaction % was already reversed in ledger at or before attribution recorded_at (%).',
                v_charge_id, NEW.recorded_at;
        END IF;

        -- 2. Lock and validate referenced target transaction (Lock #2 in deterministic order)
        SELECT transaction_type, recorded_at
        INTO v_target_type, v_target_recorded_at
        FROM public.portfolio_transactions
        WHERE id = v_target_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id
        FOR UPDATE;

        IF v_target_type IS NULL THEN
            RAISE EXCEPTION 'Referenced target transaction % not found in portfolio % account %.',
                v_target_id, NEW.portfolio_id, NEW.account_id;
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
                v_target_type, v_target_id;
        END IF;

        -- Verify target was not already reversed in ledger at or before attribution recorded_at
        IF EXISTS (
            SELECT 1
            FROM public.portfolio_transactions rev
            WHERE rev.portfolio_id = NEW.portfolio_id
              AND rev.account_id = NEW.account_id
              AND rev.transaction_type = 'reversal'
              AND rev.reverses_transaction_id = v_target_id
              AND rev.recorded_at <= NEW.recorded_at
        ) THEN
            RAISE EXCEPTION 'Referenced target transaction % was already reversed in ledger at or before attribution recorded_at (%).',
                v_target_id, NEW.recorded_at;
        END IF;

        -- 3. Single-allocation capacity check
        IF NEW.allocated_amount > v_charge_cash_amount THEN
            RAISE EXCEPTION 'Allocation amount % exceeds charge cash_amount % for charge %.',
                NEW.allocated_amount, v_charge_cash_amount, v_charge_id;
        END IF;

        -- 4. Active cumulative allocation capacity check
        SELECT COALESCE(SUM(ae.allocated_amount), 0::numeric)
        INTO v_active_allocated_total
        FROM public.portfolio_fee_tax_attribution_events ae
        WHERE ae.portfolio_id = NEW.portfolio_id
          AND ae.account_id = NEW.account_id
          AND ae.charge_transaction_id = v_charge_id
          AND ae.event_type = 'allocation'
          AND NOT EXISTS (
              SELECT 1
              FROM public.portfolio_fee_tax_attribution_events rev
              WHERE rev.portfolio_id = NEW.portfolio_id
                AND rev.account_id = NEW.account_id
                AND rev.event_type = 'reversal'
                AND rev.reverses_attribution_event_id = ae.id
          );

        IF v_active_allocated_total + NEW.allocated_amount > v_charge_cash_amount THEN
            RAISE EXCEPTION 'Cumulative active allocations (% + %) exceed charge cash_amount % for charge %.',
                v_active_allocated_total, NEW.allocated_amount, v_charge_cash_amount, v_charge_id;
        END IF;

        -- 5. Active duplicate charge->target pair rejection
        IF EXISTS (
            SELECT 1
            FROM public.portfolio_fee_tax_attribution_events ae
            WHERE ae.portfolio_id = NEW.portfolio_id
              AND ae.account_id = NEW.account_id
              AND ae.charge_transaction_id = v_charge_id
              AND ae.target_transaction_id = v_target_id
              AND ae.event_type = 'allocation'
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.portfolio_fee_tax_attribution_events rev
                  WHERE rev.portfolio_id = NEW.portfolio_id
                    AND rev.account_id = NEW.account_id
                    AND rev.event_type = 'reversal'
                    AND rev.reverses_attribution_event_id = ae.id
              )
        ) THEN
            RAISE EXCEPTION 'Active attribution already exists for charge % and target % in portfolio % account %.',
                v_charge_id, v_target_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        -- 6. Knowledge-time causality
        IF NEW.recorded_at < v_charge_recorded_at THEN
            RAISE EXCEPTION 'Allocation recorded_at (%) cannot precede charge transaction recorded_at (%).',
                NEW.recorded_at, v_charge_recorded_at;
        END IF;

        IF NEW.recorded_at < v_target_recorded_at THEN
            RAISE EXCEPTION 'Allocation recorded_at (%) cannot precede target transaction recorded_at (%).',
                NEW.recorded_at, v_target_recorded_at;
        END IF;

        -- 7. Per-charge monotonic history check
        SELECT MAX(ae.recorded_at)
        INTO v_max_prior_recorded_at
        FROM public.portfolio_fee_tax_attribution_events ae
        WHERE ae.portfolio_id = NEW.portfolio_id
          AND ae.account_id = NEW.account_id
          AND (
              ae.charge_transaction_id = v_charge_id
              OR ae.reverses_attribution_event_id IN (
                  SELECT alloc.id
                  FROM public.portfolio_fee_tax_attribution_events alloc
                  WHERE alloc.portfolio_id = NEW.portfolio_id
                    AND alloc.account_id = NEW.account_id
                    AND alloc.charge_transaction_id = v_charge_id
              )
          );

        IF v_max_prior_recorded_at IS NOT NULL AND NEW.recorded_at < v_max_prior_recorded_at THEN
            RAISE EXCEPTION 'Backdated attribution event rejected: recorded_at (%) is earlier than latest existing recorded_at (%) for charge %.',
                NEW.recorded_at, v_max_prior_recorded_at, v_charge_id;
        END IF;

    ELSIF NEW.event_type = 'reversal' THEN
        -- 1. Fetch referenced attribution event (resolving both charge and target IDs)
        SELECT event_type, charge_transaction_id, target_transaction_id, recorded_at
        INTO v_reversed_event_type, v_charge_id, v_target_id, v_ref_allocation_recorded_at
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

        -- Precheck for already-reversed event
        IF EXISTS (
            SELECT 1
            FROM public.portfolio_fee_tax_attribution_events rev
            WHERE rev.portfolio_id = NEW.portfolio_id
              AND rev.account_id = NEW.account_id
              AND rev.event_type = 'reversal'
              AND rev.reverses_attribution_event_id = NEW.reverses_attribution_event_id
        ) THEN
            RAISE EXCEPTION 'Referenced attribution event % is already reversed.',
                NEW.reverses_attribution_event_id;
        END IF;

        -- 2. Lock charge ledger transaction (Lock #1 in deterministic order)
        SELECT transaction_type, cash_amount, recorded_at
        INTO v_charge_type, v_charge_cash_amount, v_charge_recorded_at
        FROM public.portfolio_transactions
        WHERE id = v_charge_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id
        FOR UPDATE;

        IF v_charge_type IS NULL THEN
            RAISE EXCEPTION 'Charge transaction % for referenced allocation % not found in portfolio % account %.',
                v_charge_id, NEW.reverses_attribution_event_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        -- 3. Lock target ledger transaction (Lock #2 in deterministic order)
        SELECT transaction_type, recorded_at
        INTO v_target_type, v_target_recorded_at
        FROM public.portfolio_transactions
        WHERE id = v_target_id
          AND portfolio_id = NEW.portfolio_id
          AND account_id = NEW.account_id
        FOR UPDATE;

        IF v_target_type IS NULL THEN
            RAISE EXCEPTION 'Target transaction % for referenced allocation % not found in portfolio % account %.',
                v_target_id, NEW.reverses_attribution_event_id, NEW.portfolio_id, NEW.account_id;
        END IF;

        -- 4. Knowledge-time causality for reversal
        IF NEW.recorded_at < v_ref_allocation_recorded_at THEN
            RAISE EXCEPTION 'Reversal recorded_at (%) cannot precede referenced allocation recorded_at (%).',
                NEW.recorded_at, v_ref_allocation_recorded_at;
        END IF;

        -- 5. Per-charge monotonic history check
        SELECT MAX(ae.recorded_at)
        INTO v_max_prior_recorded_at
        FROM public.portfolio_fee_tax_attribution_events ae
        WHERE ae.portfolio_id = NEW.portfolio_id
          AND ae.account_id = NEW.account_id
          AND (
              ae.charge_transaction_id = v_charge_id
              OR ae.reverses_attribution_event_id IN (
                  SELECT alloc.id
                  FROM public.portfolio_fee_tax_attribution_events alloc
                  WHERE alloc.portfolio_id = NEW.portfolio_id
                    AND alloc.account_id = NEW.account_id
                    AND alloc.charge_transaction_id = v_charge_id
              )
          );

        IF v_max_prior_recorded_at IS NOT NULL AND NEW.recorded_at < v_max_prior_recorded_at THEN
            RAISE EXCEPTION 'Backdated reversal event rejected: recorded_at (%) is earlier than latest existing recorded_at (%) for charge %.',
                NEW.recorded_at, v_max_prior_recorded_at, v_charge_id;
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
