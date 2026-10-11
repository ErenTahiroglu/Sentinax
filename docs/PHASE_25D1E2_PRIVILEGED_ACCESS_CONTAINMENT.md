# Phase 25D1E2 — Ayrıcalıklı erişim kısıtlama ve sağlık ucu sertleştirmesi

Çevrimdışı güvenlik fazı; hiçbir dağıtım, canlı SQL, gerçek hesap veya anahtar işlemi yok.

## 1. Ayrıcalıklı (service-role) giriş noktası envanteri ve sınıflandırma
Service-role RLS'i atlar; RLS bu yolları korumaz. `require_private_owner` yalnızca onu kullanan rotaları korur — tüm service-role erişimini değil.
| Giriş noktası | Sınıf (kanıt) | Bu fazdaki işlem |
|---|---|---|
| `GET /api/health` (`backend/api/main.py`) | **Aktif, dışarıdan çağrılabilir, kimliksiz.** Tüketiciler: `render.yaml` healthCheckPath, `docker-compose.yml` wget healthcheck, `keep_alive_cron.sql`, eski `healthcheck.yml` (yalnızca eski Vercel/Render hostlarına), frontend `checkServerHealth` (yalnızca HTTP 200'e bakar). Önceden service-role ile `portfolios?limit=1` çağırıyor ve istisna metnini yanıtlıyordu → **doğrulanmış sorun**. | **Düzeltildi:** yalnızca süreç canlılığı, `{"status":"ok"}`, `no-store`, GET+HEAD; kimlik bilgisi/DB/ağ/istisna yok. |
| `check_llm_quota` (`backend/api/dependencies.py`) | **Etkin olmayan / gizli risk.** Çağıran yok (git grep: yalnızca atlanan `/api/news` testi dependency_override ile anıyor). Her doğrulanmış kullanıcıya service-role ile `user_settings`/`llm_usage_logs` okuyordu. Dinamik erişim yolu bulunmadı. Genel olarak sömürülebilir DEĞİL, ama yanlışlıkla içe aktarılırsa tehlikeli. | **Emekli edildi:** her çağrıda sabit 503, hiçbir G/Ç yok. Abonelik/faturalama sistemi icat edilmedi. |
| `get_supabase_client` (`backend/api/dependencies.py`) | **Etkin olmayan.** Backend'de çağıran yok (`oracle_worker`'ın aynı adlı kendi fonksiyonu ayrı). API katmanında service-role istemci üretiyordu. | **Emekli edildi** (RuntimeError). |
| `backend/infrastructure/scheduler.py` | **Etkin olmayan (içe aktaran yok).** Modül yüklenince service-role anahtarını okur; Telegram/REST kodu. | Değiştirilmedi; ayrı temizlik (UNKNOWN: harici çağıran). |
| `oracle_worker/` (`whitelist.py`, `pipeline.py`) | **Zamanlanmış/harici çalışan** (kendi docker-compose'u); service-role ile `allowed_assets` okur. Meşru servis yolu. | Değiştirilmedi. Üretim dağıtımı UNKNOWN. |
| `supabase/functions/analyze` | **Dışarıdan çağrılabilir Edge Function**; kullanıcı JWT'li istemci kimlik için, `adminClient` yalnızca oran sınırı ve Vault için. Canlı `verify_jwt` ayarı UNKNOWN. | Değiştirilmedi. |
| `PortfolioRepository` (ledger yazıcı) | **Yetkili yazma sınırı.** İstemci enjekte edilir; modül ortam okumaz/istemci üretmez. Üretimde bağlayan kod yok (bağlama UNKNOWN). | Değiştirilmedi; invariant testi gerçek sınırı doğrular (bkz. §3). |
| `render.yaml` | **Eski.** service-role, JWT secret, owner id anahtarlarını tanımlar; hedef mimari değil. | Değiştirilmedi. |
| `GET /api/metrics` | **Kimliksiz**; yalnızca WebSocket bağlantı sayısı ve zaman. Ayrıcalıklı değil ama gereksiz bilgi. | Değiştirilmedi (not). |

Başka kritik canlı sızıntı bulunmadı; bulunsaydı kapsam sessizce genişletilmeyecekti.

## 2. Sağlık ucu sözleşmesi
`GET|HEAD /api/health` → `200 {"status":"ok"}`; `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. Süreç canlılığıdır, sistem hazırlığı değildir; Redis/Supabase/Oracle gerektirmez; bağımlılık durumu uydurulmaz. Hazırlık denetimi gerekirse ayrı, erişimi kısıtlı bir tasarımla yapılmalıdır.

## 3. Mevcut invariant değişikliği (belgelenmiş)
`backend/tests/test_portfolio_transaction_write_surface_hardening_schema.py::test_canonical_repository_append_path_still_inserts_with_service_role_privilege` son satırı "`SUPABASE_SERVICE_ROLE_KEY` `backend/api/dependencies.py` içinde geçer" diyordu (güvenilir istemci kanıtı). Bu varsayım eskiydi: gerçek yazma sınırı `PortfolioRepository`'nin **enjekte edilmiş istemcisi**dir; anahtarın API katmanında okunması bir gereklilik değil bir sızıntı yüzeyiydi. Test artık (a) depo yapıcısının istemciyi enjekte aldığını, (b) depo modülünün ortam okumadığını/istemci üretmediğini, (c) API katmanında service-role kullanımı olmadığını doğrular. Ledger INSERT-only ve ekleme yolu doğrulamaları aynen korunur.

Aynı gerekçeyle iki eski invariant daha güncellendi (silinmedi, zayıflatılmadı; ilkinin amacı olan "private tabloyu hedefler" şartı artık "hiçbir tabloya dokunmaz" ile daha güçlü karşılanır):
- `test_portfolio_persistence_schema.py::test_health_check_still_targets_the_private_portfolios_table` → `test_health_check_references_no_portfolio_table_at_all`: `main.py` içinde `/rest/v1/` yok, eski `legacy_portfolio_watchlists` çarpışmasına dokunan sorgu yok.
- `test_portfolio_import_write_surface_schema.py::test_backend_uses_service_role_key`: kanıt artık gerçek sözleşme — import-commit RPC'leri yalnızca `service_role`'e açık (migration 016/017 GRANT), ledger deposu ayrıcalıklı istemciyi enjeksiyonla alır (ortam okumaz/istemci üretmez) ve `dependencies.py` anahtarı içermez.
**Süreç notu:** ilk push'ta bu iki test yerelde çalıştırılmayan CI adımlarında (Phase 12/13) vardı ve Backend Correctness Gate'i kırdı; düzeltme normal bir takip commit'iyle yapıldı ve CI'daki tüm PostgreSQL-dışı backend adımları (32) yerelde eksiksiz çalıştırıldı.

## 4. Artık riskler
Diğer service-role çağıranları (yukarıdaki tablo), `/api/metrics` açığı, CORS varsayılanları, idempotency ve WebSocket kimlik doğrulaması, canlı Edge Function ayarı, ledger yazıcının üretim kablolaması: ayrı kapılar.
