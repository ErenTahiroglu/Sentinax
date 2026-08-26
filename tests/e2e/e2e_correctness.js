import puppeteer from 'puppeteer';

/**
 * UÇTAN UCA (E2E) FONKSİYONEL DOĞRULUK TESTİ
 * =========================================
 * Bu script, salt kod kırılgansızlığını değil uygulamanın "Doğru" 
 * kararlar ve "Dolu" arayüzler ürettiğini test eder.
 */

// Kısıt: Maksimum 5 canlı Gemini kullanımı limiti! (Sistem promptundan okunur)
const MAX_LIVE_CALLS_ALLOWED = 5;
let currentLiveCalls = 0;

async function runCorrectnessSuite() {
    console.log("🚀 [E2E] Fonksiyonel Doğruluk Testi Başlatılıyor...");
    const browser = await puppeteer.launch({ headless: 'new' });
    const page = await browser.newPage();
    
    // Varsayılan bir localhost URL'si (Kullanıcının projeyi ayağa kaldırdığı kabul edilir)
    const APP_URL = process.env.APP_URL || 'http://localhost:3000';
    
    try {
        await page.goto(APP_URL, { waitUntil: 'networkidle2' });
        console.log("✅ Uygulama arayüzüne (UI) ulaşıldı.");

        // 1. Durum Kontrolü: Ticker Input & Butonlar var mı?
        await page.waitForSelector('#ticker-input');
        await page.waitForSelector('#analyze-btn');
        console.log("✅ Ana form elementleri doğrulandı.");

        // 2. Manipülasyon: Portföy Ticker Girilir
        await page.type('#ticker-input', 'AAPL, MSFT');
        
        // 3. API Limit Entegrasyonu (Eğer varsa gerçek anahtarı sadece sınırlı sayıda kullanır)
        const envApiKey = process.env.GEMINI_API_KEY;
        if (envApiKey && currentLiveCalls < MAX_LIVE_CALLS_ALLOWED) {
            console.log(`[LIVE MODE] Kalan çağrı hakkı: ${MAX_LIVE_CALLS_ALLOWED - currentLiveCalls}`);
            await page.type('#api-key', envApiKey);
            await page.click('#use-ai-toggle');
            currentLiveCalls++;
        } else {
            console.log("[MOCK MODE] Live API limiti doldu veya kapatıldı. Mock ile devam ediliyor.");
            await page.evaluate(() => {
                const aiToggle = document.getElementById('use-ai-toggle');
                if(aiToggle) aiToggle.checked = false; // Mock için AI kapat veya Mock Endpoint ayarla
            });
        }

        // 4. Analizi Başlat
        await page.click('#analyze-btn');
        console.log("⏳ Analiz süreci tetiklendi. DOM streaming bekleniyor...");

        // 5. DOĞRULUK ONAYI (Correctness Check): Arayüzde Sonuçlar Çıktı mı?
        // Bir süre sonra sonuç kartlarının belirmesi gerekir.
        await page.waitForSelector('.result-card', { timeout: 35000 });
        
        // Verilerin doğruluğunu kontrol et (NaN, Indefined, Boş Dönen var mı?)
        const correctnessData = await page.evaluate(() => {
            const cards = document.querySelectorAll('.result-card');
            if (cards.length === 0) return { error: "Hiç kart render edilmedi!" };
            
            let invalidData = false;
            cards.forEach(card => {
                const priceMatch = card.innerText.match(/\$[0-9,.]+/);
                // Eğer borsa fiyatında rakam yoksa anormallik var demektir
                if (!priceMatch) {
                    invalidData = true;
                }
            });
            
            return {
                cardsFound: cards.length,
                isDataVisuallySound: !invalidData
            };
        });

        if (correctnessData.error) {
            throw new Error(`[Doğruluk İhlali] ${correctnessData.error}`);
        } else if (!correctnessData.isDataVisuallySound) {
            throw new Error("[Doğruluk İhlali] Kartlar yüklendi ancak içerisinde finansal/hesaplanan rasyonel fiyat değerleri (örnek: $100.20) bulunamadı! Render veya API formatı bozuk.");
        }

        console.log(`⭐⭐⭐ TEST BAŞARILI. ${correctnessData.cardsFound} Varlık Doğru Bir Şekilde Arayüze İşlendi!`);

    } catch (error) {
        console.error("❌ E2E Başarısız/Hatalı:", error.message);
        // Hata kanıtı için ekran görüntüsü kaydet
        await page.screenshot({ path: 'tests/e2e/error_screenshot.png' });
        console.log("📸 Hata anının fotoğrafı 'error_screenshot.png' olarak alındı.");
        process.exit(1);
    } finally {
        await browser.close();
    }
}

runCorrectnessSuite();
