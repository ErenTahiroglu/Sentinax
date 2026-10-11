# Phase 25D1E1 — İlk korumalı özel okuma API'si ve Deep Ocean arayüz temeli (ÇEVRİMDIŞI GELİŞTİRME)

Bu faz dağıtım değildir. Hiçbir bulut kaynağı, gerçek hesap, gerçek veri, canlı SQL veya anahtar işlemi yok. Hedef mimari: Cloudflare Pages + Oracle VM + Supabase (Vercel/Render yalnızca eski yapılandırma).

## 1. API sözleşmesi
`GET /private/portfolio/summary` (yeni `/private` ad alanı; `/buffett` genel alanıyla karışmaz, `/api/*` eski sağlık/metrik uçlarıdır). Yalnızca-okuma; `/private` altında başka yol ve POST/PUT/PATCH/DELETE yok (testle kilitli).

Yetkilendirme: mevcut `require_private_owner` (ikinci bir doğrulama yok): imzalı Supabase JWT (`verify_jwt`: imza/JWKS, iss, aud, exp, rol, anonim değil) → operatör yapılandırmasındaki `SENTINAX_OWNER_USER_ID` ile `sub` eşleşmesi → `aal = aal2` → imzalı `amr` içinde `{method: totp, timestamp: int}`. Sahip yapılandırması yoksa/geçersizse 503 (fail-closed). Sahip kimliği istekten (başlık, sorgu, çerez, gövde) alınmaz; belirteç yalnızca `Authorization: Bearer`. Service-role anahtarı kimlik doğrulamada kullanılmaz.

Yanıt `private_portfolio_summary.v1` (pydantic, `extra=forbid`): `contract_version, data_status, reason_code, reason, source, as_of, generated_at, warnings, portfolios, positions`.
- `UNAVAILABLE`: nitelikli kaynak bağlı değil (bu fazda **tek** üretilebilen durum; `source/as_of` null, koleksiyonlar boş).
- `EMPTY`: yetkili kaynak kontrol edildi ve pozisyon yok → model `source` ve `as_of` ister; bağlantı yokluğu EMPTY olamaz.
- `AVAILABLE`: nitelikli kanıt var → `source` ve `as_of` zorunlu.
Üretim kodunda yalnızca `build_summary()` vardır ve yalnızca UNAVAILABLE döner; router yalnızca fastapi/pydantic/datetime/`private_access` içe aktarır (ağ, DB, ortam değişkeni, service-role yok — AST ile test edilir). Test verisi üretim modülünde yoktur.
Başlıklar: başarı ve 401/403/503 hatalarında `Cache-Control: private, no-store`, `Pragma: no-cache`, `Vary: Authorization`, `nosniff` (`NoCacheMiddleware` `/private` için genişletildi). Hata gövdeleri belirteç, sahip UUID, yapılandırma adı veya URL içermez.

## 2. Arayüz kabuğu (`private_ui/`, çevrimdışı prototip)
Statik HTML+CSS+JS (bağımlılık yok), Deep Ocean paleti (`frontend/styles.css` ve owner sayfasından). Altı alan: Genel Bakış, Yatırım Fırsatları, Portföy, Dağılım ve Senaryolar, Piyasa Araştırması, Risk ve Kanıt; her biri açık **UNAVAILABLE** durum kartı. Sade/Gelişmiş mod bellekte (depolama yok); gelişmiş mod sözleşme alanlarını belgelenmiş durum olarak gösterir (sunucu yanıtı değil). Sayı/yüzde/para tutarı, grafik, görsel, form, giriş, depolama, ağ, belirteç yok; sıkı CSP (`connect-src 'none'`).
**Oturum-origin kuralı:** gerçek kimlikli özel arayüz ile sahip girişi AYNI yalıtılmış özel origin'de olmalıdır; genel Buffett ayrı origin. Owner SDK oturumu bellektedir. Bu fazda giriş→portföy geçişi, URL ile belirteç aktarımı, origin'ler arası el değiştirme, localStorage, postMessage **yok**; ayrı tasarlanıp test edilmeden eklenmeyecek.
**Dağıtım yalıtımı:** `private_ui/` `frontend/` dışındadır (eski Vercel kökü `/frontend/` altını sunar), owner allowlist'inde ve Cloudflare owner çıktısında yoktur (9 dosya testle korunur), Buffett'te yoktur, hiçbir iş akışı onu dağıtmaz.

## 3. Ayrıcalıklı (service-role) giriş noktaları — YALNIZCA DURUM (bu fazda değiştirilmedi)
Service-role RLS'i atlar; RLS bu yolları korumaz. FastAPI sahip kontrolü yalnızca `require_private_owner` kullanan rotaları korur; aşağıdakiler kullanmaz:
| Giriş noktası | Durum |
|---|---|
| `backend/api/main.py` `GET /api/health` | **Kimliksiz** herkese açık uç; service-role anahtarıyla PostgREST `portfolios?limit=1` çağırır ve yanıta istisna metnini koyabilir (`error: ...`). Sahip-korumalı değil. Ayrı düzeltme gerekir. |
| `backend/api/dependencies.py` `check_llm_quota` | `verify_jwt` (herhangi bir kimliği doğrulanmış kullanıcı, sahip DEĞİL) sonrası service-role ile `user_settings` (migrasyon dışı eski tablo, canlıda varlığı UNKNOWN) okur. Eski sohbet/LLM yolu. |
| `backend/api/dependencies.py` `get_supabase_client` | service-role istemci üreten yardımcı; mevcut kod tabanında çağıranı bulunmadı (UNKNOWN: dış çağıranlar). |
| `backend/infrastructure/scheduler.py` | service-role ile REST; eski/ölü kod (içe aktaran yok, doğrulanmadı). |
| `oracle_worker/src/data/whitelist.py` | işçi, service-role ile okur; dağıtım yok (UNKNOWN). |
| `supabase/functions/analyze/index.ts` | kullanıcı JWT'li istemci ile kimlik, `adminClient` (service-role) yalnızca oran sınırı ve Vault için; Edge Function kimlik/`verify_jwt` ayarı canlıda UNKNOWN. |
| `render.yaml` (eski) | service-role, JWT secret ve `SENTINAX_OWNER_USER_ID` ortam anahtarlarını tanımlar; Render hedef mimari değildir; canlı durum UNKNOWN. |
| `backend/engine/private/portfolio/*` PostgREST taşıyıcı | kimlik bilgisi enjekte edilen istemciyle gelir; üretim kablolaması bu fazda incelenmedi/bağlanmadı. |
Not: CORS `allow_methods` yazma fiillerini ve eski bir Vercel origin'ini varsayılan olarak içerir; `/private` altında yazma yolu olmadığından etki yok, ama üretimde `ALLOWED_ORIGINS` açıkça belirlenmelidir.

## 4. Migration 032 adayı — geri alma düzeltmesi
`supabase/candidates/032_*` UYGULANMADI/terfi ettirilmedi. Geri alma betiği kısıtlayıcı politikaları ve yardımcıyı kaldırır ama **032'nin geri çektiği yetkileri geri VERMEZ** (anon tablo hakları, authenticated TRUNCATE/REFERENCES/TRIGGER, anon RPC EXECUTE). Üretime terfi öncesi AYRI bir "migrasyon sertleştirme kapısı" şarttır: (1) tam pre-032 katalog ve yetki anlık görüntüsü; (2) fonksiyon imzası uyumu; (3) açık atomik uygulama/geri alma sözleşmesi; (4) canlı şema sınıflandırması (migrasyon dışı 7 tablo dahil); (5) veritabanı ve arka uçta sahip kimliği tutarlılığı (`private_config.owner_identity` = `SENTINAX_OWNER_USER_ID`); (6) canlı PostgREST JWT `aal/amr` taleplerinin doğrulanması.

## 5. Açık üretim kapıları (hiçbiri bu fazda kapanmadı)
Canlı şema/grant; gerçek sahip hesabı ve iki kurtarılabilir TOTP; gerçek Supabase AAL2/AMR davranışı; `SENTINAX_OWNER_USER_ID` üretim yapılandırması; Migration 032 terfisi; eski HS256 geçişi; service-role geçişi; idempotency; WebSocket kimlik doğrulama; eski SPA XSS; genel Buffett AVAILABLE dalı; üretim dağıtımı.

## 6. Sonraki en küçük faz
`/api/health` ve `check_llm_quota` gibi kimliksiz/sahip-dışı service-role yollarının kapatılması veya sahip korumasına alınması (küçük, test edilebilir, canlı gerektirmez); ardından özel oturum yaşam döngüsünün (aynı-origin giriş→kabuk) bağımsız tasarımı.
