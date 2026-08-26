// ═══════════════════════════════════════
// TECHNICAL INDICATORS COMPONENT
// ═══════════════════════════════════════

export function renderTechnicals(tech) {
    if (!tech) return "";
    
    // t() is the global translation function from i18n
    const getTr = (key) => typeof t === "function" ? t(key) : "Teknik Analiz";

    let html = `<div class="collapsible-header" onclick="toggleCollapsible(this)"><h4><i class="fas fa-chart-area"></i> ${getTr("card.technicals")}</h4><i class="fas fa-chevron-down collapse-icon"></i></div><div class="collapsible-body"><div class="technicals-grid">`;

    // RSI
    if (tech.rsi_14 !== undefined) {
        const cls = tech.rsi_14 > 70 ? "rsi-overbought" : (tech.rsi_14 < 30 ? "rsi-oversold" : "rsi-neutral");
        const label = tech.rsi_14 > 70 ? "Aşırı Alım" : (tech.rsi_14 < 30 ? "Aşırı Satım" : "Nötr");
        html += `<div class="tech-box"><div class="tech-label">RSI (14)</div><div class="tech-value ${cls}">${tech.rsi_14}</div><div class="tech-label">${label}</div></div>`;
    }
    // MACD
    if (tech.macd !== undefined) {
        const cls = tech.macd_hist > 0 ? "macd-bullish" : "macd-bearish";
        html += `<div class="tech-box"><div class="tech-label">MACD</div><div class="tech-value ${cls}">${tech.macd}</div></div>`;
        html += `<div class="tech-box"><div class="tech-label">Sinyal</div><div class="tech-value">${tech.macd_signal}</div></div>`;
    }
    // EMA
    [20, 50, 100, 200].forEach(p => {
        const key = `ema_${p}`;
        if (tech[key] !== undefined) {
            const above = tech.last_close >= tech[key];
            html += `<div class="tech-box"><div class="tech-label">EMA ${p}</div><div class="tech-value ${above ? 'positive' : 'negative'}">${tech[key]}</div></div>`;
        }
    });
    // SMA
    [20, 50, 100, 200].forEach(p => {
        const key = `sma_${p}`;
        if (tech[key] !== undefined) {
            const above = tech.last_close >= tech[key];
            html += `<div class="tech-box"><div class="tech-label">SMA ${p}</div><div class="tech-value ${above ? 'positive' : 'negative'}">${tech[key]}</div></div>`;
        }
    });

    html += `</div></div>`;
    return html;
}
