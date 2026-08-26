-- ============================================================================
-- 🏛️ Sentinax Migration 007: Macro Source Availability & Geography (FRED/ALFRED)
-- ============================================================================
-- Extends macro data layer to support global macro series (US/FRED) and
-- date-level point-in-time availability semantics (ALFRED realtime_start).
-- ============================================================================

-- 1. Extend macro_series with geography
ALTER TABLE public.macro_series
    ADD COLUMN IF NOT EXISTS geography VARCHAR(8) NOT NULL DEFAULT 'TR',
    ADD COLUMN IF NOT EXISTS provider_native_units TEXT,
    ADD COLUMN IF NOT EXISTS seasonal_adjustment TEXT,
    ADD COLUMN IF NOT EXISTS origin_source TEXT,
    ADD COLUMN IF NOT EXISTS release_name TEXT;

CREATE INDEX IF NOT EXISTS idx_macro_series_geography
    ON public.macro_series (geography);

-- 2. Extend macro_observations with date-level availability & origin metadata
ALTER TABLE public.macro_observations
    ADD COLUMN IF NOT EXISTS source_available_date DATE,
    ADD COLUMN IF NOT EXISTS availability_precision VARCHAR(16) NOT NULL DEFAULT 'DATE',
    ADD COLUMN IF NOT EXISTS realtime_end DATE,
    ADD COLUMN IF NOT EXISTS vintage_date DATE,
    ADD COLUMN IF NOT EXISTS origin_source TEXT,
    ADD COLUMN IF NOT EXISTS release_name TEXT;

CREATE INDEX IF NOT EXISTS idx_macro_obs_available_date
    ON public.macro_observations (macro_series_id, source_available_date DESC)
    WHERE source_available_date IS NOT NULL;

-- 3. Update Immutability Trigger to cover newly added substantive columns
CREATE OR REPLACE FUNCTION public.prevent_macro_observation_tamper()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Hard delete prohibited on macro_observations (id=%).', OLD.id;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        -- Strict allow-list: ONLY is_superseded and superseded_at may change
        IF (
            OLD.id != NEW.id OR
            OLD.macro_series_id != NEW.macro_series_id OR
            (OLD.snapshot_id IS DISTINCT FROM NEW.snapshot_id) OR
            OLD.effective_date != NEW.effective_date OR
            (OLD.value IS DISTINCT FROM NEW.value) OR
            OLD.unit != NEW.unit OR
            OLD.frequency != NEW.frequency OR
            OLD.data_status != NEW.data_status OR
            OLD.confidence_level != NEW.confidence_level OR
            OLD.source_tier != NEW.source_tier OR
            (OLD.published_at IS DISTINCT FROM NEW.published_at) OR
            OLD.observed_at != NEW.observed_at OR
            OLD.ingested_at != NEW.ingested_at OR
            (OLD.source_available_date IS DISTINCT FROM NEW.source_available_date) OR
            OLD.availability_precision != NEW.availability_precision OR
            (OLD.realtime_end IS DISTINCT FROM NEW.realtime_end) OR
            (OLD.vintage_date IS DISTINCT FROM NEW.vintage_date) OR
            (OLD.origin_source IS DISTINCT FROM NEW.origin_source) OR
            (OLD.release_name IS DISTINCT FROM NEW.release_name) OR
            (OLD.supersedes_record_id IS DISTINCT FROM NEW.supersedes_record_id) OR
            (OLD.warnings::text != NEW.warnings::text) OR
            (OLD.source_ref IS DISTINCT FROM NEW.source_ref) OR
            OLD.created_at != NEW.created_at
        ) THEN
            RAISE EXCEPTION 'Full-row immutability violation on macro_observations (id=%). Only supersession fields may be updated.', OLD.id;
        END IF;

        IF OLD.is_superseded = false AND NEW.is_superseded = true THEN
            IF NEW.superseded_at IS NULL THEN
                NEW.superseded_at := timezone('utc'::text, now());
            END IF;
            RETURN NEW;
        END IF;

        IF OLD.is_superseded = true AND NEW.is_superseded = false THEN
            RAISE EXCEPTION 'Cannot un-supersede a macro observation (id=%).', OLD.id;
        END IF;

        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
