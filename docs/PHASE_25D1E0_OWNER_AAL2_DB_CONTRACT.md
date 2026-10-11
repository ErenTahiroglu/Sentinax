# Phase 25D1E0 — Owner/AAL2 veritabanı sözleşmesi (Migration 032 ADAYI, çevrimdışı)

Durum: **aday, uygulanmadı.** Dosyalar `supabase/candidates/` altındadır, `supabase/migrations/` altında **değildir**: Supabase CLI/Preview/GitHub entegrasyonu yalnızca `migrations/` dizinini alır; bir merge'ün bağlı projeye kendiliğinden ulaşması engellenir. Terfi (promotion) ayrı, onaylı bir kontrol noktasıdır (operatör envanteri + Red Team kararı sonrası). Canlı Supabase, canlı grant'lar ve canlı owner kimliği **UNKNOWN**.

## Dosyalar
- `supabase/candidates/032_owner_aal2_restrictive_rls.sql` — aday (engelleyici kontroller + `private_config` + RESTRICTIVE politikalar + yalnızca daraltan REVOKE'lar).
- `supabase/candidates/032_owner_aal2_rollback.sql` — yalnızca operatör, `sentinax.rollback_confirm` onayı olmadan çalışmaz; politikaları/yardımcıyı kaldırır, **yetki geri VERMEZ**.
- `supabase/candidates/032_operator_live_inventory.sql` — yalnızca SELECT; **ajan tarafından çalıştırılmaz**.
- `backend/tests/test_owner_aal2_database_postgres.py` — 175 gerçek PG16 testi (CI'da mevcut tek-kullanımlık postgres:16 servisi).

## A. Tasarım
- `private_config.owner_identity`: tekil (singleton) tablo, boşsa **her şey reddedilir**; RLS açık, hiçbir API rolünde (service_role dahil) şema/tablo yetkisi yok. Operatör, gerçek hesap açıldıktan sonra **elle** doldurur (aday bunu yapmaz, UUID içermez).
- `private_config.owner_aal2()`: `SECURITY DEFINER`, `SET search_path = ''`, `STABLE`, boolean döner; yalnızca `authenticated` çalıştırabilir, ama şema yetkisi olmadığından **adıyla çağıramaz** (yalnızca politika ifadesinden erişir) → owner kimliği sorgulanamaz. `public` şemasında olmadığı için PostgREST RPC değildir.
- Koşul (backend `private_access.py` ile aynı): `auth.uid() = owner_id` **ve** `aal = 'aal2'` **ve** `amr` bir dizi ve içinde `{"method":"totp","timestamp":<sayı>}` nesnesi. Boş/yanlış/eksik/biçimsiz her talep reddedilir.
- 8 sahip tablosuna tek `owner_aal2_required` politikası: `AS RESTRICTIVE FOR ALL TO authenticated`. Kısıtlayıcı politika yalnızca daraltır; izin vermez.
- Yalnızca REVOKE (hiçbir yetki genişlemez): `anon` sahip tablolarında tüm yetkileri ve 3 sahip RPC'sinde (`commit_portfolio_import_claim`, `_batch`, `lookup_portfolio_transaction_external_identity`) EXECUTE'u kaybeder (012/016/017 yalnızca PUBLIC ve/veya authenticated'ı kaldırmıştı; `anon` varsayılan Supabase grant'ıyla kalıyordu); `authenticated` bu tablolarda TRUNCATE/REFERENCES/TRIGGER'ı kaybeder.
- Dokunulmayanlar: ledger yazıcı modeli (`portfolio_transactions` için authenticated INSERT/UPDATE/DELETE yok; service_role ekleme + tersine çevirme), permissive politikalar, varsayılan ACL, service_role yetkileri, referans/PIT tabloları, `user_profiles`.

## B. Çevrimdışı envanter (001–031 yeniden oynatımı, Supabase tarzı varsayılan grant'lar)
| Sınıf | Tablolar | Not |
|---|---|---|
| Sahip-özel (RESTRICTIVE eklendi) | portfolios, portfolio_accounts, cash_buckets, investment_goals, planned_contributions (ALL, `owner_id = auth.uid()`), portfolio_transactions, portfolio_fee_tax_attribution_events, portfolio_import_claim_bindings (yalnızca SELECT) | önceki durum: `anon` ve `authenticated` tüm tablo yetkilerine sahipti, tek bariyer RLS idi |
| Yalnızca servis | private_scheduler_runs, private_scheduler_run_transitions, learning_evidence_bindings, rate_limits | API rollerinde yetki yok |
| Herkese açık referans | allowed_assets, macro_observations, macro_series | SELECT politikası; yazma yetkileri hâlâ geniş (yalnızca RLS bariyeri) |
| Authenticated-okuma referans | instruments, corporate_actions, provider_aliases, normalized_observations, raw_provider_snapshots, sec_* | **her** authenticated kullanıcı okur; canlıda kayıt açık mı UNKNOWN |
| Legacy kullanıcı-kapsamlı | user_profiles | `public` INSERT/UPDATE politikaları (`user_id = auth.uid()`); Vault işaretçisi, finansal değil; ayrı karar |
| **Migrasyon dışı (UNKNOWN)** | user_settings, paper_trades, portfolio_snapshots, legacy_portfolio_watchlists, alerts, llm_usage_logs, user_events | yalnızca `infrastructure/database/*.sql` içinde; canlıda var mı bilinmiyor → aday **uygulanmayı reddeder** (`owner_aal2_blocker`) |

**UNKNOWN hücreler:** canlı tablo/grant/politika/varsayılan ACL, migrasyon dışı tabloların varlığı, görünümler, canlı `auth.jwt()` `amr` içeriği, canlı kayıt (signup) ayarı, canlıda 028'in varsayılan ACL etkisi. Hepsi `032_operator_live_inventory.sql` çıktısıyla kapanır.

## C. Kanıt (yerel gerçek PostgreSQL 16.15, CI'da postgres:16)
- RED: aday yokken 140 başarısız / 34 geçen (zayıf kimlikler sahip satırlarını okuyor; diğer kullanıcı yazabiliyor; `anon` tam yetkili ve RPC'leri çalıştırabiliyor; dosyalar yok).
- GREEN: 175 geçti. Kapsam: 12 zayıf talep × 8 tablo okuma, yazma matrisi (önceden izinli işlemler birebir korunur), boş yapılandırma = ret, anon/authenticated yetki daralması, **hiçbir yetki genişlemez** (tablo/sütun/fonksiyon/sekans/şema anlık görüntüsü farkı yalnızca authenticated→helper EXECUTE), politika kataloğu, SECURITY DEFINER sabitlemesi, stok Postgres varsayılan ACL'sinde de kapalı, service_role BYPASSRLS'in RLS koruması olarak raporlanmaması, ledger/reversal korunumu, idempotent kurulum, onaylı geri alma, atomik engelleyiciler (legacy tablo ×7, görünüm, RLS kapalı, `auth.jwt()` yok).
- Mutasyon kontrolleri (aday bozulur → test kümesi başarısız): aal2 kontrolü silindi, politika PERMISSIVE yapıldı, helper PUBLIC'e açık, anon REVOKE silindi, blokaj silindi, search_path sabiti silindi, service_role'e şema yetkisi, amr yöntem kontrolü gevşetildi, şema/tablo REVOKE'ları, RLS kapatıldı, authenticated TRUNCATE REVOKE'u, RPC REVOKE'u.
- Sınırlar: fixture Supabase değildir; `auth.uid()/jwt()` vekilleri PostgREST GUC'larını taklit eder (doğrudan SQL oturumu GUC'u kendisi ayarlayabilir; ağ yolu yok); `service_role` RLS'i atlar, korumayı FastAPI owner/AAL2 katmanı sağlar.

## D. Operatör-yalnız adımlar (bu kontrol noktasında YÜRÜTÜLMEZ)
1. `032_operator_live_inventory.sql`'i (salt SELECT) canlı projede çalıştır; çıktıyı (sır içermez) kaydet = pre-032 anlık görüntüsü; Red Team'e ver.
2. Migrasyon dışı tablolar varsa her biri için sınıflandırma kararı (kaldır / sahip-özel / servis-yalnız) ayrı kontrol noktasında verilir; aday ancak bundan sonra terfi eder.
3. Gerçek sahip hesabı + iki doğrulanmış TOTP sonrası `INSERT INTO private_config.owner_identity ...` operatör tarafından yapılır.
4. Geri alma: yalnızca `sentinax.rollback_confirm` ile; yetkiler anlık görüntüden bilinçli geri verilir.

## E. Sonraki en küçük dilim
Özel API/arayüz **sözleşmesi** (canlı altyapı gerektirmez): `require_private_owner` arkasında yalnızca-okuma özel portföy özeti için tiplenmiş zarf (`UNAVAILABLE`/boş durum, kaynak/as-of alanları, MOCK etiketi) + Deep Ocean statik kabuk/boş durumlar. Gerçek veri, gerçek kimlik bilgisi ve canlı DB yok.
