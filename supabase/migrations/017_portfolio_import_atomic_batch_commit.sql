-- 017_portfolio_import_atomic_batch_commit.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 13R: File-Level Atomic Binding-Batch Commit & All-or-Nothing Import Execution
--
-- Security & Operational Invariants:
--   1. File-Level Transactional Atomicity:
--      - Commits a complete ImportLedgerBindingBatch as a single all-or-nothing unit.
--      - If any item encounters a conflict or database error, all candidate transaction
--        and claim inserts created by earlier items in the batch are completely rolled back.
--   2. Delegation to Closed Single-Intent RPC:
--      - Delegates every individual item to public.commit_portfolio_import_claim(p_transaction, p_binding).
--      - Preserves single authoritative SQL boundary for payload shape, database constraints,
--        identity null safety, claim-PK race handling, and immutability.
--   3. Dedicated Conflict Rollback Signal (SQLSTATE P13R1):
--      - The single-intent RPC returns 'conflict' rather than raising.
--      - The batch function converts per-item conflict into internal exception (SQLSTATE P13R1)
--        to trigger outer subtransaction rollback of all earlier writes in the batch.
--      - The outer handler catches ONLY SQLSTATE P13R1 and returns batch_status = 'conflict'.
--      - Generic database errors are NOT swallowed and abort the transaction.
--   4. Trust Boundary & Exclusivity:
--      - Backend persistence primitive: SECURITY INVOKER, VOLATILE, search_path = public, pg_temp.
--      - Executable strictly by service_role (revoked from PUBLIC and authenticated).

CREATE OR REPLACE FUNCTION public.commit_portfolio_import_claim_batch(
    p_items JSONB
)
RETURNS TABLE (
    batch_status TEXT,
    transaction_ids UUID[],
    item_statuses TEXT[],
    conflict_record_ordinal BIGINT,
    conflict_transaction_id UUID,
    diagnostic TEXT
)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_item JSONB;
    v_item_keys TEXT[];
    v_expected_item_keys CONSTANT TEXT[] := ARRAY['binding', 'transaction'];

    v_tx_json JSONB;
    v_b_json JSONB;

    v_single_status TEXT;
    v_single_tx_id UUID;
    v_single_diag TEXT;

    v_tx_ids UUID[] := ARRAY[]::UUID[];
    v_item_statuses TEXT[] := ARRAY[]::TEXT[];
    v_has_appended BOOLEAN := FALSE;

    v_conflict_ordinal BIGINT;
    v_conflict_tx_id UUID;
    v_conflict_diagnostic TEXT;
BEGIN
    -- ─────────────────────────────────────────────────────────────────────────
    -- 1. Input Validation
    -- ─────────────────────────────────────────────────────────────────────────
    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' THEN
        RAISE EXCEPTION 'p_items must be a non-null JSON array.';
    END IF;

    IF jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'p_items array cannot be empty.';
    END IF;

    -- ─────────────────────────────────────────────────────────────────────────
    -- 2. Atomic Batch Loop with Dedicated Conflict Rollback
    -- ─────────────────────────────────────────────────────────────────────────
    BEGIN
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_items)
        LOOP
            IF v_item IS NULL OR jsonb_typeof(v_item) <> 'object' THEN
                RAISE EXCEPTION 'Each element in p_items must be a non-null JSON object.';
            END IF;

            SELECT ARRAY_AGG(k ORDER BY k) INTO v_item_keys
            FROM jsonb_object_keys(v_item) AS k;

            IF v_item_keys IS DISTINCT FROM v_expected_item_keys THEN
                RAISE EXCEPTION 'Item payload keys must be exactly ["binding", "transaction"].';
            END IF;

            v_tx_json := v_item->'transaction';
            v_b_json := v_item->'binding';

            IF v_tx_json IS NULL OR jsonb_typeof(v_tx_json) <> 'object' THEN
                RAISE EXCEPTION 'Item transaction must be a non-null JSON object.';
            END IF;

            IF v_b_json IS NULL OR jsonb_typeof(v_b_json) <> 'object' THEN
                RAISE EXCEPTION 'Item binding must be a non-null JSON object.';
            END IF;

            -- Execute closed single-intent RPC
            SELECT commit_status, transaction_id, diagnostic
            INTO STRICT v_single_status, v_single_tx_id, v_single_diag
            FROM public.commit_portfolio_import_claim(v_tx_json, v_b_json);

            IF v_single_status = 'appended' THEN
                v_has_appended := TRUE;
                v_tx_ids := array_append(v_tx_ids, v_single_tx_id);
                v_item_statuses := array_append(v_item_statuses, 'appended');

            ELSIF v_single_status = 'idempotent_duplicate' THEN
                v_tx_ids := array_append(v_tx_ids, v_single_tx_id);
                v_item_statuses := array_append(v_item_statuses, 'idempotent_duplicate');

            ELSIF v_single_status = 'conflict' THEN
                v_conflict_ordinal := (v_b_json->>'record_ordinal')::BIGINT;
                v_conflict_tx_id := v_single_tx_id;
                v_conflict_diagnostic := COALESCE(v_single_diag, 'Import batch conflict on record ordinal ' || v_conflict_ordinal);

                -- Trigger subtransaction rollback of all earlier batch writes via custom SQLSTATE
                RAISE EXCEPTION USING
                    ERRCODE = 'P13R1',
                    MESSAGE = 'Import batch item conflict on record ordinal ' || v_conflict_ordinal;

            ELSE
                RAISE EXCEPTION 'Unexpected single-intent commit status: %', v_single_status;
            END IF;
        END LOOP;

        -- Batch loop completed without conflict or exception
        IF v_has_appended THEN
            RETURN QUERY SELECT
                'appended'::TEXT,
                v_tx_ids,
                v_item_statuses,
                NULL::BIGINT,
                NULL::UUID,
                'Batch committed successfully with new transactions.'::TEXT;
            RETURN;
        ELSE
            RETURN QUERY SELECT
                'idempotent_duplicate'::TEXT,
                v_tx_ids,
                v_item_statuses,
                NULL::BIGINT,
                NULL::UUID,
                'Entire batch matches existing claims and economics.'::TEXT;
            RETURN;
        END IF;

    EXCEPTION WHEN SQLSTATE 'P13R1' THEN
        -- Catch strictly the dedicated conflict exception.
        -- Outer subtransaction rollback has automatically discarded all earlier candidate writes.
        RETURN QUERY SELECT
            'conflict'::TEXT,
            ARRAY[]::UUID[],
            ARRAY[]::TEXT[],
            v_conflict_ordinal,
            v_conflict_tx_id,
            v_conflict_diagnostic;
        RETURN;
    END;
END;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Permissions & Security
-- ─────────────────────────────────────────────────────────────────────────────
REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim_batch(JSONB) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim_batch(JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.commit_portfolio_import_claim_batch(JSONB) TO service_role;
