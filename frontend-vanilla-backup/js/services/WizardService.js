// ═══════════════════════════════════════
// AI WIZARD SERVICE
// ═══════════════════════════════════════

export async function runWizard() {
    const promptText = document.getElementById("wizard-input").value.trim();
    if (!promptText) { 
        showToast(t("toast.enterTickers") || "Lütfen sihirbaza bir talimat yazın", "warning"); 
        return; 
    }

    const apiKey = document.getElementById("api-key").value;
    if (!apiKey) { 
        showToast(t("toast.noApiKey") || "AI Sihirbazı için AI API Anahtarı gereklidir (Ayarlar)", "warning"); 
        return; 
    }

    const currModel = document.getElementById("model-select").value || "gemini-2.5-flash";
    const btn = document.getElementById("btn-run-wizard");
    const origText = btn.innerHTML;

    btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Sihirbaz Düşünüyor...`;
    btn.disabled = true;

    try {
        let jwtToken = "";
        try {
            const session = await window.SupabaseAuth.getValidSession();
            if (session) jwtToken = session.access_token;
        } catch (e) {
            throw new Error(e.message);
        }

        const res = await fetch(`${API_BASE}/api/wizard`, {
            method: "POST",
            headers: { 
                "Content-Type": "application/json",
                "Authorization": `Bearer ${jwtToken}`
            },
            body: JSON.stringify({ prompt: promptText, api_key: apiKey, model: currModel, lang: getLang() })
        });

        const initialData = await res.json();
        
        if (res.status === 202 && initialData.job_id) {
             // Polling devrede (Vercel kalkanı)
             btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Zeka Motoru Çalışıyor (Arkaplan)...`;
             
             try {
                 const data = await window.pollJobResult(initialData.job_id, 3000);
                 
                 if (data.portfolio && Array.isArray(data.portfolio)) {
                     const tickerString = data.portfolio.map(p => `${p.ticker}:${p.weight}`).join(", ");
                     document.getElementById("ticker-input").value = tickerString;
                     showToast("Yapay Zeka portföyünüzü hazırladı! Analiz başlıyor...", "success");
         
                     document.getElementById("ticker-input").scrollIntoView({ behavior: "smooth", block: "center" });
         
                     setTimeout(() => {
                         const analyzeBtn = document.getElementById("analyze-btn");
                         if (analyzeBtn) analyzeBtn.click();
                     }, 800);
                 } else {
                     showToast("Geçerli bir portföy döndürülemedi.", "error");
                 }
             } catch (pollErr) {
                 showToast(`Sihirbaz Hatası: ${pollErr.message}`, "error");
             }
        } else {
            throw new Error(initialData.detail || "Makine öğrenimi servisi yanıt vermedi");
        }
    } catch (err) {
        showToast(`Sihirbaz Hatası: ${err.message}`, "error");
    } finally {
        btn.innerHTML = origText;
        btn.disabled = false;
    }
}
