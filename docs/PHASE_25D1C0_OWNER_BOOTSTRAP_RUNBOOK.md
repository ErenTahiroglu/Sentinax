# Phase 25D1C0 — Tek sahip hesabı, MFA kaydı ve kurtarma (operatör el kitabı)

Kapsam: sahibin güvenle tek hesap açması, iki bağımsız TOTP faktörünü kaydedip doğrulaması ve gerçek token uyumluluğunu **salt-okunur** sınaması. Bu belge canlı ayarları değiştirmez; hiçbir gerçek kimlik, UUID, token veya sır depoya yazılmaz. **Üretim güvenliği veya Phase 25D1C kapanışı iddia edilmez.**

## 0. Doğrulanmış başlangıç durumu (operatör beyanı; bu oturumda dashboard'a erişilmedi)

Aktif imza anahtarı ES256 (P-256); önceki anahtar legacy HS256; genel kayıt kapalı; TOTP açık, SMS kapalı; JWT ömrü 3600 sn; panelde aktif kullanıcı yok. Backend modu bu yüzden `jwks`.

## 1. Hesap daveti (yalnızca operatör)

1. Supabase Dashboard → Authentication → Users → **Create new user** (e-postayı otomatik onayla), parolayı parola yöneticisinde üretilmiş uzun rastgele bir değer olarak gir. Parolayı sohbete, depoya, ekran görüntüsüne koyma. "Invite" bağlantısı **kullanma**: sahip sayfası (`/owner/`) `detectSessionInUrl` kapalı olduğundan davet/parola belirleme bağlantısını işleyemez.
2. Genel kayıt (Allow new users to sign up) **kapalı kalmalı**; panelden kullanıcı oluşturmak bundan etkilenmez. Tarayıcı kodu hesap oluşturamaz: `/owner/` sayfasında ve eski istemcide `signUp`/OAuth çağrısı yoktur (testle sabitlendi).
3. Parola sıfırlama arayüzü bu checkpoint'te **yoktur**; parola değişikliği şimdilik operatör işlemidir (Dashboard).

## 2. Sahip UUID'sinin yapılandırılması (depoya yazmadan)

1. Dashboard → Users → kullanıcının **User UID** değerini kopyala.
2. Üretim ortamında (Render) gizli ortam değişkeni olarak `SENTINAX_OWNER_USER_ID` ayarla. Depoya, `.env.example`'a, issue/PR'a, sohbete, loglara yazma. Yerel deneme için yalnızca git'e girmeyen `.env` kullan.
3. Eksik veya geçersiz değer = özel erişim kapalı (503). UUID'yi değiştirirken önce yeni değeri yayına al, sonra eski kullanıcıyı sil (bkz. §7).

## 3. Üretim JWT modu

`SENTINAX_SUPABASE_JWT_MODE=jwks`, `SUPABASE_URL=https://<proje>.supabase.co`. Bu modda `SUPABASE_JWT_SECRET` okunmaz; HS256 token'lar reddedilir. Gerçek bir token'daki `iss` değerinin `<SUPABASE_URL>/auth/v1` olduğunu §5 testi doğrular (özel alan adı kullanılıyorsa iss farklı olabilir ve tüm token'lar reddedilir).

## 4. MFA kaydı: iki bağımsız TOTP faktörü (RLS'den önce zorunlu)

1. `/owner/` sayfasında e-posta + parola ile gir (durum: *NEEDS_ENROLLMENT*; özel içerik yok).
2. Faktör adı `birincil`; QR'ı **A uygulaması/cihazı** ile okut; 6 haneli kodu gir → *UNLOCKED (AAL2)*.
3. "Yedek faktör ekle": ad `yedek`; QR'ı **farklı bir uygulama/cihazla (B)** okut; kodu doğrula → sayfa "2 doğrulanmış faktör" gösterir ve uyarı kaybolur.
4. Çıkış yap, tekrar gir; *NEEDS_CHALLENGE* ekranında **önce A, sonra (ayrı bir oturumda) B** ile doğrula. Her iki faktörün de taze bir aal1→aal2 geçişinde çalıştığı kanıtlanmadan RLS (Migration 032) açılmaz.
5. Kayıt/QR/anahtar ekran görüntüsü alma; yedek anahtarı yalnızca parola yöneticisinde sakla. Supabase kurtarma kodu üretmez; en fazla 10 faktör kaydedilebilir.
6. Beş hatalı kayıt kodundan sonra bekleyen faktör iptal edilir ve silinir (istemci sınırı); sunucu tarafı oran sınırı ayrıca geçerlidir.

## 5. Gerçek token uyumluluk testi (salt-okunur, token basmaz)

Önkoşul: §2–§4 tamam. Operatör kendi makinesinde, depo kökünden:

```
export SUPABASE_URL=https://<proje>.supabase.co
export SENTINAX_SUPABASE_JWT_MODE=jwks
export SENTINAX_OWNER_USER_ID=<sahip uuid>        # kabuk geçmişine yazmamak için set +o history önerilir
export SUPABASE_PUBLISHABLE_KEY=<sb_publishable_...>   # tarayıcıdaki ile aynı, gizli değildir
python scripts/owner_token_smoke.py
```

E-posta, parola ve TOTP kodu `getpass` ile gizli sorulur (argv'ye yazılmaz). Betik oturum açar, TOTP ile aal2'ye çıkar, **üretimdeki doğrulayıcıyı ve sahip bağımlılığını** çalıştırır, yalnızca şu alanları yazar ve oturumu kapatır (`scope=local`): `verifier_accepts`, `alg` (beklenen `ES256`), `aal` (`aal2`), `totp_evidence`, `owner_match`, `owner_authorized`, `seconds_until_expiry` (≤ 3600). Beklenen sonuç: hepsi `true`/`aal2`/`ES256`. `verifier_accepts=false` + `http_401` → iss/aud/claim uyuşmazlığı; `http_503` → yapılandırma veya JWKS erişimi. Çıktıyı paylaşmak güvenlidir (token/UUID/e-posta içermez). Betik hiçbir hesap, faktör veya veri oluşturmaz.

## 6. Faktör kaybı ve kurtarma

| Durum | Adım |
|---|---|
| Bir faktör kayıp, diğeri çalışıyor | Çalışan faktörle gir; kayıp faktörü operatör olarak kaldır (Dashboard'da kullanıcı → faktörler seçeneği varsa; yoksa Auth Admin API ile faktör silme — **uç nokta adını güncel Supabase belgesinden doğrula**, gizli anahtarı yalnızca yerel makinede kullan). Sonra yeni bir yedek faktör ekle; iki faktöre dönmeden RLS açma. |
| Tüm faktörler kayıp | Break-glass: operatör, yerel makinede Auth Admin API ile kullanıcının faktörlerini siler (service-role/secret anahtarı **tarayıcıya veya depoya asla**), kullanıcının oturumlarını sonlandırır, sonra §4'ü baştan uygular. Belgeye göre Supabase, tüm faktörler kaybolursa destek yoluyla da erişimi geri getirmeyebilir; bu yüzden iki faktör şarttır. Prosedürü canlıdan önce bir Supabase **branch/test projesinde** prova et (bu oturumda denenmedi). |
| Parola kayıp | Dashboard'dan operatör parolayı yeniler; MFA ayrıca gerekir, parola sıfırlama tek başına aal2 vermez. |
| Cihaz çalındı | `/owner/` → "Tüm cihazlardan çıkış" (global), parolayı yenile, gerekirse faktörleri yeniden kaydet. Mevcut access token'lar `exp`'e (≤ 1 saat) kadar geçerli kalır. |

RLS (032) açıldıktan sonra yalnızca doğrudan PostgREST ile sahip erişimi kilitlenir; backend service-role ile çalışmaya devam eder. Bu nedenle 032 yalnızca §4 kanıtlarından sonra, önce Supabase branch'inde uygulanır.

## 7. Silinmiş Auth kullanıcıları: bağımlı kayıtlar ve geçmiş token riski

Kanıt (migration taraması; gerçek veri incelenmedi — `backend/tests/test_owner_bootstrap_readiness.py` şemayı sabitler):

* `auth.users`'a yalnızca iki tablo FK ile bağlıdır: `portfolios.owner_id` → **ON DELETE RESTRICT** (011:129) ve `user_profiles.user_id` → CASCADE (001:37). Diğer defter tabloları (`portfolio_accounts`, `cash_buckets`, `investment_goals`, `planned_contributions`, `portfolio_transactions`) `(portfolio_id, owner_id)` ile `portfolios`'a bağlıdır. Sonuç: portföyü olan bir kullanıcı silinemez (RESTRICT), yani silinmiş kullanıcıdan **artakalan defter kaydı FK ile engellenmiştir**; kayıt yoksa "aktif kullanıcı yok" görünümü tutarlıdır.
* FK ile **korunmayan** `user_id`/`owner_id` alanları (örn. `private_scheduler_runs.owner_id`, 024; PostgREST'ten kullanılan `user_settings`, `llm_usage_logs`, `alerts`, `portfolio_snapshots`, `legacy_portfolio_watchlists` ve Vault sırları) silinmiş kullanıcıdan yetim kayıt tutabilir; bunların bazıları incelenen migration'larda tanımlı değildir (şema dışı oluşturulmuş olabilir). Operatör, SQL Editor'da yalnızca **sayım** çalıştırır, satır içeriği paylaşmaz. Örnek (tablo yoksa atla):
  `select count(*) from public.<tablo> t where not exists (select 1 from auth.users u where u.id = t.user_id);`
  Sıfırdan farklı sayım → ayrı bir temizlik/saklama kararı gerekir (bu checkpoint silmez).
* Geçmiş token: silinen kullanıcının access token'ı imzası geçerli olduğu için `exp`'e (≤ 3600 sn) kadar doğrulayıcıdan geçer; refresh token'lar kullanıcıyla silinir. Backend sahip kontrolü `sub == SENTINAX_OWNER_USER_ID` olduğundan yeni oluşturulan hesap yeni rastgele UUID alır, eski token eşleşmez. Tek risk: ortamdaki UUID eski kullanıcıya işaret ederken o kullanıcıyı silmek/yeniden oluşturmak → önce env'i güncelle, sonra sil, ≥ 1 saat bekle.
* Legacy HS256 anahtarı "önceki" durumda olduğu sürece Supabase ürünleri (PostgREST/RLS) onunla imzalı token'ları kabul eder; backend `jwks` modunda reddeder. Legacy sır panelden çıkarılabilir olduğundan **PostgREST yüzeyi legacy sır sahibine açık kalır**. İptal planı (Supabase belgesi): döndürmeden sonra token ömrü + 15 dk (≥ 1 sa 15 dk) bekle; **ancak** eski `anon`/`service_role` anahtarları bu sırla imzalıdır — iptalden önce yayımlanabilir/gizli (`sb_...`) anahtarlara geçilmeli ve backend'in `SUPABASE_SERVICE_ROLE_KEY` kullanımının yeni gizli anahtarla çalıştığı doğrulanmalıdır (bu oturumda doğrulanmadı). İptal 25D1C öncesi bir önkoşuldur, bu checkpoint'te yapılmaz.

## 8. Ön yüz sınırları (dürüst kapsam)

* `/owner/` yalnızca kimlik doğrulama durumunu gösterir; özel finansal veri **yoktur**. Durum makinesi (`frontend/owner/js/mfa_flow.js`) aal2 + doğrulanmış faktör yoksa `canRenderPrivate()` = false döndürür; hata/belirsizlik = kilitli. Bu istemci denetimi UX'tir; otorite backend sahip bağımlılığı ve (sonraki) RLS'tir.
* Eski SPA: `admin_bypass`/`mock_bypass_token`/`dev_admin` kaldırıldı ve kalıntı anahtar silinir; `getUser`/`getValidSession` aal2 olmadan `null` döner; parola girişi sonrası aal2 değilse uygulama kabuğu açılmaz; kayıt ve Google girişi istemcide kapatıldı. Eski SPA'nın tamamı güvenli **değildir** (XSS, `offline_portfolio_*`, service worker önbelleği, `_gk/_ak` yerel depolama açık risk).
* `supabase-js` `@2.117.1` olarak sabitlendi, jsDelivr ESM üzerinden yüklenir; ESM içe aktarmada SRI yoktur (tedarik zinciri riski → ileride yerel paketleme). CSP `<meta>` ile verilir (HTTP başlığı değildir; `frame-ancestors` meta ile uygulanamaz, `X-Frame-Options: DENY` vercel.json'da genel başlık). `vercel.json` JS dosyalarına uzun süreli `immutable` önbellek verir: kimlik doğrulama kodu güncellemesinde önbellek temizliği/ad değişikliği gerekir.
* Gerçek tarayıcıda, gerçek Supabase projesinde uçtan uca deneme **yapılmadı**; testler sahte `auth` yüzeyi ile deterministiktir.

## 9. Çıkış kriterleri (25D1C öncesi)

Hesap oluşturuldu; UUID gizli ortamda; `jwks` modu açık; iki TOTP faktörü kayıtlı ve ayrı ayrı sınanmış; §5 çıktısı tamamen olumlu; kurtarma prosedürü branch'te denendi; legacy anahtar iptal planı ve anahtar geçişi doğrulandı; §7 sayımları sıfır veya karara bağlandı. Bunlar sağlanmadan Migration 032 uygulanmaz.
