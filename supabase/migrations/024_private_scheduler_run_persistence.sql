-- ============================================================================
-- Migration 024: Private scheduler run persistence authority (Phase 24D1)
-- ============================================================================
-- Persists ONE logical scheduler run: its immutable admission / trigger authority, its current lifecycle snapshot and its complete,
-- append-only transition history, with database primitives for idempotent initialization and an atomic optimistic state_version CAS.
--
-- Mirrors the CLOSED Python authorities (Phase 24A trigger, 24C2A admission, 24C2B lifecycle). Python remains the semantic domain authority;
-- PostgreSQL adds durable uniqueness, transactional atomicity, optimistic CAS, immutable history and defense-in-depth structural constraints.
--
--   * Logical identity: run_idempotency_sha256 (the canonical occurrence trigger hash) is the PRIMARY KEY. No random row identity exists.
--   * admission_payload is the immutable audit provenance of the 24C2A admission (the canonical JSON codec is Phase 24D2).
--   * transition_at is persisted per history row because the pure 24C2B transition record cannot hold the renewal instant:
--       claim -> claimed_at, renew_claim -> renewed_at, take_over_expired_claim -> new claimed_at, succeed / fail -> terminal_at,
--       initialize -> NULL.
--   * Only created_at, updated_at and recorded_at use the database clock; they are persistence metadata, never logical identity or any
--     domain instant (claim, lease, terminal, scheduled or cause time are always explicit inputs).
--   * The write surface is service_role only (RLS enabled, no policies, PUBLIC / anon / authenticated revoked). service_role permission is
--     not semantic permission to fabricate a lifecycle: Phase 24D2 will expose only canonical Python serialization / repository methods.
--   * No retry, result payload, queue, worker, delete path or generated identity exists here (FAILED is terminal).
--
-- Exact semantics of the transition RPC (all instants explicit, no tolerance):
--   claim                    ready only; p_transition_at >= not-before (scheduled_for | cause_available_at); p_transition_at < p_lease_expires_at
--   renew_claim              claimed only; same claim key; claimed_at <= p_transition_at < lease_expires_at; strictly later new expiry
--   take_over_expired_claim  claimed only; different claim key; p_transition_at >= current lease_expires_at; new expiry > p_transition_at
--   succeed / fail           claimed only; same claim key; claimed_at <= p_transition_at < lease_expires_at; no lease parameter
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Current run table
-- ----------------------------------------------------------------------------
CREATE TABLE public.private_scheduler_runs (
    run_idempotency_sha256 VARCHAR(64) NOT NULL,
    admission_source VARCHAR(32) NOT NULL,

    trigger_kind VARCHAR(16) NOT NULL,
    work_kind VARCHAR(32) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    owner_id UUID,
    portfolio_id UUID,

    scheduled_for TIMESTAMPTZ,
    event_cause_kind VARCHAR(32),
    cause_key TEXT,
    cause_available_at TIMESTAMPTZ,

    policy_key VARCHAR(128) NOT NULL,
    policy_revision BIGINT NOT NULL,

    admission_payload JSONB NOT NULL,

    state VARCHAR(16) NOT NULL,
    state_version BIGINT NOT NULL,

    claim_key TEXT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    terminal_at TIMESTAMPTZ,
    failure_code VARCHAR(128),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_private_scheduler_runs PRIMARY KEY (run_idempotency_sha256),
    CONSTRAINT ck_psr_run_hash CHECK (run_idempotency_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_psr_admission_source CHECK (admission_source IN ('scheduled_direct', 'scheduled_calendar_applicable', 'event_driven')),
    CONSTRAINT ck_psr_trigger_kind CHECK (trigger_kind IN ('scheduled', 'event_driven')),
    CONSTRAINT ck_psr_source_kind_matrix CHECK (
        (admission_source IN ('scheduled_direct', 'scheduled_calendar_applicable') AND trigger_kind = 'scheduled')
        OR (admission_source = 'event_driven' AND trigger_kind = 'event_driven')
    ),
    CONSTRAINT ck_psr_work_kind CHECK (work_kind IN ('source_data_refresh', 'portfolio_analysis_refresh', 'portfolio_health_check', 'game_changer_review')),
    CONSTRAINT ck_psr_scope CHECK (scope IN ('system', 'portfolio')),
    CONSTRAINT ck_psr_scope_owner CHECK (
        (scope = 'system' AND owner_id IS NULL AND portfolio_id IS NULL)
        OR (scope = 'portfolio' AND owner_id IS NOT NULL AND portfolio_id IS NOT NULL)
    ),
    CONSTRAINT ck_psr_work_scope CHECK (
        (work_kind = 'source_data_refresh' AND scope = 'system')
        OR (work_kind <> 'source_data_refresh' AND scope = 'portfolio')
    ),
    CONSTRAINT ck_psr_trigger_shape CHECK (
        (trigger_kind = 'scheduled'
            AND scheduled_for IS NOT NULL
            AND event_cause_kind IS NULL AND cause_key IS NULL AND cause_available_at IS NULL)
        OR (trigger_kind = 'event_driven'
            AND scheduled_for IS NULL
            AND event_cause_kind IS NOT NULL AND cause_key IS NOT NULL AND cause_available_at IS NOT NULL)
    ),
    CONSTRAINT ck_psr_event_cause_kind CHECK (
        event_cause_kind IS NULL OR event_cause_kind IN (
            'disclosure_ingested', 'macro_release_ingested', 'policy_configuration_changed', 'portfolio_changed',
            'new_cash_confirmed', 'user_view_changed', 'risk_limit_breach'
        )
    ),
    CONSTRAINT ck_psr_cause_key CHECK (
        cause_key IS NULL OR (
            char_length(cause_key) BETWEEN 1 AND 128
            AND cause_key !~ '(^\s)|(\s$)'
            AND cause_key !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT ck_psr_policy_key CHECK (policy_key ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),
    CONSTRAINT ck_psr_policy_revision CHECK (policy_revision >= 1),
    CONSTRAINT ck_psr_admission_payload CHECK (jsonb_typeof(admission_payload) = 'object'),
    CONSTRAINT ck_psr_state CHECK (state IN ('ready', 'claimed', 'succeeded', 'failed')),
    CONSTRAINT ck_psr_claim_key CHECK (
        claim_key IS NULL OR (
            char_length(claim_key) BETWEEN 1 AND 128
            AND claim_key !~ '(^\s)|(\s$)'
            AND claim_key !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT ck_psr_failure_code CHECK (failure_code IS NULL OR failure_code ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),
    CONSTRAINT ck_psr_lifecycle_shape CHECK (
        (state = 'ready'
            AND state_version = 1
            AND claim_key IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL
            AND terminal_at IS NULL AND failure_code IS NULL)
        OR (state = 'claimed'
            AND state_version >= 2
            AND claim_key IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
            AND claimed_at < lease_expires_at
            AND terminal_at IS NULL AND failure_code IS NULL)
        OR (state = 'succeeded'
            AND state_version >= 3
            AND claim_key IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
            AND terminal_at IS NOT NULL AND failure_code IS NULL
            AND claimed_at <= terminal_at AND terminal_at < lease_expires_at)
        OR (state = 'failed'
            AND state_version >= 3
            AND claim_key IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
            AND terminal_at IS NOT NULL AND failure_code IS NOT NULL
            AND claimed_at <= terminal_at AND terminal_at < lease_expires_at)
    )
);

-- ----------------------------------------------------------------------------
-- 2. Append-only transition history
-- ----------------------------------------------------------------------------
CREATE TABLE public.private_scheduler_run_transitions (
    run_idempotency_sha256 VARCHAR(64) NOT NULL,
    after_state_version BIGINT NOT NULL,

    transition_kind VARCHAR(32) NOT NULL,
    before_state_version BIGINT,
    transition_at TIMESTAMPTZ,

    after_state VARCHAR(16) NOT NULL,
    claim_key TEXT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    terminal_at TIMESTAMPTZ,
    failure_code VARCHAR(128),

    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_private_scheduler_run_transitions PRIMARY KEY (run_idempotency_sha256, after_state_version),
    CONSTRAINT fk_psrt_run FOREIGN KEY (run_idempotency_sha256)
        REFERENCES public.private_scheduler_runs (run_idempotency_sha256) ON DELETE RESTRICT,
    CONSTRAINT ck_psrt_transition_kind CHECK (
        transition_kind IN ('initialize', 'claim', 'renew_claim', 'take_over_expired_claim', 'succeed', 'fail')
    ),
    CONSTRAINT ck_psrt_after_state CHECK (after_state IN ('ready', 'claimed', 'succeeded', 'failed')),
    CONSTRAINT ck_psrt_initialize_shape CHECK (
        transition_kind <> 'initialize' OR (
            before_state_version IS NULL
            AND after_state_version = 1
            AND transition_at IS NULL
            AND after_state = 'ready'
            AND claim_key IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL
            AND terminal_at IS NULL AND failure_code IS NULL
        )
    ),
    CONSTRAINT ck_psrt_version_step CHECK (
        transition_kind = 'initialize' OR (
            before_state_version IS NOT NULL
            AND after_state_version = before_state_version + 1
            AND transition_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_psrt_claim_key CHECK (
        claim_key IS NULL OR (
            char_length(claim_key) BETWEEN 1 AND 128
            AND claim_key !~ '(^\s)|(\s$)'
            AND claim_key !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT ck_psrt_failure_code CHECK (failure_code IS NULL OR failure_code ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),
    CONSTRAINT ck_psrt_after_snapshot_shape CHECK (
        (after_state = 'ready'
            AND after_state_version = 1
            AND claim_key IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL
            AND terminal_at IS NULL AND failure_code IS NULL)
        OR (after_state = 'claimed'
            AND after_state_version >= 2
            AND claim_key IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
            AND claimed_at < lease_expires_at
            AND terminal_at IS NULL AND failure_code IS NULL)
        OR (after_state = 'succeeded'
            AND after_state_version >= 3
            AND claim_key IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
            AND terminal_at IS NOT NULL AND failure_code IS NULL
            AND claimed_at <= terminal_at AND terminal_at < lease_expires_at)
        OR (after_state = 'failed'
            AND after_state_version >= 3
            AND claim_key IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
            AND terminal_at IS NOT NULL AND failure_code IS NOT NULL
            AND claimed_at <= terminal_at AND terminal_at < lease_expires_at)
    )
);

-- ----------------------------------------------------------------------------
-- 3. Guards: immutable admission identity, terminal finality, exact version step, append-only history
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.private_scheduler_runs_guard_update()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    IF OLD.state IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'private_scheduler_runs: terminal runs are final and cannot be updated.';
    END IF;
    IF NEW.run_idempotency_sha256 IS DISTINCT FROM OLD.run_idempotency_sha256
       OR NEW.admission_source IS DISTINCT FROM OLD.admission_source
       OR NEW.trigger_kind IS DISTINCT FROM OLD.trigger_kind
       OR NEW.work_kind IS DISTINCT FROM OLD.work_kind
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.portfolio_id IS DISTINCT FROM OLD.portfolio_id
       OR NEW.scheduled_for IS DISTINCT FROM OLD.scheduled_for
       OR NEW.event_cause_kind IS DISTINCT FROM OLD.event_cause_kind
       OR NEW.cause_key IS DISTINCT FROM OLD.cause_key
       OR NEW.cause_available_at IS DISTINCT FROM OLD.cause_available_at
       OR NEW.policy_key IS DISTINCT FROM OLD.policy_key
       OR NEW.policy_revision IS DISTINCT FROM OLD.policy_revision
       OR NEW.admission_payload IS DISTINCT FROM OLD.admission_payload
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'private_scheduler_runs: the admission identity is immutable.';
    END IF;
    IF NEW.state_version <> OLD.state_version + 1 THEN
        RAISE EXCEPTION 'private_scheduler_runs: state_version must advance by exactly one.';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_private_scheduler_runs_guard_update
    BEFORE UPDATE ON public.private_scheduler_runs
    FOR EACH ROW EXECUTE FUNCTION public.private_scheduler_runs_guard_update();

CREATE OR REPLACE FUNCTION public.private_scheduler_runs_guard_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'private_scheduler_runs: runs are durable and cannot be deleted.';
END;
$$;

CREATE TRIGGER trg_private_scheduler_runs_guard_delete
    BEFORE DELETE ON public.private_scheduler_runs
    FOR EACH ROW EXECUTE FUNCTION public.private_scheduler_runs_guard_delete();

CREATE OR REPLACE FUNCTION public.private_scheduler_run_transitions_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'private_scheduler_run_transitions is append-only.';
END;
$$;

CREATE TRIGGER trg_private_scheduler_run_transitions_append_only
    BEFORE UPDATE OR DELETE ON public.private_scheduler_run_transitions
    FOR EACH ROW EXECUTE FUNCTION public.private_scheduler_run_transitions_append_only();

CREATE TRIGGER trg_private_scheduler_run_transitions_no_truncate
    BEFORE TRUNCATE ON public.private_scheduler_run_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION public.private_scheduler_run_transitions_append_only();

-- ----------------------------------------------------------------------------
-- 4. Row level security and table privileges (service_role only)
-- ----------------------------------------------------------------------------
ALTER TABLE public.private_scheduler_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.private_scheduler_run_transitions ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.private_scheduler_runs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.private_scheduler_run_transitions FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE ON TABLE public.private_scheduler_runs TO service_role;
GRANT SELECT, INSERT ON TABLE public.private_scheduler_run_transitions TO service_role;

-- ----------------------------------------------------------------------------
-- 5. Idempotent initialization RPC
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.initialize_private_scheduler_run(
    p_run_idempotency_sha256 VARCHAR(64),
    p_admission_source VARCHAR(32),
    p_trigger_kind VARCHAR(16),
    p_work_kind VARCHAR(32),
    p_scope VARCHAR(16),
    p_owner_id UUID,
    p_portfolio_id UUID,
    p_scheduled_for TIMESTAMPTZ,
    p_event_cause_kind VARCHAR(32),
    p_cause_key TEXT,
    p_cause_available_at TIMESTAMPTZ,
    p_policy_key VARCHAR(128),
    p_policy_revision BIGINT,
    p_admission_payload JSONB
)
RETURNS TABLE (
    status TEXT,
    run_idempotency_sha256 VARCHAR(64),
    state VARCHAR(16),
    state_version BIGINT
)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_existing public.private_scheduler_runs%ROWTYPE;
    v_constraint TEXT;
BEGIN
    BEGIN
        INSERT INTO public.private_scheduler_runs (
            run_idempotency_sha256, admission_source, trigger_kind, work_kind, scope, owner_id, portfolio_id,
            scheduled_for, event_cause_kind, cause_key, cause_available_at, policy_key, policy_revision, admission_payload,
            state, state_version, claim_key, claimed_at, lease_expires_at, terminal_at, failure_code
        ) VALUES (
            p_run_idempotency_sha256, p_admission_source, p_trigger_kind, p_work_kind, p_scope, p_owner_id, p_portfolio_id,
            p_scheduled_for, p_event_cause_kind, p_cause_key, p_cause_available_at, p_policy_key, p_policy_revision, p_admission_payload,
            'ready', 1, NULL, NULL, NULL, NULL, NULL
        );

        INSERT INTO public.private_scheduler_run_transitions (
            run_idempotency_sha256, after_state_version, transition_kind, before_state_version, transition_at,
            after_state, claim_key, claimed_at, lease_expires_at, terminal_at, failure_code
        ) VALUES (
            p_run_idempotency_sha256, 1, 'initialize', NULL, NULL,
            'ready', NULL, NULL, NULL, NULL, NULL
        );

        RETURN QUERY SELECT 'initialized'::TEXT, p_run_idempotency_sha256::VARCHAR(64), 'ready'::VARCHAR(16), 1::BIGINT;
        RETURN;
    EXCEPTION WHEN unique_violation THEN
        -- Only the logical-run primary key may enter the duplicate / conflict path; any other uniqueness violation is re-raised.
        GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
        IF v_constraint IS DISTINCT FROM 'pk_private_scheduler_runs' THEN
            RAISE;
        END IF;

        -- The subtransaction rollback discarded both tentative inserts: re-read the authoritative existing run.
        SELECT r.* INTO v_existing
        FROM public.private_scheduler_runs AS r
        WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256;

        IF NOT FOUND THEN
            RAISE;
        END IF;

        IF p_admission_source IS NOT DISTINCT FROM v_existing.admission_source
           AND p_trigger_kind IS NOT DISTINCT FROM v_existing.trigger_kind
           AND p_work_kind IS NOT DISTINCT FROM v_existing.work_kind
           AND p_scope IS NOT DISTINCT FROM v_existing.scope
           AND p_owner_id IS NOT DISTINCT FROM v_existing.owner_id
           AND p_portfolio_id IS NOT DISTINCT FROM v_existing.portfolio_id
           AND p_scheduled_for IS NOT DISTINCT FROM v_existing.scheduled_for
           AND p_event_cause_kind IS NOT DISTINCT FROM v_existing.event_cause_kind
           AND p_cause_key IS NOT DISTINCT FROM v_existing.cause_key
           AND p_cause_available_at IS NOT DISTINCT FROM v_existing.cause_available_at
           AND p_policy_key IS NOT DISTINCT FROM v_existing.policy_key
           AND p_policy_revision IS NOT DISTINCT FROM v_existing.policy_revision
           AND p_admission_payload IS NOT DISTINCT FROM v_existing.admission_payload THEN
            RETURN QUERY SELECT 'idempotent_duplicate'::TEXT, v_existing.run_idempotency_sha256, v_existing.state, v_existing.state_version;
            RETURN;
        END IF;

        RETURN QUERY SELECT 'conflict'::TEXT, v_existing.run_idempotency_sha256, v_existing.state, v_existing.state_version;
        RETURN;
    END;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.initialize_private_scheduler_run(VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR, UUID, UUID, TIMESTAMPTZ, VARCHAR, TEXT, TIMESTAMPTZ, VARCHAR, BIGINT, JSONB) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.initialize_private_scheduler_run(VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR, UUID, UUID, TIMESTAMPTZ, VARCHAR, TEXT, TIMESTAMPTZ, VARCHAR, BIGINT, JSONB) FROM anon;
REVOKE EXECUTE ON FUNCTION public.initialize_private_scheduler_run(VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR, UUID, UUID, TIMESTAMPTZ, VARCHAR, TEXT, TIMESTAMPTZ, VARCHAR, BIGINT, JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.initialize_private_scheduler_run(VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR, UUID, UUID, TIMESTAMPTZ, VARCHAR, TEXT, TIMESTAMPTZ, VARCHAR, BIGINT, JSONB) TO service_role;

-- ----------------------------------------------------------------------------
-- 6. Atomic state_version CAS transition RPC
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.apply_private_scheduler_run_transition(
    p_run_idempotency_sha256 VARCHAR(64),
    p_expected_version BIGINT,
    p_transition_kind VARCHAR(32),
    p_claim_key TEXT,
    p_transition_at TIMESTAMPTZ,
    p_lease_expires_at TIMESTAMPTZ,
    p_failure_code VARCHAR(128)
)
RETURNS TABLE (
    status TEXT,
    run_idempotency_sha256 VARCHAR(64),
    state VARCHAR(16),
    state_version BIGINT
)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_row public.private_scheduler_runs%ROWTYPE;
    v_after public.private_scheduler_runs%ROWTYPE;
    v_rows INTEGER;
    v_not_before TIMESTAMPTZ;
    v_ok BOOLEAN := FALSE;
    v_applied BOOLEAN := FALSE;
BEGIN
    IF p_transition_kind IS NULL
       OR p_transition_kind NOT IN ('claim', 'renew_claim', 'take_over_expired_claim', 'succeed', 'fail') THEN
        RAISE EXCEPTION 'p_transition_kind must be a supported non-initial transition kind.';
    END IF;
    IF p_run_idempotency_sha256 IS NULL OR p_expected_version IS NULL THEN
        RAISE EXCEPTION 'p_run_idempotency_sha256 and p_expected_version must be non-null.';
    END IF;

    -- The row lock serializes concurrent transitions; every UPDATE below still carries the full compare-and-swap predicate.
    SELECT r.* INTO v_row
    FROM public.private_scheduler_runs AS r
    WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found'::TEXT, p_run_idempotency_sha256, NULL::VARCHAR(16), NULL::BIGINT;
        RETURN;
    END IF;

    IF v_row.state_version <> p_expected_version THEN
        RETURN QUERY SELECT 'version_conflict'::TEXT, v_row.run_idempotency_sha256, v_row.state, v_row.state_version;
        RETURN;
    END IF;

    IF p_transition_kind = 'claim' THEN
        v_not_before := CASE v_row.trigger_kind WHEN 'scheduled' THEN v_row.scheduled_for ELSE v_row.cause_available_at END;
        v_ok := COALESCE(
            v_row.state = 'ready'
            AND p_claim_key IS NOT NULL
            AND p_transition_at IS NOT NULL
            AND p_lease_expires_at IS NOT NULL
            AND p_failure_code IS NULL
            AND p_transition_at >= v_not_before
            AND p_transition_at < p_lease_expires_at, FALSE);
        IF v_ok THEN
            UPDATE public.private_scheduler_runs AS r
            SET state = 'claimed',
                state_version = r.state_version + 1,
                claim_key = p_claim_key,
                claimed_at = p_transition_at,
                lease_expires_at = p_lease_expires_at,
                terminal_at = NULL,
                failure_code = NULL
            WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256
              AND r.state_version = p_expected_version
              AND r.state = 'ready'
            RETURNING r.* INTO v_after;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_applied := v_rows = 1;
        END IF;

    ELSIF p_transition_kind = 'renew_claim' THEN
        v_ok := COALESCE(
            v_row.state = 'claimed'
            AND p_claim_key IS NOT NULL
            AND p_claim_key IS NOT DISTINCT FROM v_row.claim_key
            AND p_transition_at IS NOT NULL
            AND p_lease_expires_at IS NOT NULL
            AND p_failure_code IS NULL
            AND (v_row.claimed_at <= p_transition_at AND p_transition_at < v_row.lease_expires_at)
            AND p_lease_expires_at > v_row.lease_expires_at, FALSE);
        IF v_ok THEN
            UPDATE public.private_scheduler_runs AS r
            SET state_version = r.state_version + 1,
                lease_expires_at = p_lease_expires_at
            WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256
              AND r.state_version = p_expected_version
              AND r.state = 'claimed'
              AND r.claim_key = p_claim_key
            RETURNING r.* INTO v_after;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_applied := v_rows = 1;
        END IF;

    ELSIF p_transition_kind = 'take_over_expired_claim' THEN
        v_ok := COALESCE(
            v_row.state = 'claimed'
            AND p_claim_key IS NOT NULL
            AND p_claim_key IS DISTINCT FROM v_row.claim_key
            AND p_transition_at IS NOT NULL
            AND p_lease_expires_at IS NOT NULL
            AND p_failure_code IS NULL
            AND p_transition_at >= v_row.lease_expires_at
            AND p_lease_expires_at > p_transition_at, FALSE);
        IF v_ok THEN
            UPDATE public.private_scheduler_runs AS r
            SET state_version = r.state_version + 1,
                claim_key = p_claim_key,
                claimed_at = p_transition_at,
                lease_expires_at = p_lease_expires_at
            WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256
              AND r.state_version = p_expected_version
              AND r.state = 'claimed'
              AND r.lease_expires_at <= p_transition_at
            RETURNING r.* INTO v_after;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_applied := v_rows = 1;
        END IF;

    ELSIF p_transition_kind = 'succeed' THEN
        v_ok := COALESCE(
            v_row.state = 'claimed'
            AND p_claim_key IS NOT NULL
            AND p_claim_key IS NOT DISTINCT FROM v_row.claim_key
            AND p_transition_at IS NOT NULL
            AND p_lease_expires_at IS NULL
            AND p_failure_code IS NULL
            AND (v_row.claimed_at <= p_transition_at AND p_transition_at < v_row.lease_expires_at), FALSE);
        IF v_ok THEN
            UPDATE public.private_scheduler_runs AS r
            SET state = 'succeeded',
                state_version = r.state_version + 1,
                terminal_at = p_transition_at
            WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256
              AND r.state_version = p_expected_version
              AND r.state = 'claimed'
              AND r.claim_key = p_claim_key
              AND r.claimed_at <= p_transition_at
              AND p_transition_at < r.lease_expires_at
            RETURNING r.* INTO v_after;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_applied := v_rows = 1;
        END IF;

    ELSE
        v_ok := COALESCE(
            v_row.state = 'claimed'
            AND p_claim_key IS NOT NULL
            AND p_claim_key IS NOT DISTINCT FROM v_row.claim_key
            AND p_transition_at IS NOT NULL
            AND p_lease_expires_at IS NULL
            AND p_failure_code IS NOT NULL
            AND (v_row.claimed_at <= p_transition_at AND p_transition_at < v_row.lease_expires_at), FALSE);
        IF v_ok THEN
            UPDATE public.private_scheduler_runs AS r
            SET state = 'failed',
                state_version = r.state_version + 1,
                terminal_at = p_transition_at,
                failure_code = p_failure_code
            WHERE r.run_idempotency_sha256 = p_run_idempotency_sha256
              AND r.state_version = p_expected_version
              AND r.state = 'claimed'
              AND r.claim_key = p_claim_key
              AND r.claimed_at <= p_transition_at
              AND p_transition_at < r.lease_expires_at
            RETURNING r.* INTO v_after;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_applied := v_rows = 1;
        END IF;
    END IF;

    IF NOT v_applied THEN
        RETURN QUERY SELECT 'transition_conflict'::TEXT, v_row.run_idempotency_sha256, v_row.state, v_row.state_version;
        RETURN;
    END IF;

    -- Same transaction as the UPDATE above: a history failure rolls the run update back.
    INSERT INTO public.private_scheduler_run_transitions (
        run_idempotency_sha256, after_state_version, transition_kind, before_state_version, transition_at,
        after_state, claim_key, claimed_at, lease_expires_at, terminal_at, failure_code
    ) VALUES (
        v_after.run_idempotency_sha256, v_after.state_version, p_transition_kind, p_expected_version, p_transition_at,
        v_after.state, v_after.claim_key, v_after.claimed_at, v_after.lease_expires_at, v_after.terminal_at, v_after.failure_code
    );

    RETURN QUERY SELECT 'applied'::TEXT, v_after.run_idempotency_sha256, v_after.state, v_after.state_version;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.apply_private_scheduler_run_transition(VARCHAR, BIGINT, VARCHAR, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, VARCHAR) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.apply_private_scheduler_run_transition(VARCHAR, BIGINT, VARCHAR, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, VARCHAR) FROM anon;
REVOKE EXECUTE ON FUNCTION public.apply_private_scheduler_run_transition(VARCHAR, BIGINT, VARCHAR, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, VARCHAR) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.apply_private_scheduler_run_transition(VARCHAR, BIGINT, VARCHAR, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, VARCHAR) TO service_role;
