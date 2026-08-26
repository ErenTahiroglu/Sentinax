const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');
const FRONTEND_DIR = path.join(__dirname, '..', '..', 'frontend');

// Create screenshots directory if it doesn't exist
if (!fs.existsSync(SCREENSHOT_DIR)) {
    fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
}

(async () => {
    console.log('🚀 Puppeteer Başlatılıyor...');
    const browser = await puppeteer.launch({
        headless: "new", // "new" is recommended for newer versions
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--allow-file-access-from-files']
    });
    
    const page = await browser.newPage();
    
    // Set viewport for desktop and mobile tests
    await page.setViewport({ width: 1280, height: 800 });

    const targetFile = path.join(FRONTEND_DIR, 'index.html');
    const fileUrl = `file://${targetFile}`;

    console.log(`🌐 Sayfa Açılıyor: ${fileUrl}`);
    
    try {
        await page.goto(fileUrl, { waitUntil: 'networkidle2', timeout: 30000 });
        
        console.log('⏳ Animasyonların bitmesi bekleniyor (3 saniye delay)...');
        await new Promise(resolve => setTimeout(resolve, 3000)); // Delay for animations

        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const desktopPath = path.join(SCREENSHOT_DIR, `desktop_${timestamp}.png`);
        
        console.log(`📸 Ekran Görüntüsü Alınıyor (Masaüstü): ${desktopPath}`);
        await page.screenshot({ path: desktopPath, fullPage: true });

        // Test Mobile Viewport
        console.log('📱 Mobil Görünüm Test Ediliyor...');
        await page.setViewport({ width: 375, height: 667, isMobile: true });
        await new Promise(resolve => setTimeout(resolve, 1000)); // Adjust layout
        const mobilePath = path.join(SCREENSHOT_DIR, `mobile_${timestamp}.png`);
        console.log(`📸 Ekran Görüntüsü Alınıyor (Mobil): ${mobilePath}`);
        await page.screenshot({ path: mobilePath, fullPage: true });

        console.log('\n✅ İşlem Başarıyla Tamamlandı.');
        console.log(`📁 Kayıt Yeri: ${SCREENSHOT_DIR}`);

    } catch (error) {
        console.error('❌ Hata oluştu:', error);
    } finally {
        await browser.close();
    }
})();
