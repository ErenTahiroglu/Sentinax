-- 🛡️ AI Portföy Yöneticisi: Eksik Tablo Kurulum Betiği (v1.3.0)
-- Bu betiği Supabase -> SQL Editor kısmına yapıştırıp çalıştırın.

-- 1. User Settings Tablosu (Onboarding ve Bildirim Ayarları)
CREATE TABLE IF NOT EXISTS public.user_settings (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    onboarding_profile JSONB DEFAULT '{}'::jsonb,
    is_onboarded BOOLEAN DEFAULT false,
    telegram_chat_id TEXT,
    risk_tolerance TEXT DEFAULT 'Orta',
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 2. Paper Trades Tablosu (Sanal İşlem Kayıtları)
CREATE TABLE IF NOT EXISTS public.paper_trades (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    type TEXT, -- BUY, SELL, REBALANCE
    target_weight DOUBLE PRECISION,
    timestamp TIMESTAMPTZ DEFAULT now()
);

-- 3. Portfolio Snapshots Tablosu
CREATE TABLE IF NOT EXISTS public.portfolio_snapshots (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ DEFAULT now(),
    assets JSONB DEFAULT '[]'::jsonb,
    total_value DOUBLE PRECISION DEFAULT 0,
    cash_balance DOUBLE PRECISION DEFAULT 0
);

-- 4. Legacy Portfolio Watchlists Tablosu (Güncel Ticker Listesi; private-engine public.portfolios ile ÇAKIŞMAZ)
CREATE TABLE IF NOT EXISTS public.legacy_portfolio_watchlists (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tickers JSONB DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 5. Alerts Tablosu (Bildirimler)
CREATE TABLE IF NOT EXISTS public.alerts (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    ticker TEXT,
    message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 6. LLM Usage Logs (Kota Takibi)
CREATE TABLE IF NOT EXISTS public.llm_usage_logs (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    timestamp TIMESTAMPTZ DEFAULT now()
);

-- 7. User Events (Telemetri / SRE)
CREATE TABLE IF NOT EXISTS public.user_events (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 🔐 RLS (Row Level Security) Politikaları
ALTER TABLE public.user_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portfolio_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.legacy_portfolio_watchlists ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_usage_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_events ENABLE ROW LEVEL SECURITY;

-- Politikalar (Genel Şablon: Sadece kendi verisini gör/ekle)
DROP POLICY IF EXISTS "Users can view own data" ON public.legacy_portfolio_watchlists;
CREATE POLICY "Users can view own data" ON public.legacy_portfolio_watchlists FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can update own data" ON public.legacy_portfolio_watchlists;
CREATE POLICY "Users can update own data" ON public.legacy_portfolio_watchlists FOR ALL USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can view own alerts" ON public.alerts;
CREATE POLICY "Users can view own alerts" ON public.alerts FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can update own alerts" ON public.alerts;
CREATE POLICY "Users can update own alerts" ON public.alerts FOR UPDATE USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can view own usage" ON public.llm_usage_logs;
CREATE POLICY "Users can view own usage" ON public.llm_usage_logs FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can view own events" ON public.user_events;
CREATE POLICY "Users can view own events" ON public.user_events FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can insert own events" ON public.user_events;
CREATE POLICY "Users can insert own events" ON public.user_events FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Ekstra Tablo Politikaları (Hardening v1.1.3)
DROP POLICY IF EXISTS "Users can own settings" ON public.user_settings;
CREATE POLICY "Users can own settings" ON public.user_settings FOR ALL USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can own paper_trades" ON public.paper_trades;
CREATE POLICY "Users can own paper_trades" ON public.paper_trades FOR ALL USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can own snapshots" ON public.portfolio_snapshots;
CREATE POLICY "Users can own snapshots" ON public.portfolio_snapshots FOR ALL USING (auth.uid() = user_id);

-- 🚀 Başarılı! Tüm tablolar ve SRE kalkanları hazır.
