# 🤖 Otonom Arayüz Geri Bildirim Döngüsü (Puppeteer)

Bu klasör, arayüz (UI/UX) değişikliklerini otonom olarak test etmek ve görsel hataları tespit etmek için gerekli araçları barındırır.

## 🚀 Çalıştırma

Botu çalıştırmak ve ekran görüntülerini (screenshot) almak için proje kök dizininde şu komutu çalıştırın:

```bash
npm run test:ui
```

## 📂 Çıktılar

Ekran görüntüleri otomatik olarak aşağıdaki klasöre kaydedilir:
`tests/ui/screenshots/`

- **Masaüstü Görünümü**: `desktop_<timestamp>.png` (1280x800)
- **Mobil Görünüm**: `mobile_<timestamp>.png` (375x667)

---

## 🦾 Otonom İyileştirme Akışı (Self-Correction)

Bir arayüz geliştirme/güncelleme sonrası otonom akış şu şekilde kurgulanır:

1. **Kod Değişikliği**: AI (veya kullanıcı) frontend dosyalarını (`index.html`, `styles.css`, vb.) günceller.
2. **Bot Tetikleme**: `npm run test:ui` komutu çalıştırılır.
3. **Screenshot Analizi**: Alınan ekran görüntüleri AI tarafından (Vision yeteneği ile) analiz edilir.
4. **Hata Tespiti**: Kayan alanlar, taşan metinler, hizalamalar veya standart dışı tasarımlar kontrol edilir.
5. **Otonom Düzeltme**: Tespit edilen hatalar doğrudan kod üzerinde düzeltilerek 1. adıma dönülür.

> [!TIP]
> `screenshot_bot.js` içerisinde bulunan `delay` (gecikme) süresi, CSS animasyonlarının veya dinamik yüklemelerin (loading) tamamlanması için eklenmiştir. Dinamik veri akışları için bu süreyi artırabilirsiniz.
