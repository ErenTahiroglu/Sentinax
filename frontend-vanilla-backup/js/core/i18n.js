/**
 * i18n — Çoklu Dil Desteği (TR / EN)
 */
const TRANSLATIONS = {
    tr: {
        // Header
        "header.badge": "AI Destekli Analiz",
        "header.title": "Portföy Analiz Platformu",
        "header.subtitle": "Hisselerinizi ve fonlarınızı yapay zeka ile saniyeler içinde derinlemesine analiz edin.",
        // Sidebar
        "sidebar.settings": "ANALİZ AYARLARI",
        "sidebar.compliance": "Uygunluk Taraması",
        "sidebar.compliance.desc": "İslami uygunluk analizi",
        "sidebar.financial": "Finansal Analiz",
        "sidebar.financial.desc": "Getiri & performans",
        "sidebar.ai": "AI Yorumları",
        "sidebar.ai.desc": "Gemini ile akıllı analiz",
        "sidebar.apiKey": "Gemini API Anahtarı",
        "sidebar.avKey": "Alpha Vantage API",
        "sidebar.model": "Model Seçimi",
        "sidebar.aiNote": "⚠ Pro modeller için faturalandırma açık olmalıdır.",
        "sidebar.watchlist": "KAYITLI PORTFÖYLER",
        "sidebar.savePortfolio": "Portföy Kaydet",
        "sidebar.noWatchlist": "Henüz kayıtlı portföy yok",
        "sidebar.quickStart": "Hızlı Başlangıç",
        "sidebar.quickStartDesc": "ABD (NYSE/NASDAQ) ve Türkiye (BIST/TEFAS) hisselerini & fonlarını aynı anda analiz edebilirsiniz.",
        "sidebar.advanced": "GELİŞMİŞ AYARLAR (PV)",
        "sidebar.profMode": "Profesyonel Mod",
        "sidebar.profModeDesc": "Kompakt veri görünümü",
        "sidebar.initBalance": "Başlangıç Bakiyesi (₺/$)",
        "sidebar.monthlyCont": "Aylık Katkı Payı",
        "sidebar.rebalance": "Yeniden Dengeleme",
        "sidebar.rebalance.none": "Yok (Buy & Hold)",
        "sidebar.rebalance.monthly": "Aylık",
        "sidebar.rebalance.quarterly": "Çeyreklik",
        "sidebar.rebalance.yearly": "Yıllık",
        "sidebar.pvNote": "PV simülasyonları geçmiş 5 yıla dayalıdır. Rakamlar nominaldir.",
        // Input & Tabs
        "tab.manual": "Manuel Giriş",
        "tab.file": "Dosya Yükle",
        "tab.wizard": "AI Sihirbazı",
        "input.tickers": "Hisse/ETF Sembolleri",
        "input.tickerPlaceholder": "Örnek: AAPL, MSFT, TSLA veya THYAO, AKB, KCHOL...",
        "input.tickerHelper": "Virgülle veya boşlukla ayırarak girebilirsiniz.",
        "input.tickerHelperBold": "ABD ve Türkiye (BIST/TEFAS) hisse/fonları aynı anda analiz edilebilir.",
        "input.file": "Dosya Yükle",
        "input.fileFormats": "Desteklenen formatlar: .txt, .csv, .xlsx, .docx",
        "input.fileDrop": "Sürükle bırak veya",
        "input.fileBrowse": "göz at",
        "input.or": "VEYA",
        // Buttons
        "btn.analyze": "Portföyü Analiz Et",
        "btn.compare": "Karşılaştır",
        "btn.share": "Paylaş",
        "btn.export": "Dışa Aktar",
        // Results
        "results.title": "Analiz Sonuçları",
        "results.summary": "Portföy Özeti",
        "results.weightedReturn": "Ağırlıklı Getiri (5Y)",
        "results.comparison": "Karşılaştırma",
        "results.sectors": "Sektör Dağılımı",
        "results.correlation": "Korelasyon Matrisi",
        "results.heatmap": "Portföy Isı Haritası",
        "results.scenarios": "Senaryo & Stres Testleri",
        "results.optimization": "Portföy Optimizasyonu",
        // Table headers
        "th.ticker": "Hisse / Fon",
        "th.market": "Pazar",
        "th.weight": "Ağırlık",
        "th.price": "Son Fiyat",
        "th.compliance": "Arındırma Oranı",
        "th.status": "Durum",
        // Cards
        "hero.score": "Portföy Skoru",
        "hero.best": "En İyi Hisse",
        "hero.risk": "Risk Seviyesi",
        "card.returns": "Dönemsel Getiriler",
        "card.chart": "Yıllık Getiri Grafiği",
        "card.ai": "AI Yorumu",
        "card.technicals": "Teknik Göstergeler",
        // Tooltips
        "tooltip.pe": "Fiyat/Kazanç Oranı. Hissenin ne kadar pahalı/ucuz olduğunu gösterir.",
        "tooltip.pb": "Piyasa Değeri / Defter Değeri. Şirketin özsermayesine oranla fiyatı.",
        "tooltip.beta": "Oynaklık ölçüsü. Piyasa 1'dir. 1'den büyükse piyasadan daha hareteklidir.",
        // Loader
        "loader.text": "Analiz ediliyor...",
        "progress.preparing": "Hazırlanıyor...",
        // Toasts
        "toast.analysisComplete": "hisse başarıyla analiz edildi",
        "toast.noAnalysis": "Lütfen en az bir analiz türü seçin.",
        "toast.noApiKey": "AI yorumları için Gemini API anahtarı gereklidir.",
        "toast.noTickers": "Lütfen hisse sembollerini girin veya dosya yükleyin.",
        "toast.exported": "Dosya indirildi!",
        "toast.exporting": "dosyası oluşturuluyor...",
        "toast.compareMin": "Karşılaştırma için en az 2 hisse gerekli",
        "toast.saved": "kaydedildi",
        "toast.loaded": "yüklendi",
        "toast.deleted": "silindi",
        "toast.enterTickers": "Önce hisse sembolleri girin",
        "toast.portfolioName": "Portföy adı:",
        // Monte Carlo
        "monte.title": "Monte Carlo Simülasyonu (1 Yıl)",
        // Theme
        "theme.toggle": "Tema",
        // Language
        "lang.toggle": "Language",
    },
    en: {
        "header.badge": "AI-Powered Analysis",
        "header.title": "Portfolio Analysis Platform",
        "header.subtitle": "Analyze your stocks and funds in-depth with artificial intelligence in seconds.",
        "sidebar.settings": "ANALYSIS SETTINGS",
        "sidebar.compliance": "Compliance Scan",
        "sidebar.compliance.desc": "Islamic compliance analysis",
        "sidebar.financial": "Financial Analysis",
        "sidebar.financial.desc": "Returns & performance",
        "sidebar.ai": "AI Commentary",
        "sidebar.ai.desc": "Smart analysis with Gemini",
        "sidebar.apiKey": "Gemini API Key",
        "sidebar.avKey": "Alpha Vantage API",
        "sidebar.model": "Model Selection",
        "sidebar.aiNote": "⚠ Billing must be enabled for Pro models.",
        "sidebar.watchlist": "SAVED PORTFOLIOS",
        "sidebar.savePortfolio": "Save Portfolio",
        "sidebar.noWatchlist": "No saved portfolios",
        "sidebar.quickStart": "Quick Start",
        "sidebar.quickStartDesc": "You can analyze US (NYSE/NASDAQ) and Turkey (BIST/TEFAS) stocks & funds simultaneously.",
        "sidebar.advanced": "Advanced Settings (PV Simulation)",
        "sidebar.profMode": "Professional Mode",
        "sidebar.profModeDesc": "Compact data view",
        "sidebar.initBalance": "Initial Balance ($/₺)",
        "sidebar.monthlyCont": "Monthly Contrib.",
        "sidebar.rebalance": "Rebalancing",
        "sidebar.rebalance.none": "None (Buy & Hold)",
        "sidebar.rebalance.monthly": "Monthly",
        "sidebar.rebalance.quarterly": "Quarterly",
        "sidebar.rebalance.yearly": "Yearly",
        "sidebar.pvNote": "PV simulations are based on the past 5 years. Figures are nominal.",
        "tab.manual": "Manual Entry",
        "tab.file": "Upload File",
        "tab.wizard": "AI Wizard",
        "input.tickers": "Stock/ETF Symbols",
        "input.tickerPlaceholder": "Example: AAPL, MSFT, TSLA or THYAO, AKB, KCHOL...",
        "input.tickerHelper": "Separate with commas or spaces.",
        "input.tickerHelperBold": "US and Turkey (BIST/TEFAS) tickers can be analyzed together.",
        "input.file": "Upload File",
        "input.fileFormats": "Supported formats: .txt, .csv, .xlsx, .docx",
        "input.fileDrop": "Drag & drop or",
        "input.fileBrowse": "browse",
        "input.or": "OR",
        "btn.analyze": "Analyze Portfolio",
        "btn.compare": "Compare",
        "btn.share": "Share",
        "btn.export": "Export",
        "results.title": "Analysis Results",
        "results.summary": "Portfolio Summary",
        "results.weightedReturn": "Weighted Return (5Y)",
        "results.comparison": "Comparison",
        "results.sectors": "Sector Distribution",
        "results.correlation": "Correlation Matrix",
        "results.heatmap": "Portfolio Heatmap",
        "results.scenarios": "Scenario & Stress Tests",
        "results.optimization": "Portfolio Optimization",
        "results.pvSim": "Portfolio Simulation (PV Risk Analysis)",
        "results.pvSimDesc": "Risk and return analysis based on 5-year historical performance.",
        "th.ticker": "Ticker / Fund",
        "th.market": "Market",
        "th.weight": "Weight",
        "th.price": "Last Price",
        "th.compliance": "Purification Ratio",
        "th.status": "Status",
        "hero.score": "Portfolio Score",
        "hero.best": "Top Performer",
        "hero.risk": "Risk Level",
        "card.returns": "Periodic Returns",
        "card.chart": "Yearly Return Chart",
        "card.ai": "AI Commentary",
        "card.technicals": "Technical Indicators",
        "tooltip.pe": "Price to Earnings ratio. Indicates how expensive/cheap the stock is.",
        "tooltip.pb": "Price to Book ratio. The market's valuation of the company relative to its book value.",
        "tooltip.beta": "Measure of volatility. Market beta is 1. >1 means more volatile.",
        "loader.text": "Analyzing...",
        "progress.preparing": "Preparing...",
        "toast.analysisComplete": "stocks successfully analyzed",
        "toast.noAnalysis": "Please select at least one analysis type.",
        "toast.noApiKey": "Gemini API key is required for AI commentary.",
        "toast.noTickers": "Please enter ticker symbols or upload a file.",
        "toast.exported": "File downloaded!",
        "toast.exporting": "file is being created...",
        "toast.compareMin": "At least 2 stocks required for comparison",
        "toast.saved": "saved",
        "toast.loaded": "loaded",
        "toast.deleted": "deleted",
        "toast.enterTickers": "Enter ticker symbols first",
        "toast.portfolioName": "Portfolio name:",
        "monte.title": "Monte Carlo Simulation (1 Year)",
        "theme.toggle": "Theme",
        "lang.toggle": "Dil",
    },
};

let currentLang = localStorage.getItem("lang") || "tr";
window.currentLang = currentLang;

window.t = t;
function t(key) {
    return TRANSLATIONS[currentLang]?.[key] || TRANSLATIONS.tr[key] || key;
}

window.setLanguage = setLanguage;
function setLanguage(lang) {
    currentLang = lang;
    localStorage.setItem("lang", lang);
    document.querySelectorAll("[data-i18n]").forEach((el) => {
        const key = el.getAttribute("data-i18n");
        if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
            el.placeholder = t(key);
        } else {
            el.textContent = t(key);
        }
    });
    // Update the language button text
    const langBtn = document.getElementById("lang-toggle-btn");
    if (langBtn) {
        // Preserve the icon if it exists
        const icon = langBtn.querySelector("i");
        langBtn.textContent = t("lang.toggle");
        if (icon) langBtn.prepend(icon);
    }
}

window.toggleLanguage = toggleLanguage;
function toggleLanguage() {
    setLanguage(currentLang === "tr" ? "en" : "tr");
}

// Get current language helper
window.getLang = getLang;
function getLang() {
    return currentLang;
}
