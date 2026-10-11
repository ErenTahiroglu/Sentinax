# Phase 25D1E4 — Genel Buffett girişi ve gerçek FastAPI ile bağlı uçtan uca doğrulama (ÇEVRİMDIŞI)

Dağıtım, gerçek hesap, gerçek veri, canlı SQL, anahtar işlemi yok. Üretim hazırlığı iddiası yoktur.

## 1. Kullanıcı yolculuğu ve sınırlar
GENEL BUFFETT → SAHİP GİRİŞİ → ÖZEL SENTINAX. Genel Buffett kimliksizdir, yalnızca Buffett araştırması (fail-closed UNAVAILABLE) gösterir. Sahip girişi **ayrı, yalıtılmış bir origin**dedir (Supabase parola + TOTP/AAL2 + sunucu tarafı sahip doğrulaması, tarayıcıda kalıcı oturum yok). Özel Sentinax, mevcut tümleşik sahip belgesinde açılır. Basit/Gelişmiş yalnızca sunum derinliğidir; finansal yetkiyi değiştirmez. Genel ve özel arayüz kimlik doğrulama origin'i paylaşmaz; belirteç/oturum aktarımı yoktur.

## 2. Genel "Sahip Girişi" bağlantısı
`frontend/buffett/js/owner_entry_config.js` (sabitlenmiş, git'te gözden geçirilen) + `owner_entry.js` (saf doğrulayıcı) + `app.js` (tek `setAttribute('href')`). Bağlantı düz bir `<a>`'dır: form yok, kayıt yok, ikinci kimlik doğrulama yok, `postMessage` yok, hedef ziyaretçi tarafından belirlenemez (URL, referrer, depolama, `window.name` okunmaz).
- **Hedef:** yalnızca `{origin, path:'/owner/'}`. `origin` şu an **`null`** (üretim ana makine adı yapılandırılmadığı için icat edilmedi) → bağlantı `aria-disabled`, `href` yok.
- **Doğrulama:** `https://` + çok etiketli küçük harfli alan adı (TLD harf/punycode); kullanıcı bilgisi, port, yol, sorgu, parça, sondaki `/`, boşluk, ters eğik çizgi, IP/`localhost`, büyük harf, `javascript:`/`data:`/`//` reddedilir; yol tam olarak `/owner/`; sayfanın kendi origin'i reddedilir (ayrı origin şart).
- **Çevrimdışı koşum kancası:** `http://127.0.0.1|localhost:<port>` hedefi **yalnızca** genel sayfanın kendisi loopback `http` üzerinden sunuluyorsa kabul edilir (üretimde asla); üretim koduna test bayrağı/atlama yoktur.
- Mobil: üstbilgi dar ekranda sarılır (bağlı test 320 px'e kadar yatay taşma olmadığını doğrular; mevcut kart taşması da düzeltildi).

## 3. Gerçek tarayıcı → gerçek FastAPI koşum düzeneği (test-only)
`playwright.connected.config.js` üç yerel sunucu başlatır:
1. `backend/tests/e2e_harness/real_api_server.py`: **gerçek** `backend.api.main.app` (gerçek middleware, gerçek JWT doğrulayıcı, gerçek `require_private_owner`) + yalnızca burada yaşayan, oturum başına gizli başlıkla korunan `/__harness/*` yolları (belirteç imzalama — geçerli ve kasıtlı bozuk varyantlar —, kayıt günlüğü, sahip yapılandırmasını değiştirme). Sentetik ES256 anahtarı koşumda üretilir; JWKS, doğrulayıcının *getirme* fonksiyonu değiştirilerek enjekte edilir (birim testlerle aynı teknik). Sentetik sahip UUID'i. Loopback dışına çıkış soketleri reddedilir.
2. `scripts/owner_deploy_serve.mjs cloudflare` + `SENTINAX_TEST_API_ORIGIN`: yerleşik Cloudflare sahip çıktısı; yalnızca GET/HEAD `/private/*` isteğini loopback API'ye iletir (gelecekteki Cloudflare→Oracle yolunun modeli). Tarayıcı isteği aynı-origin görür.
3. `scripts/buffett_harness_serve.mjs`: genel Buffett'i ayrı origin olarak sunar, `/buffett/*` isteğini gerçek uygulamaya iletir.
Supabase Auth tarayıcı düzeyinde sahte kalır ama **access token'ları koşum imzalar**; böylece gerçek doğrulayıcı onları değerlendirir. Üretim kodunda `TEST_MODE`, kimlik doğrulama atlama veya doğrulanmamış belirteç kabulü yoktur (CI korumasi: backend üretim kodu koşum adlarına atıf yapamaz).

## 4. Kapsanan bağlı durumlar (Chromium + Mobile Chrome, 14 test × 2)
Genel Buffett kimliksiz yüklenir ve gerçek uygulamadan UNAVAILABLE alır (belirteç/çerez yok); bağlantı pinli origin'e gider (URL tam `.../owner/`, sorgu/parça yok, `document.referrer` boş); sahip sayfası SIGNED_OUT; yalnız parola/AAL1 özel uygulamayı açmaz ve **sunucuya hiç gitmez**; doğru TOTP → AAL2 → gerçek FastAPI 200 (`x-correlation-id` gerçek middleware'den; `Cache-Control: private, no-store`; `Set-Cookie` yok); canlı `generated_at`; Deep Ocean ve basit/gelişmiş; depolama/çerez/IDB boş (iki origin); ikinci sekme oturumu devralmaz; çıkış anında temizler; yenileme oturumu düşürür; yabancı kullanıcı (geçerli AAL2/TOTP) 403; yanlış imza/yanlış issuer/yanlış audience 401, bozuk `amr` ve TOTP'siz AAL2 403; sahip yapılandırması yok → 503; çıkış sonrası gecikmiş yanıt arayüzü doldurmaz; yanlış TOTP kodu kapalı kalır; yapılandırılmamış/onaysız hedef bağlantıyı devre dışı bırakır; düşman sorgu dizesi hedefi değiştiremez; HTTP seviyesinde 401/401/401/403/403/200 ve yazma fiilleri 404/405.
Süresi dolmuş belirteç yalnızca HTTP düzeyinde sınanır (SDK süresi dolmuş oturumu tarayıcıda yenileme akışına sokar).

## 5. Doğrulananlar ve varsayımlar
**Doğrulanan:** tarayıcı→gerçek FastAPI yetkilendirme zinciri (gerçek doğrulayıcı, gerçek sahip bağımlılığı, sentetik imzalı JWT'ler), genel/özel origin yalıtımı, depolamasızlık.
**Koşum varsayımları:** Auth tarayıcıda sahte; `/private/*` iletimi yerel bir yönlendirici (gerçek Cloudflare yönlendirmesi değil); iki loopback portu = iki origin; sahte JWKS. **Doğrulanmadı:** gerçek Supabase projesi/TOTP/AMR, gerçek Cloudflare Pages ve başlık teslimi, Oracle dağıtımı, genel Buffett'in Cloudflare Pages build/dağıtımı (henüz tanımlı değil; yalnızca `frontend/buffett` doğrulandı).

## 6. CI
Bağlı süit yalnızca Python bağımlılıkları olan **Backend Correctness Gate** işinde koşar (Node + Chromium kurulumu eklendi; yeni iş yok): atlanma/flaky/<28 geçiş = hata. Frontend rehberi (guard) süitin iki projede ≥14 test ile keşfedildiğini, `retries:0` ve gerçek API koşumunun yapılandırıldığını, backend üretim kodunun koşuma atıf yapmadığını ve genel sayfada form/kimlik doğrulama kodu olmadığını denetler. `vitest.config.js` `tests/connected/**` dosyasını hariç tutar (Playwright süiti).

## 7. Açık canlı kapılar
Canlı Cloudflare Pages projeleri (genel + sahip) ve alan adları (genel sayfadaki pinli origin'in belirlenmesi); Cloudflare→Oracle `/private/*` yönlendirmesi + CSP/CORS; gerçek Supabase sahip hesabı ve iki TOTP; Migration 032 sertleştirme/terfi; HS256/service-role geçişi; idempotency; WebSocket; `/api/metrics`; eski SPA XSS; Buffett AVAILABLE dalı; genel Buffett için üretim build/artifact tanımı.
