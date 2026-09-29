# ADR-0001: Finansal Değişmezler (Invariants)

**Durum:** Kabul edildi
**Kaynak:** `ARCHITECTURE.md` "Temel İlkeler"

## Karar

Aşağıdaki değişmezler `ARCHITECTURE.md` "Temel İlkeler" ile aynıdır ve proje geneli için geçerlidir. Bu ADR bunları özellikle `backend/engine/private/` kodu ve `supabase/migrations/` için bağlayıcı kılar. Numaralandırma `ARCHITECTURE.md` ile aynıdır.

1. **Karar destek, emir değil.** Sistem BUY/SELL emri göndermez.
2. **Eksik veri ≠ sıfır.** `DataStatus.UNAVAILABLE` → `None`. Veri uydurulmaz.
3. **Kripto kapsam dışı.** `detect_market()` → `UNKNOWN`.
4. **PARTIAL geçerli sonuçtur.** Eksik input çökme sebebi değildir.
5. **Point-in-time bütünlüğü.** `effective_date` ≠ `retrieved_at`. Karar anında bilinmeyen veri kullanılmaz.
6. **Test izolasyonu.** `pytest-socket` ağ erişimini engeller.

## Doğrulama

- Değişmez ihlalleri testle yakalanır (`backend/tests/`); graf araçları yalnızca yapıyı gösterir, doğruluğu denetlemez.
- SQL tarafı: PIT depolama (`004`), atomik import (`015`, `017`), fee/tax attribution (`018`–`021`).

## Sonuç

Yeni migration veya engine değişikliği bu ADR ile çelişirse önce ADR güncellenir.
