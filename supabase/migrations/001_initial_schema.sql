-- 001_initial_schema.sql
-- Zero-Trust Architecture: Initial Schema Definition

-- Enable Supabase Vault
CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";

-- ============================================================================
-- 1. allowed_assets (Whitelist Table)
-- ============================================================================
-- This table acts as a strict whitelist. The system must ONLY process
-- assets present in this table with is_active = true.
CREATE TABLE public.allowed_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol VARCHAR(20) NOT NULL UNIQUE,
    asset_type VARCHAR(20) NOT NULL CHECK (asset_type IN ('BIST', 'FON', 'US_STOCK')),
    name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Enable RLS on allowed_assets
ALTER TABLE public.allowed_assets ENABLE ROW LEVEL SECURITY;

-- RLS Policy: Only active assets are viewable by everyone.
CREATE POLICY "Public can view active assets"
    ON public.allowed_assets
    FOR SELECT
    USING (is_active = true);

-- ============================================================================
-- 2. user_profiles
-- ============================================================================
-- Stores authenticated user data. Links to Supabase Vault for secret storage.
CREATE TABLE public.user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
    
    -- Reference to Supabase Vault for the API Key
    vault_secret_id UUID REFERENCES vault.secrets(id) ON DELETE SET NULL,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Enable RLS on user_profiles
ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;

-- RLS Policy: Users can only insert/update their own profile.
-- Direct SELECT is denied to keep internal vault_secret_id private.
CREATE POLICY "Users can insert own profile"
    ON public.user_profiles
    FOR INSERT
    WITH CHECK ( (SELECT auth.uid()) = user_id );

CREATE POLICY "Users can update own profile"
    ON public.user_profiles
    FOR UPDATE
    USING ( (SELECT auth.uid()) = user_id )
    WITH CHECK ( (SELECT auth.uid()) = user_id );

-- ============================================================================
-- 3. Safe Frontend Access (RPC)
-- ============================================================================
-- Returns true if the authenticated user has an API key configured.
-- This follows Zero-Trust by not exposing the encrypted key or vault ID to the client.
CREATE OR REPLACE FUNCTION public.check_user_has_api_key()
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER -- Essential to access user_profiles table without direct RLS permission
SET search_path = public
AS $$
DECLARE
    has_key BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 
        FROM public.user_profiles 
        WHERE user_id = auth.uid() 
        AND vault_secret_id IS NOT NULL
    ) INTO has_key;
    
    RETURN COALESCE(has_key, false);
END;
$$;

-- ============================================================================
-- 4. Triggers for updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = timezone('utc'::text, now());
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_allowed_assets_updated_at
    BEFORE UPDATE ON public.allowed_assets
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_user_profiles_updated_at
    BEFORE UPDATE ON public.user_profiles
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();
