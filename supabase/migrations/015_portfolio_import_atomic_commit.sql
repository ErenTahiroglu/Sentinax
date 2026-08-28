-- 015_portfolio_import_atomic_commit.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 13Q: Atomic Import Claim + Ledger Transaction Commit RPC
--
-- Core Invariants Enforced at DB Level:
--   1. Atomic All-or-Nothing Commit:
--      - Single PostgreSQL function inserting BOTH portfolio_transactions and portfolio_import_claim_bindings.
--      - No split-brain state (orphan transaction or orphan claim).
--   2. Strict Payload Contract & Validation:
--      - Exact key sets for p_transaction (24 canonical serializer keys) and p_binding (9 serializer keys).
--      - Cross-payload identity equality (owner_id, portfolio_id, account_id, transaction_id).
--      - Explicit rejection of external_source / external_reference (import claims are NOT ledger external identity).
--      - Explicit rejection of cash_bucket_id (must be NULL in Phase 13Q).
--      - Explicit rejection of reversal transactions or reverses_transaction_id.
--      - Explicit rejection of non-null notes.
--      - Canonical 64-char lowercase hex economic_fingerprint format validation.
--      - Timezone-aware ISO timestamp string validation for recorded_at and executed_at.
--   3. Idempotent Replay vs. Conflict Behavior:
--      - Pre-check on (owner_id, portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256).
--      - Same claim + same plan SHA + same economic fingerprint => 'idempotent_duplicate' (no INSERT).
--      - Same claim + differing plan SHA or differing economic fingerprint => 'conflict' (no INSERT).
--   4. Race-Safe Subtransaction Handling (SQLSTATE 23505):
--      - If concurrent insert causes uniqueness violation, re-reads authoritative claim.
--      - Re-raises original error if 23505 is unexplained by matching claim.
--   5. Security & Isolation:
--      - SECURITY INVOKER, VOLATILE, explicit search_path = public, pg_temp.
--      - REVOKE EXECUTE FROM PUBLIC; GRANT EXECUTE TO authenticated, service_role.

CREATE OR REPLACE FUNCTION public.commit_portfolio_import_claim(
    p_transaction JSONB,
    p_binding JSONB
)
RETURNS TABLE (
    commit_status TEXT,
    transaction_id UUID,
    diagnostic TEXT
)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tx_keys TEXT[];
    v_binding_keys TEXT[];
    v_expected_tx_keys CONSTANT TEXT[] := ARRAY[
        'account_id', 'cash_amount', 'cash_bucket_id', 'cash_currency',
        'economic_fingerprint', 'effective_date', 'executed_at', 'external_reference',
        'external_source', 'from_amount', 'from_currency', 'id', 'instrument_id',
        'notes', 'owner_id', 'portfolio_id', 'quantity', 'recorded_at',
        'reverses_transaction_id', 'to_amount', 'to_currency', 'trade_currency',
        'transaction_type', 'unit_price'
    ];
    v_expected_binding_keys CONSTANT TEXT[] := ARRAY[
        'account_id', 'expected_plan_sha256', 'file_content_sha256', 'owner_id',
        'portfolio_id', 'record_ordinal', 'record_sha256', 'source_key', 'transaction_id'
    ];

    v_tx_owner_id UUID;
    v_tx_portfolio_id UUID;
    v_tx_account_id UUID;
    v_tx_id UUID;

    v_binding_owner_id UUID;
    v_binding_portfolio_id UUID;
    v_binding_account_id UUID;
    v_binding_tx_id UUID;
    v_binding_source_key TEXT;
    v_binding_file_sha TEXT;
    v_binding_ordinal BIGINT;
    v_binding_rec_sha TEXT;
    v_binding_plan_sha TEXT;

    v_existing_plan_sha TEXT;
    v_existing_tx_id UUID;
    v_existing_tx_fp TEXT;

    v_tx_recorded_at_raw TEXT;
    v_tx_executed_at_raw TEXT;
    v_tx_fp TEXT;
BEGIN
    -- ─────────────────────────────────────────────────────────────────────────
    -- 1. Input Shape & Key Validation (Fail-Closed on extra or missing keys)
    -- ─────────────────────────────────────────────────────────────────────────
    IF p_transaction IS NULL OR jsonb_typeof(p_transaction) <> 'object' THEN
        RAISE EXCEPTION 'p_transaction must be a non-null JSON object.';
    END IF;

    IF p_binding IS NULL OR jsonb_typeof(p_binding) <> 'object' THEN
        RAISE EXCEPTION 'p_binding must be a non-null JSON object.';
    END IF;

    SELECT ARRAY_AGG(k ORDER BY k) INTO v_tx_keys
    FROM jsonb_object_keys(p_transaction) AS k;

    IF v_tx_keys IS DISTINCT FROM v_expected_tx_keys THEN
        RAISE EXCEPTION 'p_transaction payload keys do not match exact canonical schema.';
    END IF;

    SELECT ARRAY_AGG(k ORDER BY k) INTO v_binding_keys
    FROM jsonb_object_keys(p_binding) AS k;

    IF v_binding_keys IS DISTINCT FROM v_expected_binding_keys THEN
        RAISE EXCEPTION 'p_binding payload keys do not match exact canonical schema.';
    END IF;

    -- ─────────────────────────────────────────────────────────────────────────
    -- 2. Extract & Cross-Validate Identities
    -- ─────────────────────────────────────────────────────────────────────────
    v_tx_owner_id := (p_transaction->>'owner_id')::UUID;
    v_tx_portfolio_id := (p_transaction->>'portfolio_id')::UUID;
    v_tx_account_id := (p_transaction->>'account_id')::UUID;
    v_tx_id := (p_transaction->>'id')::UUID;

    v_binding_owner_id := (p_binding->>'owner_id')::UUID;
    v_binding_portfolio_id := (p_binding->>'portfolio_id')::UUID;
    v_binding_account_id := (p_binding->>'account_id')::UUID;
    v_binding_tx_id := (p_binding->>'transaction_id')::UUID;

    -- Explicit non-null validation on all eight required identity fields
    IF v_tx_owner_id IS NULL
       OR v_tx_portfolio_id IS NULL
       OR v_tx_account_id IS NULL
       OR v_tx_id IS NULL
       OR v_binding_owner_id IS NULL
       OR v_binding_portfolio_id IS NULL
       OR v_binding_account_id IS NULL
       OR v_binding_tx_id IS NULL THEN
        RAISE EXCEPTION 'Transaction and binding identity fields must be non-null.';
    END IF;

    -- NULL-safe identity cross-matching
    IF v_tx_owner_id IS DISTINCT FROM v_binding_owner_id
       OR v_tx_portfolio_id IS DISTINCT FROM v_binding_portfolio_id
       OR v_tx_account_id IS DISTINCT FROM v_binding_account_id
       OR v_tx_id IS DISTINCT FROM v_binding_tx_id THEN
        RAISE EXCEPTION 'Cross-payload identity mismatch between transaction and binding.';
    END IF;

    -- Extract binding claim fields and validate non-null and domain grammar before precheck
    v_binding_source_key := p_binding->>'source_key';
    v_binding_file_sha := p_binding->>'file_content_sha256';
    v_binding_rec_sha := p_binding->>'record_sha256';
    v_binding_plan_sha := p_binding->>'expected_plan_sha256';

    IF v_binding_source_key IS NULL
       OR v_binding_file_sha IS NULL
       OR p_binding->>'record_ordinal' IS NULL
       OR v_binding_rec_sha IS NULL
       OR v_binding_plan_sha IS NULL THEN
        RAISE EXCEPTION 'Binding claim fields must be non-null.';
    END IF;

    v_binding_ordinal := (p_binding->>'record_ordinal')::BIGINT;

    IF v_binding_source_key !~ '^[a-z0-9][a-z0-9._-]{0,63}$' THEN
        RAISE EXCEPTION 'Invalid source_key format in binding payload.';
    END IF;

    IF v_binding_file_sha !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Invalid file_content_sha256 format in binding payload.';
    END IF;

    IF v_binding_ordinal < 1 THEN
        RAISE EXCEPTION 'record_ordinal must be a positive integer (>= 1).';
    END IF;

    IF v_binding_rec_sha !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Invalid record_sha256 format in binding payload.';
    END IF;

    IF v_binding_plan_sha !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Invalid expected_plan_sha256 format in binding payload.';
    END IF;

    -- ─────────────────────────────────────────────────────────────────────────
    -- 3. Invariant Checks on Transaction Payload
    -- ─────────────────────────────────────────────────────────────────────────
    IF p_transaction->>'external_source' IS NOT NULL OR p_transaction->>'external_reference' IS NOT NULL THEN
        RAISE EXCEPTION 'Import transactions must have null external_source and external_reference.';
    END IF;

    IF p_transaction->>'cash_bucket_id' IS NOT NULL THEN
        RAISE EXCEPTION 'Import transactions must have null cash_bucket_id.';
    END IF;

    IF p_transaction->>'reverses_transaction_id' IS NOT NULL OR p_transaction->>'transaction_type' = 'reversal' THEN
        RAISE EXCEPTION 'Import transactions cannot be reversals.';
    END IF;

    IF p_transaction->>'notes' IS NOT NULL THEN
        RAISE EXCEPTION 'Import transactions must have null notes.';
    END IF;

    v_tx_fp := p_transaction->>'economic_fingerprint';
    IF v_tx_fp IS NULL OR v_tx_fp !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Invalid economic_fingerprint in transaction payload.';
    END IF;

    -- Timestamp timezone-safety check
    v_tx_recorded_at_raw := p_transaction->>'recorded_at';
    IF v_tx_recorded_at_raw IS NULL OR v_tx_recorded_at_raw !~ '(Z|[+-]\d{2}(:\d{2})?)$' THEN
        RAISE EXCEPTION 'Transaction recorded_at must be an explicit timezone-aware timestamp string.';
    END IF;

    v_tx_executed_at_raw := p_transaction->>'executed_at';
    IF v_tx_executed_at_raw IS NOT NULL AND v_tx_executed_at_raw !~ '(Z|[+-]\d{2}(:\d{2})?)$' THEN
        RAISE EXCEPTION 'Transaction executed_at must be an explicit timezone-aware timestamp string.';
    END IF;

    -- ─────────────────────────────────────────────────────────────────────────
    -- 4. Authoritative Claim Pre-Check (Idempotent replay vs. Conflict)
    -- ─────────────────────────────────────────────────────────────────────────
    SELECT b.expected_plan_sha256, b.transaction_id, pt.economic_fingerprint
    INTO v_existing_plan_sha, v_existing_tx_id, v_existing_tx_fp
    FROM public.portfolio_import_claim_bindings b
    JOIN public.portfolio_transactions pt
      ON pt.id = b.transaction_id AND pt.portfolio_id = b.portfolio_id AND pt.account_id = b.account_id
    WHERE b.owner_id = v_binding_owner_id
      AND b.portfolio_id = v_binding_portfolio_id
      AND b.account_id = v_binding_account_id
      AND b.source_key = v_binding_source_key
      AND b.file_content_sha256 = v_binding_file_sha
      AND b.record_ordinal = v_binding_ordinal
      AND b.record_sha256 = v_binding_rec_sha;

    IF FOUND THEN
        IF v_existing_plan_sha = v_binding_plan_sha AND v_existing_tx_fp = v_tx_fp THEN
            RETURN QUERY SELECT
                'idempotent_duplicate'::TEXT,
                v_existing_tx_id,
                'Existing claim matches plan SHA and economic fingerprint.'::TEXT;
            RETURN;
        ELSE
            RETURN QUERY SELECT
                'conflict'::TEXT,
                v_existing_tx_id,
                'Existing claim has different plan SHA or economic fingerprint.'::TEXT;
            RETURN;
        END IF;
    END IF;

    -- ─────────────────────────────────────────────────────────────────────────
    -- 5. New Claim Atomic Insert Path (with race-safe 23505 resolution)
    -- ─────────────────────────────────────────────────────────────────────────
    BEGIN
        -- Insert canonical transaction
        INSERT INTO public.portfolio_transactions (
            id,
            portfolio_id,
            account_id,
            owner_id,
            transaction_type,
            effective_date,
            executed_at,
            recorded_at,
            instrument_id,
            quantity,
            unit_price,
            trade_currency,
            cash_amount,
            cash_currency,
            cash_bucket_id,
            from_currency,
            from_amount,
            to_currency,
            to_amount,
            external_source,
            external_reference,
            reverses_transaction_id,
            notes,
            economic_fingerprint
        ) VALUES (
            v_tx_id,
            v_tx_portfolio_id,
            v_tx_account_id,
            v_tx_owner_id,
            p_transaction->>'transaction_type',
            (p_transaction->>'effective_date')::DATE,
            (p_transaction->>'executed_at')::TIMESTAMPTZ,
            (p_transaction->>'recorded_at')::TIMESTAMPTZ,
            (p_transaction->>'instrument_id')::UUID,
            (p_transaction->>'quantity')::NUMERIC,
            (p_transaction->>'unit_price')::NUMERIC,
            p_transaction->>'trade_currency',
            (p_transaction->>'cash_amount')::NUMERIC,
            p_transaction->>'cash_currency',
            NULL,
            p_transaction->>'from_currency',
            (p_transaction->>'from_amount')::NUMERIC,
            p_transaction->>'to_currency',
            (p_transaction->>'to_amount')::NUMERIC,
            NULL,
            NULL,
            NULL,
            NULL,
            v_tx_fp
        );

        -- Insert claim binding referencing the newly inserted transaction
        INSERT INTO public.portfolio_import_claim_bindings (
            owner_id,
            portfolio_id,
            account_id,
            source_key,
            file_content_sha256,
            record_ordinal,
            record_sha256,
            expected_plan_sha256,
            transaction_id
        ) VALUES (
            v_binding_owner_id,
            v_binding_portfolio_id,
            v_binding_account_id,
            v_binding_source_key,
            v_binding_file_sha,
            v_binding_ordinal,
            v_binding_rec_sha,
            v_binding_plan_sha,
            v_binding_tx_id
        );

        RETURN QUERY SELECT
            'appended'::TEXT,
            v_tx_id,
            'Transaction and claim binding appended successfully.'::TEXT;
        RETURN;

    EXCEPTION WHEN unique_violation THEN
        -- Re-read authoritative claim in case a concurrent call won the race
        SELECT b.expected_plan_sha256, b.transaction_id, pt.economic_fingerprint
        INTO v_existing_plan_sha, v_existing_tx_id, v_existing_tx_fp
        FROM public.portfolio_import_claim_bindings b
        JOIN public.portfolio_transactions pt
          ON pt.id = b.transaction_id AND pt.portfolio_id = b.portfolio_id AND pt.account_id = b.account_id
        WHERE b.owner_id = v_binding_owner_id
          AND b.portfolio_id = v_binding_portfolio_id
          AND b.account_id = v_binding_account_id
          AND b.source_key = v_binding_source_key
          AND b.file_content_sha256 = v_binding_file_sha
          AND b.record_ordinal = v_binding_ordinal
          AND b.record_sha256 = v_binding_rec_sha;

        IF FOUND THEN
            IF v_existing_plan_sha = v_binding_plan_sha AND v_existing_tx_fp = v_tx_fp THEN
                RETURN QUERY SELECT
                    'idempotent_duplicate'::TEXT,
                    v_existing_tx_id,
                    'Concurrently bound claim matches plan SHA and economic fingerprint.'::TEXT;
                RETURN;
            ELSE
                RETURN QUERY SELECT
                    'conflict'::TEXT,
                    v_existing_tx_id,
                    'Concurrently bound claim has different plan SHA or economic fingerprint.'::TEXT;
                RETURN;
            END IF;
        ELSE
            -- Unexplained 23505 (e.g. transaction UUID collision) -> re-raise original exception
            RAISE;
        END IF;
    END;
END;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Permissions & Security
-- ─────────────────────────────────────────────────────────────────────────────
REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim(JSONB, JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.commit_portfolio_import_claim(JSONB, JSONB) TO authenticated, service_role;
