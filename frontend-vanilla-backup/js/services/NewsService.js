// ═══════════════════════════════════════
// AI NEWS SERVICE
// ═══════════════════════════════════════

export async function loadNews(results) {
    const wrap = document.getElementById("news-wrap");
    const container = document.getElementById("news-container");

    if (!wrap || !container) return;

    // Sadece geçerli ticker listesi
    const tickers = results.filter(r => !r.error && r.ticker).map(r => r.ticker);
    if (tickers.length === 0) {
        wrap.classList.add("hidden");
        return;
    }

    wrap.classList.remove("hidden");
    const { createLoadingSpinnerCard, createMessageCard, createNewsCard } = await import('../components/CardComponent.js');
    container.innerHTML = createLoadingSpinnerCard("Yapay zeka haberleri tarayıp portföyünüz için en önemlilerini seçiyor...");

    let aKey = "";
    if (localStorage.getItem("settingsParams")) {
        try {
            const sp = JSON.parse(await decryptData(localStorage.getItem("settingsParams")));
            aKey = sp.geminiApiKey || "";
        } catch (e) { /* ignore */ }
    }
    const currModel = localStorage.getItem("ai_model") || "gemini-2.5-flash";

    try {
        let jwtToken = "";
        try {
            const session = await window.SupabaseAuth.getValidSession();
            if (session) jwtToken = session.access_token;
        } catch (e) {
            container.innerHTML = createMessageCard(`Güvenlik Hatası: ${e.message}`, "error");
            return;
        }

        const res = await fetch(`${API_BASE}/api/news`, {
            method: "POST",
            headers: { 
                "Content-Type": "application/json",
                "Authorization": `Bearer ${jwtToken}` 
            },
            body: JSON.stringify({ tickers: tickers.slice(0, 5), api_key: aKey, model: currModel, lang: getLang() })
        });

        const data = await res.json();

        if (!data.news || data.news.length === 0) {
            container.innerHTML = createMessageCard("Portföydeki şirketler için kayda değer önemli bir haber bulunamadı.");
            return;
        }

        container.innerHTML = ""; // Clear Previous
        data.news.forEach(item => {
            const cardNode = createNewsCard(item);
            container.appendChild(cardNode);
        });

    } catch (err) {
        container.innerHTML = createMessageCard(`Haberler yüklenirken hata oluştu: ${err.message}`, "error");
    }
}
