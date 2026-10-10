-- 030_learning_evidence_bindings.sql
-- Phase 28B-1A: append-only learning evidence bindings (offline; no collection, no labels, no models).
--
-- A binding ties a Phase 28B-0 source-evidence declaration to ONE retained row of the existing PIT store (public.raw_provider_snapshots). It stores references and
-- declared metadata only: no payload copy, no universe/observation claims. What the database itself enforces (insert trigger): the referenced snapshot exists, its provider
-- and endpoint equal the declared source identity, a payload is retained, the declared content hash equals the snapshot's stored payload_hash and the declared retrieval
-- instant equals the snapshot's retrieved_at. The database CANNOT recompute Python's canonical payload hash: re-hashing the retained payload is performed by the trusted
-- writer (backend.engine.learning.adapters.verify_evidence_binding) and is recorded only as WRITER_REHASH_ASSERTED. Every other verification level is a CHECK-constant
-- UNVERIFIED, and capture_authorized is CHECK-constant false. recorded_at is database-assigned and is not the provider retrieval time.
--
-- Audit-driven guarantee for the existing store: a bound snapshot is protected from payload/identity change or deletion by guard_learning_bound_raw_snapshot() even when the
-- original prevent_raw_snapshot_tamper() trigger is absent from a live database (migration 028 once found it absent). Unbound snapshots are not touched by this migration.

CREATE TABLE IF NOT EXISTS public.learning_evidence_bindings (
    raw_snapshot_id UUID NOT NULL REFERENCES public.raw_provider_snapshots(id) ON DELETE RESTRICT,
    source_id TEXT NOT NULL CHECK (source_id ~ '^[a-z][a-z0-9]*([._-][a-z0-9]+)*$' AND length(source_id) <= 128),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    prev_revision INTEGER GENERATED ALWAYS AS (CASE WHEN revision > 1 THEN revision - 1 END) STORED,

    expected_provider VARCHAR(64) NOT NULL,
    expected_endpoint VARCHAR(255) NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot_retrieved_at TIMESTAMPTZ NOT NULL,
    capture_attempted_at TIMESTAMPTZ NOT NULL,
    economic_date DATE,
    publication_time TIMESTAMPTZ,
    publication_evidence_sha256 TEXT CHECK (publication_evidence_sha256 IS NULL OR publication_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    data_revision TEXT,
    document_version TEXT,
    source_reference TEXT NOT NULL,
    authority_class TEXT NOT NULL CHECK (authority_class IN
        ('primary_official_document', 'official_public_provider_surface', 'academic_primary', 'secondary_reporting', 'unclassified')),
    availability_declared TEXT NOT NULL CHECK (availability_declared IN ('publicly_available', 'access_restricted', 'unavailable', 'unknown')),
    licensing_declared TEXT NOT NULL CHECK (licensing_declared IN ('unresolved', 'permission_declared', 'prohibition_declared')),
    licensing_evidence_sha256s TEXT[] NOT NULL DEFAULT '{}',
    known_limitations TEXT[] NOT NULL,

    stored_content_integrity TEXT NOT NULL DEFAULT 'WRITER_REHASH_ASSERTED' CHECK (stored_content_integrity = 'WRITER_REHASH_ASSERTED'),
    record_reference_existence TEXT NOT NULL DEFAULT 'VERIFIED_BY_DATABASE' CHECK (record_reference_existence = 'VERIFIED_BY_DATABASE'),
    source_identity_binding TEXT NOT NULL DEFAULT 'VERIFIED_BY_DATABASE' CHECK (source_identity_binding = 'VERIFIED_BY_DATABASE'),
    document_assertion_verification TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (document_assertion_verification = 'UNVERIFIED'),
    licensing_access_verification TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (licensing_access_verification = 'UNVERIFIED'),
    economic_completeness_verification TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (economic_completeness_verification = 'UNVERIFIED'),
    publication_time_authority TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (publication_time_authority = 'UNVERIFIED'),
    capture_authorized BOOLEAN NOT NULL DEFAULT false CHECK (capture_authorized = false),

    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (raw_snapshot_id, source_id, revision),
    FOREIGN KEY (raw_snapshot_id, source_id, prev_revision)
        REFERENCES public.learning_evidence_bindings (raw_snapshot_id, source_id, revision) ON DELETE RESTRICT,
    CONSTRAINT learning_bindings_attempt_not_after_retrieval CHECK (capture_attempted_at <= snapshot_retrieved_at),
    CONSTRAINT learning_bindings_publication_pair CHECK ((publication_time IS NULL) = (publication_evidence_sha256 IS NULL)),
    CONSTRAINT learning_bindings_publication_not_after_retrieval CHECK (publication_time IS NULL OR publication_time <= snapshot_retrieved_at),
    CONSTRAINT learning_bindings_licensing_evidence_pair CHECK ((licensing_declared = 'unresolved') = (cardinality(licensing_evidence_sha256s) = 0))
);

ALTER TABLE public.learning_evidence_bindings ENABLE ROW LEVEL SECURITY;
-- Intentionally NO policy: access is by explicit table grants to service_role only (same pattern as the private scheduler tables).

REVOKE ALL ON TABLE public.learning_evidence_bindings FROM PUBLIC;
REVOKE ALL ON TABLE public.learning_evidence_bindings FROM anon;
REVOKE ALL ON TABLE public.learning_evidence_bindings FROM authenticated;
REVOKE ALL ON TABLE public.learning_evidence_bindings FROM service_role;
GRANT SELECT, INSERT ON TABLE public.learning_evidence_bindings TO service_role;

-- ---------------------------------------------------------------------------------------------------------------------------------------------
-- Insert-time integrity: runs for EVERY insert path (RPC or direct), so a privileged writer cannot bind a mismatching or nonexistent snapshot.
CREATE OR REPLACE FUNCTION public.enforce_learning_evidence_binding_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_snapshot public.raw_provider_snapshots%ROWTYPE;
    v_hash text;
BEGIN
    SELECT * INTO v_snapshot FROM public.raw_provider_snapshots WHERE id = NEW.raw_snapshot_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'learning evidence binding: referenced raw snapshot % does not exist', NEW.raw_snapshot_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF v_snapshot.provider IS DISTINCT FROM NEW.expected_provider OR v_snapshot.endpoint IS DISTINCT FROM NEW.expected_endpoint THEN
        RAISE EXCEPTION 'learning evidence binding: source identity mismatch for snapshot %', NEW.raw_snapshot_id USING ERRCODE = 'check_violation';
    END IF;
    IF v_snapshot.raw_payload IS NULL THEN
        RAISE EXCEPTION 'learning evidence binding: snapshot % retains no payload', NEW.raw_snapshot_id USING ERRCODE = 'check_violation';
    END IF;
    IF v_snapshot.payload_hash IS DISTINCT FROM NEW.content_sha256 THEN
        RAISE EXCEPTION 'learning evidence binding: content hash does not equal the stored payload_hash of snapshot %', NEW.raw_snapshot_id USING ERRCODE = 'check_violation';
    END IF;
    IF v_snapshot.retrieved_at IS DISTINCT FROM NEW.snapshot_retrieved_at THEN
        RAISE EXCEPTION 'learning evidence binding: retrieval instant does not equal the retained snapshot retrieved_at (%)', NEW.raw_snapshot_id USING ERRCODE = 'check_violation';
    END IF;
    IF (SELECT count(DISTINCT h) FROM unnest(NEW.licensing_evidence_sha256s) AS h) <> cardinality(NEW.licensing_evidence_sha256s)
       OR EXISTS (SELECT 1 FROM unnest(NEW.licensing_evidence_sha256s) AS h WHERE h !~ '^[0-9a-f]{64}$') THEN
        RAISE EXCEPTION 'learning evidence binding: licensing evidence hashes must be unique lowercase SHA-256' USING ERRCODE = 'check_violation';
    END IF;
    NEW.recorded_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_learning_bindings_insert_integrity ON public.learning_evidence_bindings;
CREATE TRIGGER trg_learning_bindings_insert_integrity
    BEFORE INSERT ON public.learning_evidence_bindings
    FOR EACH ROW EXECUTE FUNCTION public.enforce_learning_evidence_binding_insert();

-- Append-only: no UPDATE, DELETE or TRUNCATE for anyone, including a superuser (privileges are revoked above; this closes the owner/superuser path too).
CREATE OR REPLACE FUNCTION public.reject_learning_evidence_binding_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'learning_evidence_bindings is append-only (% rejected); record a new revision instead', TG_OP USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_learning_bindings_no_update_delete ON public.learning_evidence_bindings;
CREATE TRIGGER trg_learning_bindings_no_update_delete
    BEFORE UPDATE OR DELETE ON public.learning_evidence_bindings
    FOR EACH ROW EXECUTE FUNCTION public.reject_learning_evidence_binding_mutation();

DROP TRIGGER IF EXISTS trg_learning_bindings_no_truncate ON public.learning_evidence_bindings;
CREATE TRIGGER trg_learning_bindings_no_truncate
    BEFORE TRUNCATE ON public.learning_evidence_bindings
    FOR EACH STATEMENT EXECUTE FUNCTION public.reject_learning_evidence_binding_mutation();

-- Bound snapshots: only the system supersession flags may change; deletion is refused. Independent of prevent_raw_snapshot_tamper().
CREATE OR REPLACE FUNCTION public.guard_learning_bound_raw_snapshot()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.learning_evidence_bindings b WHERE b.raw_snapshot_id = OLD.id) THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'raw snapshot % is bound by learning evidence and cannot be deleted', OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(OLD) - 'is_superseded' - 'superseded_at') IS DISTINCT FROM (to_jsonb(NEW) - 'is_superseded' - 'superseded_at') THEN
        RAISE EXCEPTION 'raw snapshot % is bound by learning evidence: only is_superseded and superseded_at may change', OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_learning_guard_bound_raw_snapshot ON public.raw_provider_snapshots;
CREATE TRIGGER trg_learning_guard_bound_raw_snapshot
    BEFORE UPDATE OR DELETE ON public.raw_provider_snapshots
    FOR EACH ROW EXECUTE FUNCTION public.guard_learning_bound_raw_snapshot();

REVOKE ALL ON FUNCTION public.enforce_learning_evidence_binding_insert() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.reject_learning_evidence_binding_mutation() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.guard_learning_bound_raw_snapshot() FROM PUBLIC, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------------------------------------------------------------------------
-- Atomic, idempotent batch writer. SECURITY INVOKER: it runs with the caller's (service_role) table privileges, so it adds no privilege. Natural idempotency key:
-- (raw_snapshot_id, source_id, revision); identical content is reported EXISTING, conflicting content is rejected. Any failure rolls back the whole call.
CREATE OR REPLACE FUNCTION public.record_learning_evidence_bindings(p_bindings jsonb)
RETURNS TABLE (o_raw_snapshot_id uuid, o_source_id text, o_revision integer, o_disposition text)
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    c_allowed constant text[] := ARRAY[
        'raw_snapshot_id', 'source_id', 'revision', 'expected_provider', 'expected_endpoint', 'content_sha256', 'snapshot_retrieved_at', 'capture_attempted_at',
        'economic_date', 'publication_time', 'publication_evidence_sha256', 'data_revision', 'document_version', 'source_reference', 'authority_class',
        'availability_declared', 'licensing_declared', 'licensing_evidence_sha256s', 'known_limitations'];
    c_required constant text[] := ARRAY[
        'raw_snapshot_id', 'source_id', 'revision', 'expected_provider', 'expected_endpoint', 'content_sha256', 'snapshot_retrieved_at', 'capture_attempted_at',
        'source_reference', 'authority_class', 'availability_declared', 'licensing_declared', 'licensing_evidence_sha256s', 'known_limitations'];
    v_item jsonb;
    v_key text;
    v_id uuid;
    v_source text;
    v_revision integer;
    v_licensing text[];
    v_limitations text[];
    v_inserted integer;
    v_same integer;
BEGIN
    IF p_bindings IS NULL OR jsonb_typeof(p_bindings) <> 'array' OR jsonb_array_length(p_bindings) NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'p_bindings must be a JSON array of 1 to 500 binding objects' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    FOR v_item IN SELECT e.value FROM jsonb_array_elements(p_bindings) AS e LOOP
        IF jsonb_typeof(v_item) <> 'object' THEN
            RAISE EXCEPTION 'each binding must be a JSON object' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        FOR v_key IN SELECT k FROM jsonb_object_keys(v_item) AS k LOOP
            IF NOT (v_key = ANY (c_allowed)) THEN
                RAISE EXCEPTION 'unexpected binding key: %', v_key USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END LOOP;
        FOREACH v_key IN ARRAY c_required LOOP
            IF NOT (v_item ? v_key) OR jsonb_typeof(v_item -> v_key) = 'null' THEN
                RAISE EXCEPTION 'missing required binding key: %', v_key USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END LOOP;

        v_id := (v_item ->> 'raw_snapshot_id')::uuid;
        v_source := v_item ->> 'source_id';
        v_revision := (v_item ->> 'revision')::integer;
        v_licensing := ARRAY(SELECT jsonb_array_elements_text(v_item -> 'licensing_evidence_sha256s'));
        v_limitations := ARRAY(SELECT jsonb_array_elements_text(v_item -> 'known_limitations'));

        INSERT INTO public.learning_evidence_bindings (
            raw_snapshot_id, source_id, revision, expected_provider, expected_endpoint, content_sha256, snapshot_retrieved_at, capture_attempted_at, economic_date,
            publication_time, publication_evidence_sha256, data_revision, document_version, source_reference, authority_class, availability_declared, licensing_declared,
            licensing_evidence_sha256s, known_limitations)
        VALUES (
            v_id, v_source, v_revision, v_item ->> 'expected_provider', v_item ->> 'expected_endpoint', v_item ->> 'content_sha256',
            (v_item ->> 'snapshot_retrieved_at')::timestamptz, (v_item ->> 'capture_attempted_at')::timestamptz, (v_item ->> 'economic_date')::date,
            (v_item ->> 'publication_time')::timestamptz, v_item ->> 'publication_evidence_sha256', v_item ->> 'data_revision', v_item ->> 'document_version',
            v_item ->> 'source_reference', v_item ->> 'authority_class', v_item ->> 'availability_declared', v_item ->> 'licensing_declared',
            v_licensing, v_limitations)
        ON CONFLICT (raw_snapshot_id, source_id, revision) DO NOTHING;
        GET DIAGNOSTICS v_inserted = ROW_COUNT;

        IF v_inserted = 1 THEN
            RETURN QUERY SELECT v_id, v_source, v_revision, 'INSERTED'::text;
        ELSE
            SELECT count(*) INTO v_same FROM public.learning_evidence_bindings b
            WHERE b.raw_snapshot_id = v_id AND b.source_id = v_source AND b.revision = v_revision
              AND b.expected_provider = v_item ->> 'expected_provider'
              AND b.expected_endpoint = v_item ->> 'expected_endpoint'
              AND b.content_sha256 = v_item ->> 'content_sha256'
              AND b.snapshot_retrieved_at = (v_item ->> 'snapshot_retrieved_at')::timestamptz
              AND b.capture_attempted_at = (v_item ->> 'capture_attempted_at')::timestamptz
              AND b.economic_date IS NOT DISTINCT FROM (v_item ->> 'economic_date')::date
              AND b.publication_time IS NOT DISTINCT FROM (v_item ->> 'publication_time')::timestamptz
              AND b.publication_evidence_sha256 IS NOT DISTINCT FROM v_item ->> 'publication_evidence_sha256'
              AND b.data_revision IS NOT DISTINCT FROM v_item ->> 'data_revision'
              AND b.document_version IS NOT DISTINCT FROM v_item ->> 'document_version'
              AND b.source_reference = v_item ->> 'source_reference'
              AND b.authority_class = v_item ->> 'authority_class'
              AND b.availability_declared = v_item ->> 'availability_declared'
              AND b.licensing_declared = v_item ->> 'licensing_declared'
              AND b.licensing_evidence_sha256s = v_licensing
              AND b.known_limitations = v_limitations;
            IF v_same <> 1 THEN
                RAISE EXCEPTION 'conflicting learning evidence binding for (%, %, revision %): same identity, different content; submit a new revision',
                    v_id, v_source, v_revision USING ERRCODE = 'unique_violation';
            END IF;
            RETURN QUERY SELECT v_id, v_source, v_revision, 'EXISTING'::text;
        END IF;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION public.record_learning_evidence_bindings(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_learning_evidence_bindings(jsonb) TO service_role;
