# Phase 25D1C5 — Cloudflare Pages sahip paketi ve üretim bağımlılık planı (HENÜZ UYGULANMADI)

Hedef mimari: **Cloudflare Pages** (ön yüz) + Cloudflare DNS/edge + **Oracle Always Free ARM64 VM** (özel arka uç) + **Supabase** (DB/Auth/TOTP/RLS). Vercel ve Render yalnızca eski yapılandırmadır, hedef değildir.
Bu belge dağıtımı yetkilendirmez. Hiçbir bulut hesabında kaynak oluşturulmadı. Cloudflare projesi: **NOT CONFIGURED / UNKNOWN**. Oracle A1 kapasitesi: **UNKNOWN**.
`docs/PHASE_25D1C3_OWNER_DEPLOY_RUNBOOK.md` (Vercel) bu hedef için **geçersizdir**; `deploy/owner-bootstrap/` yalnızca eski paket olarak kalır (header eşitliği testle korunur).

## Paket
- Üretim: `node scripts/owner_cloudflare_build.mjs` → `deploy/owner-cloudflare/dist` (git'te yok). Yalnızca node yerleşik modülleri; ek bağımlılık yok.
- İçerik (tam allowlist, 9 dosya): 7 sahip dosyası (`owner/index.html`, `owner/css/owner.css`, `owner/js/{owner_app,client_config,owner_public_config,mfa_flow}.js`, `owner/vendor/supabase-js.2.117.1.mjs`) + 2 platform dosyası: `404.html`, `_headers`. `_redirects`, Functions, Worker, kök `index.html`, `sw.js`, legacy/Buffett/backend dosyası **yok**.
- Sahip dosyaları `frontend/owner/` kaynağından bayt-bayt kopyalanır; SDK SHA-256 `fd67f317…51e7` (manifest + `owner_sdk_build.mjs --check`).
- `404.html` zorunlu: **üst düzey 404.html yoksa Pages projeyi SPA sayar ve "/" içeriğini her bilinmeyen yola döner** (belgeli). Build, 404.html ve `_headers` yoksa/zayıfsa başarısız olur.
- `_headers`: tek `/*` kuralı (öncelik/birleşim belirsizliği yok); CSP (script-src 'self', connect-src yalnızca Supabase proje origin'i, frame-ancestors 'none', unsafe-*/wildcard yok), `Cache-Control: no-store`, nosniff, X-Frame-Options DENY, Referrer-Policy no-referrer, COOP/CORP same-origin, HSTS, Permissions-Policy, X-Robots-Tag ve **`! Access-Control-Allow-Origin`** (Pages her yanıta `Access-Control-Allow-Origin: *` ekler; sahip projesi bunu kaldırır). Satır içi yorum yok (platform başlık sanabilir).

## Pages proje ayarları (öneri; uygulanmadı)
- Ayrı proje "owner" (genel Buffett ön yüzü ayrı Pages projesi, ayrı origin). Aynı GitHub deposu.
- Root directory: **boş (depo kökü)** — Vercel'deki "kök dışı kaynak" bağımlılığı yoktur; build komutu `scripts/` ve `frontend/owner/` yollarını depo kökünden okur.
- Build command: `node scripts/owner_cloudflare_build.mjs` · Build output directory: `deploy/owner-cloudflare/dist` · Framework preset: None.
- Kökte `package.json` olduğundan Pages bağımlılık kurulumu çalıştırabilir (sonuç gözlenmedi); gerekirse Node sürümü `NODE_VERSION` ortam değişkeniyle sabitlenir (sır değil). Başka ortam değişkeni gerekmez. CI hiçbir Cloudflare token'ı kullanmaz/gerektirmez.
- Önizleme dağıtımları herkese açık sahip girişi olmamalı (Access koruması veya branch kontrolü); ilk dağıtım yalnızca `*.pages.dev`.

## DOĞRULANMAMIŞ platform varsayımları (gerçek dağıtımda gözlenmeli)
1. `/owner` ve `/owner/index.html` → `/owner/` yönlendirmesi ve durum kodu (model 308 varsayar; belgede yalnızca `index.html → /dir/` var).
2. `404.html` yanıtlarına `_headers` uygulanması ve durum kodunun 404 olması.
3. `.mjs` dosyasının JavaScript MIME türüyle sunulması (modül importu için zorunlu; yanlışsa sahip sayfası yüklenmez — kritik kontrol).
4. `! Access-Control-Allow-Origin` ile varsayılanın gerçekten kaldırılması; Referrer-Policy/Cache-Control geçersiz kılmaları.
5. Nokta-segment ve kodlanmış gezinme isteklerinin 404'e düşmesi; `/_headers` ve `/404` yollarının davranışı.
6. pages.dev önizleme alt alanlarının davranışı ve `Etag`/304 ile `no-store` etkileşimi.
Hiçbiri **VERIFIED** sayılamaz; yerel sunucu yalnızca belgelenmiş modeli uygular.

## Operatör doğrulama (dağıtım sonrası, giriş yok)
Tam dağıtılan SHA'yı kaydet; anonim GET/HEAD: `/owner/`, 6 varlık → 200 ve tam başlık seti; `/owner`, `/owner/index.html` → yönlendirme zinciri; `/`, `/index.html`, `/sw.js`, `/manifest.json`, `/api/x`, `/frontend/*`, `/buffett/*`, `/owner/js/yok.js`, `/_headers`, SDK manifest yolu → 404 ve sahip HTML'i yok. Temiz Chromium: sign-in görünür, CSP ihlali yok, service worker yok, depolama boş, yalnızca aynı origin istekleri. Eski (legacy) origin'de `/owner` yayını kaldırılmadan gerçek kimlik bilgisi girilmez (Gate A).

## Üretim bağımlılık planı (bu kontrol noktasında YÜRÜTÜLMEZ)
A. Cloudflare Pages sahip projesi (bu paket). B. Ayrı Pages projesi: genel Buffett (kendi build çıktısı, kendi origin'i). C. Oracle Always Free VM: kapasite ve dağıtılabilirlik UNKNOWN (A1 stoğu garanti değil; ücretli yedek yok → $0 hedefi). D. Arka uca Cloudflare girişi: DNS/proxy veya Tunnel, yalnızca gerektiğinde; kaynak origin doğrudan erişimi kapatılır. E. Supabase Auth: JWKS/ES256, TOTP, tek sahip kimliği. F. Migration 032 + RLS (aal2 RESTRICTIVE). G. Eski HS256/API anahtarı geçişi (geri alınabilir sıra ile). H. Service-role sertleştirme, idempotency, WebSocket güvenliği. I. Özel finans verisinin kontrollü etkinleştirilmesi. Her adım ayrı kapı ve ayrı Red Team onayı gerektirir; hiçbiri bu kontrol noktasında başlatılmadı.

## Geri alma
Pages projesini duraklat/sil veya özel alan adını kaldır → "sahip sayfası yok". Depoda geri alma gerekmez (paket pasif).
