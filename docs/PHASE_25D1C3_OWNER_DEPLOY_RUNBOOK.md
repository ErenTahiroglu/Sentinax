# Phase 25D1C3 — İzole sahip sayfası dağıtımı (operatör kapısı, HENÜZ UYGULANMADI)

Durum: **paket hazır, dağıtım yok.** Canlı Vercel projesi: **NOT CONFIGURED / UNKNOWN** (bu fazda oluşturulmadı, sorgulanmadı).
Bu belge dağıtımı yetkilendirmez. Gerçek kimlik bilgisi girmek **Gate A** (Red Team onayı) sonrasına kadar yasaktır.

## Paket
- Kaynak: `frontend/owner/` (7 dosya, allowlist: `scripts/owner_deploy_build.mjs`). Üretilen çıktı: `deploy/owner-bootstrap/dist/` (git'te yok).
- Yapılandırma: `deploy/owner-bootstrap/vercel.json` — `buildCommand: node ../../scripts/owner_deploy_build.mjs`, `outputDirectory: dist`, `cleanUrls`, `trailingSlash:false`, **rewrite/route yok**, tek `/(.*)` başlık kuralı.
- Legacy SPA, Buffett, `sw.js`, `config.js`, backend ve veri dosyaları pakete **girmez** (allowlist + build'de fail-closed denetim).
- Kök `vercel.json` ve `frontend/vercel.json` bu projeyi **yönetmez**; Root Directory `deploy/owner-bootstrap` olduğunda yalnızca oradaki `vercel.json` okunur (Vercel belgeleri: yapılandırma Root Directory'den okunur — canlıda doğrulanmadı).

## Operatör prosedürü
1. **Erişim:** Vercel hesabında yeni proje oluşturma yetkisi; GitHub repo bağlantısı. Hiçbir sır/Vercel token'ı repoya girmez; CI bunları gerektirmez.
2. **Proje ayarı:** Yeni, ayrı proje. Root Directory = `deploy/owner-bootstrap`. Framework Preset = Other. Build/Output ayarları `vercel.json`'dan gelir (UI'da farklıysa `vercel.json` önceliklidir; ikisini de kontrol et). **"Include source files outside of the Root Directory in the Build Step" açık olmalı** (build komutu `../../scripts` ve `../../frontend/owner` okur) — bu ayar bu repoda doğrulanmadı. Ortam değişkeni gerekmez; ekleme.
3. **Host adı:** Legacy uygulamadan farklı, ayrı bir origin (ayrı alt alan adı veya ayrı `*.vercel.app`). Legacy host üzerinde `/owner` yayını bu aşamada **kaldırılmalı/planlanmalı** (aynı origin = XSS riski). Preview dağıtımları herkese açık olmamalı (Deployment Protection).
4. **Bütünlük:** Dağıtılan commit SHA'sını kaydet; `node scripts/owner_sdk_build.mjs --check` ve `node scripts/owner_deploy_build.mjs` yerelde temiz çalışmalı; SDK SHA-256 = `fd67f317…51e7` (`supabase-js.manifest.json`).
5. **Anonim GET/HEAD denetimi (dağıtım sonrası, gerçek HTTP):** `/owner`, `/owner/css/owner.css`, `/owner/js/{owner_app,client_config,owner_public_config,mfa_flow}.js`, `/owner/vendor/supabase-js.2.117.1.mjs` → 200. `/owner/` ve `/owner/index.html` → 308 → `/owner`. 
6. **Beklenen 404:** `/`, `/index.html`, `/sw.js`, `/manifest.json`, `/api/*`, `/frontend/*`, `/buffett/*`, `/owner/js/yok.js`, `/owner/vendor/supabase-js.manifest.json`. 404 gövdesi sahip HTML'i içermemeli.
7. **Başlıklar (her yanıtta, 404 dahil):** `Cache-Control: no-store`, `Content-Security-Policy` (script-src 'self', connect-src yalnızca proje origin'i, frame-ancestors 'none', unsafe-* yok), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, COOP/CORP `same-origin`, HSTS, `Permissions-Policy`, `X-Robots-Tag`. `curl -sI` çıktısı kanıt olarak saklanır. Statik yapılandırma tek başına teslimi kanıtlamaz.
8. **Temiz tarayıcı + eski service worker:** Yeni profil; DevTools → Application → Service Workers boş olmalı, Storage boş olmalı. Daha önce legacy origin'de kayıtlı SW **farklı origin'de etkisizdir**; yine de eski origin'de `/owner` kaldırıldıktan sonra SW'nin unregister edildiği ayrıca doğrulanır.
9. **Supabase public ayar:** `owner_public_config.js` yalnızca proje URL'si + `sb_publishable_` anahtarı içerir; Supabase panelindeki proje ref'i/anahtar ile karşılaştır. Servis/secret anahtar, JWT, UUID, parola pakette olmamalı (build bunu reddeder).
10. **Geri alma:** Vercel projesini duraklat/sil veya domain'i kaldır → "sahip sayfası yok" durumu. Repo tarafında geri alma gerekmez (paket pasif).

## Yasaklar
Gate A öncesi: gerçek sahip hesabı oluşturma/oturum açma, TOTP kaydı, canlı smoke betiği, Migration 032, HS256/service-role iptali, üretim SQL'i.
Dağıtım yapılırsa: tam dağıtılan SHA + bağımsız gözlenmiş HTTP kanıtı zorunlu; simüle yönlendirme testi dağıtım kanıtı değildir.
