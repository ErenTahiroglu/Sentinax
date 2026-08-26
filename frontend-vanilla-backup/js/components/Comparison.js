// ═══════════════════════════════════════
// COMPARISON MODULE
// ═══════════════════════════════════════

export async function showComparison() {
    const lastResults = window.lastResults || (typeof AppState !== "undefined" && AppState.results) || [];
    if (!lastResults || lastResults.length < 2) { 
        if (typeof showToast === "function") showToast("Karşılaştırmak için en az 2 hisse olmalı", "warning"); 
        return; 
    }
    const view = document.getElementById("comparison-view");
    const content = document.getElementById("comparison-content");
    if (!view || !content) return;

    view.classList.remove("hidden");

    const fmtNum = (val, suffix = "") => (val !== undefined && val !== null) ? `${val}${suffix}` : "-";
    const formatMarketCap = (cap) => typeof window.formatMarketCap === "function" ? window.formatMarketCap(cap) : cap;

    const metrics = [
        ["Son Fiyat", r => r.financials?.son_fiyat?.fiyat?.toFixed(2) || "-"],
        ["Değişim", r => fmtNum(r.financials?.son_fiyat?.degisim, "%")],
        ["P/E", r => fmtNum(r.valuation?.pe)], 
        ["P/B", r => fmtNum(r.valuation?.pb)],
        ["Beta", r => fmtNum(r.valuation?.beta)], 
        ["Piyasa Değeri", r => formatMarketCap(r.valuation?.market_cap)],
        ["Temettü", r => fmtNum(r.valuation?.div_yield, "%")], 
        ["EPS", r => fmtNum(r.valuation?.eps)],
        ["ROE", r => fmtNum(r.valuation?.roe, "%")], 
        ["52H Yüksek", r => fmtNum(r.valuation?.high_52w)],
        ["52H Düşük", r => fmtNum(r.valuation?.low_52w)],
        ["RSI (14)", r => r.technicals?.rsi_14 !== undefined ? r.technicals.rsi_14 : "-"],
        ["MACD", r => r.technicals?.macd !== undefined ? r.technicals.macd : "-"],
        ["5Y Getiri", r => fmtNum(r.financials?.s5, "%")], 
        ["3Y Getiri", r => fmtNum(r.financials?.s3, "%")],
        ["Sharpe", r => fmtNum(r.financials?.risk?.sharpe_ratio)], 
        ["Max DD", r => fmtNum(r.financials?.risk?.max_drawdown, "%")],
        ["Sektör", r => r.sector || "-"],
    ];
    
    const tickers = lastResults.filter(r => !r.error);
    const { createComparisonTable } = await import('./CardComponent.js');
    content.innerHTML = createComparisonTable(tickers, metrics);
    view.scrollIntoView({ behavior: "smooth" });
}
