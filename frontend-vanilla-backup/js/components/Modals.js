// ═══════════════════════════════════════
// METRIC DETAILS & MODALS MODULE
// ═══════════════════════════════════════

export const METRIC_DESCRIPTIONS = {
    pe: {
        tr: "Fiyat/Kazanç Oranı (P/E): Şirketin piyasa değerinin yıllık karına oranıdır. Şirketin her 1 TL'lik karı için piyasanın ne kadar ödemeye razı olduğunu gösterir. Düşük oran 'ucuz', yüksek oran 'büyüme beklentisi' veya 'pahalı' olarak yorumlanabilir.",
        en: "Price-to-Earnings Ratio (P/E): The ratio of a company's share price to its earnings per share. High P/E could mean that a stock's price is high relative to earnings and possibly overvalued. Conversely, a low P/E might indicate that the current stock price is low relative to earnings."
    },
    pb: {
        tr: "Piyasa Değeri/Defter Değeri (P/B): Şirketin piyasa değerinin, özsermayesine (net varlıklarına) oranıdır. 1'in altı genellikle şirketin varlıklarından daha ucuza satıldığını gösterir.",
        en: "Price-to-Book Ratio (P/B): Compares a firm's market capitalization to its book value. A P/B ratio under 1.0 is considered a good P/B value, indicating a potentially undervalued stock."
    },
    beta: {
        tr: "Beta: Hissenin piyasaya (endekse) göre oynaklığını ölçer. Beta > 1 ise hisse piyasadan daha hareketli, Beta < 1 ise daha durağandır. Risk iştahınıza göre kritik bir göstergedir.",
        en: "Beta: A measure of a stock's volatility in relation to the overall market. A beta greater than 1.0 suggests that the stock is more volatile than the market, while a beta less than 1.0 indicates it is less volatile."
    },
    sharpe: {
        tr: "Sharpe Oranı: Risk birimi başına elde edilen getiriyi ölçer. Oranın yüksek olması (özellikle 1 ve üzeri), alınan riskin karşılığının iyi bir getiriyle alındığını gösterir.",
        en: "Sharpe Ratio: Measures the performance of an investment compared to a risk-free asset, after adjusting for its risk. A higher Sharpe ratio is better."
    },
    max_dd: {
        tr: "Maximum Drawdown (Max DD): Bir varlığın zirve noktasından en dip noktasına kadar yaşadığı en büyük değer kaybıdır. Portföyün 'en kötü senaryoda' ne kadar düşebileceğini gösterir.",
        en: "Maximum Drawdown (Max DD): The maximum observed loss from a peak to a trough of a portfolio, before a new peak is attained. It's a key indicator of downside risk."
    },
    div: {
        tr: "Temettü Verimi: Şirketin dağıttığı temettünün hisse fiyatına oranıdır. Pasif gelir odaklı yatırımcılar için ana performans göstergesidir.",
        en: "Dividend Yield: A financial ratio that shows how much a company pays out in dividends each year relative to its stock price."
    },
    s5: {
        tr: "5 Yıllık Reel Getiri: Son 5 yıldaki toplam getirinin enflasyondan arındırılmış halidir. Paranızı enflasyona karşı ne kadar koruduğunuzu ve büyüttüğünüzü temsil eder.",
        en: "5-Year Real Return: The total return over the last 5 years adjusted for inflation. Represents how much you have grown your purchasing power."
    }
};

export function openMetricModal(ticker, metricKey, label, aiCommentRaw) {
    const modal = document.getElementById("metric-modal");
    const title = document.getElementById("metric-detail-title");
    const tickerEl = document.getElementById("metric-detail-ticker");
    const desc = document.getElementById("metric-static-desc");
    const aiBox = document.getElementById("metric-ai-insight");

    if (!modal || !title || !tickerEl || !desc || !aiBox) return;

    title.textContent = label;
    tickerEl.textContent = ticker;

    const lang = typeof getLang === "function" ? getLang() : "tr";
    desc.textContent = METRIC_DESCRIPTIONS[metricKey] ? METRIC_DESCRIPTIONS[metricKey][lang] : "Bu metrik hakkında detaylı bilgi bulunmuyor.";

    let insight = "Bu metrik için yapay zeka analizi henüz hazır değil veya analiz sırasında üretilmedi.";
    if (aiCommentRaw) {
        const match = aiCommentRaw.match(/<!--METRIC_INSIGHTS:\s*([\s\S]*?)\s*-->/);
        if (match) {
            try {
                const insights = JSON.parse(match[1]);
                if (insights[metricKey]) insight = insights[metricKey];
            } catch (e) { console.error("JSON parse error for metric insights:", e); }
        }
    }

    aiBox.textContent = insight;
    modal.classList.remove("hidden");
}

export function openPaywallModal() {
    const existing = document.getElementById("paywall-modal");
    if (existing) {
        existing.style.display = "flex";
        return;
    }

    const modal = document.createElement("div");
    modal.id = "paywall-modal";
    modal.className = "modal-overlay";
    modal.style = "position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); backdrop-filter: blur(10px); z-index: 2000; justify-content: center; align-items: center; display: flex;";
    
    modal.innerHTML = `
        <div class="modal-content glass-panel" style="max-width: 440px; width: 90%; padding: 2.5rem; text-align: center; border: 1px solid rgba(243, 156, 18, 0.4); background: rgba(20,20,25,0.95); animation: zoomIn 0.3s ease-out;">
            <div style="font-size: 3.5rem; color: #f39c12; margin-bottom: 1rem;"><i class="fas fa-crown"></i></div>
            <h2 style="color: #fff; font-size: 1.5rem; margin-bottom: 0.5rem; font-weight: 700;">Aylık Limitine Ulaştın!</h2>
            <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 2rem; line-height: 1.5;">Yapay zeka analizlerini kullanmaya devam etmek için Pro sürümüne yükseltebilirsin.</p>
            
            <div style="background: rgba(255,255,255,0.03); padding: 1.25rem; border-radius: 8px; margin-bottom: 2rem; border: 1px solid rgba(255,255,255,0.05);">
                 <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 0.9rem;"><span>Ücretsiz Plan:</span> <span style="color: var(--text-muted);">50k Token</span></div>
                 <div style="display: flex; justify-content: space-between; font-weight: 700; color: #f1c40f; font-size: 1rem;"><span>Pro Plan:</span> <span>1.000.000 Token ✨</span></div>
            </div>

            <button id="upgrade-pro-btn" class="btn btn-primary" style="width: 100%; padding: 14px; font-weight: 700; font-size: 1rem; background: linear-gradient(135deg, #f1c40f, #f39c12); color: #111; border: none; box-shadow: 0 4px 20px rgba(243, 156, 18, 0.4); cursor:pointer; border-radius:8px; transition: transform 0.2s;"><i class="fas fa-rocket"></i> Pro'ya Yükselt</button>
            <button id="close-paywall-btn" style="background: none; border: none; color: var(--text-muted); margin-top: 1.25rem; cursor: pointer; font-size: 0.85rem; text-decoration: underline;">Daha Sonra</button>
        </div>
    `;

    document.body.appendChild(modal);

    document.getElementById("upgrade-pro-btn").addEventListener("click", async () => {
         try {
              const session = await window.SupabaseAuth.getValidSession();
              if(!session) return;
              
              const resp = await fetch(`${window.API_BASE}/api/billing/upgrade`, {
                  method: "POST",
                  headers: { "Authorization": `Bearer ${session.access_token}` }
              });
              
              if (resp.ok) {
                   modal.style.display = "none";
                   alert("Tebrikler! Hesabınız Pro sürümüne başarıyla yükseltildi.");
                   window.location.reload();
              } else {
                   alert("Abonelik işlemi gerçekleştirilemedi.");
              }
         } catch(e) { console.error("Upgrade failed:", e); }
    });

    document.getElementById("close-paywall-btn").addEventListener("click", () => {
         modal.style.display = "none";
    });
}
