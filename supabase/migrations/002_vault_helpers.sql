-- 002_vault_helpers.sql
-- Secure helpers for managing API keys in Supabase Vault

CREATE OR REPLACE FUNCTION public.upsert_user_api_key(p_api_key TEXT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_user_id UUID;
    v_old_secret_id UUID;
    v_new_secret_id UUID;
BEGIN
    v_user_id := auth.uid();
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'Not authenticated';
    END IF;

    -- 1. Check for existing secret_id in user_profiles
    SELECT vault_secret_id INTO v_old_secret_id
    FROM public.user_profiles
    WHERE user_id = v_user_id;

    -- 2. If old secret exists, delete it from vault.secrets first 
    -- (Vault secrets are immutable in terms of content usually, so we replace)
    IF v_old_secret_id IS NOT NULL THEN
        DELETE FROM vault.secrets WHERE id = v_old_secret_id;
    END IF;

    -- 3. Insert new secret into Vault
    -- Note: We use the 'description' to track which user this belongs to for auditability
    INSERT INTO vault.secrets (name, secret, description)
    VALUES (
        'gemini_api_key_' || v_user_id::text,
        p_api_key,
        'Gemini API Key for user ' || v_user_id::text
    )
    RETURNING id INTO v_new_secret_id;

    -- 4. Update or Insert user_profile
    INSERT INTO public.user_profiles (user_id, vault_secret_id)
    VALUES (v_user_id, v_new_secret_id)
    ON CONFLICT (user_id) DO UPDATE
    SET vault_secret_id = v_new_secret_id,
        updated_at = now();

END;
$$;
