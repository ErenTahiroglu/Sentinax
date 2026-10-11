# Phase 25D1E3 — Aynı-origin sahip oturum yaşam döngüsü ve Deep Ocean entegrasyonu (ÇEVRİMDIŞI)

Dağıtım, gerçek hesap, gerçek veri, canlı SQL, anahtar işlemi yok. Üretim hazırlığı iddiası yoktur.

## 1. Mimari karar
Sahip girişi ve özel uygulama **tek belgede, tek Supabase istemcisinde, tek bellek-içi oturumda** çalışır (`/owner/`, kanonik yol Cloudflare Pages klasör-index anlambilimidir; ayrı `/owner/app` rotası yoktur çünkü sayfa yeniden yüklemesi bellek-içi oturumu kaybettirirdi). `private_ui/` önizlemesi kaldırıldı; Deep Ocean kabuğu `frontend/owner/` içine taşındı (tek kaynak, tek CSS, `.app` altında kapsamlı). Genel Buffett ayrı origin'dedir. İstemci `persistSession:false`, `autoRefreshToken:true`, `detectSessionInUrl:false` ayarlarını aynen korur.

Yeni modüller (SDK'ya, depolamaya, mesajlaşmaya, doğrudan ağa dokunmaz):
- `private_api_config.js`: yalnızca `{mode:'same-origin'}`; ana makine adı yok, çapraz-origin modu yok.
- `private_session.js`: saf denetleyici `createPrivateSession` (DOM'suz). MFA makinesi (`createOwnerMfaFlow`) tek AAL mantığı olarak kalır.
- `private_shell.js`: DOM bağlama; yalnızca metin düğümü, gizlenince tüm alanlar temizlenir.
- `owner_app.js`: tek `createClient`; `fetch` yalnızca burada, tek sarmalayıcı.

## 2. Oturum sözleşmesi
Özel uygulama yalnızca şu zincirle açılır: flow `UNLOCKED` → `canRenderPrivate()` → taze bellek-içi erişim belirteci → `GET /private/portfolio/summary` (`require_private_owner`) **200** ve katı biçimde doğrulanmış `UNAVAILABLE` zarfı. Sunucu sahip kimliği ve AAL2/TOTP için tek otoritedir; istemcinin AAL2'si tek başına yetmez. İstek: `Authorization: Bearer` (başka hiçbir yerde belirteç yok), `credentials:'omit'`, `cache:'no-store'`, `redirect:'error'`, `referrerPolicy:'no-referrer'`, `mode:'same-origin'`, 10 sn zaman aşımı.
Geçersiz kılma (senkron, SDK beklenmeden): çıkış, global çıkış, MFA düşürme, kilit/ERROR_LOCKED → `IDLE` + özet `null`. 401/403 → DENIED; 503 → SERVER_UNAVAILABLE; ağ/zaman aşımı → UNREACHABLE; yapılandırma yok → UNCONFIGURED; biçimsiz/EMPTY/AVAILABLE/ek alan/HTML → MALFORMED; diğer → ERROR. Hiçbiri EMPTY sayılmaz; sunucu metni gösterilmez.
Bayat sonuç: `gen` sayacı + `unlocked` + `flow.canRenderPrivate()`; kimlik değişimi (kullanıcı id + `session_id`, yalnızca değişiklik tespiti, yetki kararı değil) yanıttan sonra yeniden okunup uyuşmazsa yanıt atılır ve yeni kimlik için yeniden yetkilendirilir (en çok 3). Aynı oturumun belirteç yenilemesi kabul edilir. Tekrarlı AUTH olayları tek isteğe birleşir; yetkili görünüm 60 sn'de, başarısızlıklar 15 sn'de yeniden denenir; yeniden doğrulama başarısızsa görünüm gizlenir.

## 3. Cloudflare çıktısı (açık, gözden geçirilmiş allowlist)
13 dosya: 11 sahip dosyası (`index.html`, `css/owner.css`, `css/deep_ocean.css`, `js/{owner_app,client_config,owner_public_config,mfa_flow,private_api_config,private_session,private_shell}.js`, SDK) + `404.html` + `_headers`. SDK bütünlük denetimi, kök 404, `no-store` ve diğer başlıklar aynen. Tek CSP değişikliği: `connect-src 'self' <Supabase proje origin'i>` (özel API'nin **aynı-origin** yolu için). Platform/derleme çıktı yolu değişmedi. Eski Vercel sahip paketinin başlık kopyası yalnızca eşitlik testi için güncellendi (hedef değil).
Üretimde `/private/*` yolunun Oracle arka ucuna yönlendirilmesi (Cloudflare tarafı) **henüz yoktur**; o zamana dek istek statik origin'de 404 alır ve arayüz kilitli kalır. Çapraz-origin API gerekirse: CSP `connect-src` ve arka uç CORS'ta aynı tam origin, joker karakter/izin verici proxy yok (ayrı kapı).
Eski Vercel kökleri: `.vercelignore` ve `frontend/.vercelignore` `owner`'ı dağıtımdan hariç tutar (platformda doğrulanmadı; eski host zaten DEPLOYMENT_NOT_FOUND dönüyordu).

## 4. Kanıt sınırları
Tarayıcı testleri gerçek SDK'yı sahte Auth origin'ine ve **test-yalnız** özel API taklidine karşı çalıştırır; taklit sunucu kuralını (sahip + aal2 + totp) yansıtır, FastAPI değildir. Arka ucun kendi yetkilendirmesi `test_private_portfolio_summary_api.py` ile (gerçek uygulama, sentetik imzalı JWT) kanıtlanır ve test fixture'ı gerçek uç yanıtıyla eşitlenir (drift testi). Gerçek Supabase, gerçek TOTP ve tarayıcı→gerçek FastAPI uçtan uca akışı **doğrulanmamıştır**.
Tarayıcıda gerçek bir `fetch` iptal edildiğinde geç yanıt zaten ulaşmaz; "iptali yok sayan sunucu" senaryosu yalnızca birim katmanında (sahte fetch) sınanır.

## 5. Açık kapılar
Canlı Cloudflare Pages/Oracle/Supabase; gerçek sahip hesabı + iki TOTP; Cloudflare→Oracle `/private/*` yönlendirmesi; Migration 032 sertleştirme/terfi; HS256/service-role geçişi; idempotency; WebSocket; CORS üretim ayarı; eski SPA XSS; Buffett AVAILABLE dalı; `/api/metrics` açığı.
