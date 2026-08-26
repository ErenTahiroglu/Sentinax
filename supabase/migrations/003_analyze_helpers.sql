-- 003_analyze_helpers.sql
-- Utilities for the LLM Decision Engine (Edge Function)

-- ============================================================================
-- 1. Distributed Rate Limiting (Token Bucket)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.rate_limits (
    identifier TEXT PRIMARY KEY,
    tokens FLOAT NOT NULL DEFAULT 15,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL
);

-- RPC for atomic rate limit consumption
CREATE OR REPLACE FUNCTION public.consume_rate_limit(
    p_identifier TEXT,
    p_capacity INT DEFAULT 15,
    p_refill_rate FLOAT DEFAULT 0.25 -- 15 tokens per 60 seconds (0.25 per sec)
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_now TIMESTAMP WITH TIME ZONE := now();
    v_last_updated TIMESTAMP WITH TIME ZONE;
    v_tokens FLOAT;
    v_new_tokens FLOAT;
BEGIN
    -- Get current state or initialize
    SELECT tokens, last_updated INTO v_tokens, v_last_updated
    FROM public.rate_limits
    WHERE identifier = p_identifier
    FOR UPDATE;

    IF NOT FOUND THEN
        v_tokens := p_capacity;
        v_last_updated := v_now;
        INSERT INTO public.rate_limits (identifier, tokens, last_updated)
        VALUES (p_identifier, v_tokens, v_last_updated);
    END IF;

    -- Calculate refill
    v_new_tokens := LEAST(p_capacity, v_tokens + (EXTRACT(EPOCH FROM (v_now - v_last_updated)) * p_refill_rate));

    -- Check if we have at least one token
    IF v_new_tokens >= 1 THEN
        UPDATE public.rate_limits
        SET tokens = v_new_tokens - 1,
            last_updated = v_now
        WHERE identifier = p_identifier;
        RETURN TRUE;
    ELSE
        RETURN FALSE;
    END IF;
END;
$$;

-- ============================================================================
-- 2. Secure Vault Decryption
-- ============================================================================
-- This function allows the Edge Function (running as service_role or authenticated user)
-- to retrieve the raw API key from Vault without exposing the Vault schema directly.
CREATE OR REPLACE FUNCTION public.get_user_api_key()
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, vault
AS $$
DECLARE
    v_secret_id UUID;
    v_api_key TEXT;
BEGIN
    -- auth.uid() is automatically available in Supabase context
    SELECT vault_secret_id INTO v_secret_id 
    FROM public.user_profiles 
    WHERE user_id = auth.uid();

    IF v_secret_id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Access the decrypted secret from the vault.decrypted_secrets view
    SELECT decrypted_secret INTO v_api_key 
    FROM vault.decrypted_secrets 
    WHERE id = v_secret_id;

    RETURN v_api_key;
END;
$$;
