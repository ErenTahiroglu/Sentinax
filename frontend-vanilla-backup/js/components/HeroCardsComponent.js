// ═══════════════════════════════════════
// HERO CARDS COMPONENT
// ═══════════════════════════════════════

/** Portföy verisi yokken Premium Empty State gösterir */
export function showEmptyPortfolioState() {
    // Eğer zaten empty state varsa tekrar ekleme
    if (document.getElementById("portfolio-empty-state")) return;

    // Sonuç alanını göster (ama içini değiştir)
    const resultsSection = document.getElementById("results");
    if (resultsSection) resultsSection.classList.remove("hidden");

    // Hero cards'ı gizle
    const heroCards = document.getElementById("hero-cards");
    if (heroCards) heroCards.classList.add("hidden");

    // Results grid ve toolbar gizlensin, sadece empty state görünsün
    const resultsGrid = document.getElementById("results-grid");
    if (resultsGrid) resultsGrid.innerHTML = "";

    // Mevcut empty state yoksa oluştur
    const emptyState = document.createElement("div");
    emptyState.id = "portfolio-empty-state";
    emptyState.style.cssText = [
        "display:flex", "flex-direction:column", "align-items:center", "justify-content:center",
        "gap:1.5rem", "padding:4rem 2rem", "text-align:center",
        "background:rgba(99,102,241,0.03)", "border:1px dashed rgba(99,102,241,0.2)",
        "border-radius:20px", "margin:2rem 0",
        "backdrop-filter:blur(8px)", "animation:slideUpFade 0.4s ease"
    ].join(";");

    emptyState.innerHTML = `
        <div style="
            width:72px;height:72px;
            background:linear-gradient(135deg,rgba(99,102,241,0.12),rgba(139,92,246,0.12));
            border:1px solid rgba(99,102,241,0.2);
            border-radius:22px;
            display:flex;align-items:center;justify-content:center;
            box-shadow:0 8px 32px rgba(99,102,241,0.12);
        ">
            <i class="fas fa-chart-pie" style="font-size:2rem;color:rgba(99,102,241,0.6);"></i>
        </div>
        <div>
            <h3 style="margin:0 0 0.5rem;font-size:1.25rem;font-weight:800;color:var(--text-main);letter-spacing:-0.3px;">
                Portföyünüz şu an boş
            </h3>
            <p style="margin:0;font-size:0.9rem;color:var(--text-muted);max-width:320px;line-height:1.6;">
                Hisse, ETF veya fon ekleyerek yapay zeka destekli analizini başlat.
            </p>
        </div>
        <div style="display:flex;gap:0.75rem;flex-wrap:wrap;justify-content:center;">
            <button id="empty-state-cta-btn" style="
                padding:0.75rem 1.5rem;
                background:linear-gradient(135deg,#6366f1,#8b5cf6);
                border:none;border-radius:12px;
                color:#fff;font-size:0.9rem;font-weight:700;
                cursor:pointer;
                display:flex;align-items:center;gap:8px;
                box-shadow:0 4px 16px rgba(99,102,241,0.35);
                transition:transform 0.15s,box-shadow 0.15s;
            "
            onmouseenter="this.style.transform='translateY(-2px)';this.style.boxShadow='0 8px 24px rgba(99,102,241,0.45)'"
            onmouseleave="this.style.transform='';this.style.boxShadow='0 4px 16px rgba(99,102,241,0.35)'"
            onclick="window._triggerAddAsset()">
                <i class="fas fa-plus-circle"></i> İlk Varlığını Ekle
            </button>
            <button style="
                padding:0.75rem 1.5rem;
                background:rgba(255,255,255,0.04);
                border:1px solid var(--glass-border);border-radius:12px;
                color:var(--text-muted);font-size:0.9rem;font-weight:600;
                cursor:pointer;
                transition:background 0.2s,color 0.2s;
            "
            onmouseenter="this.style.background='rgba(255,255,255,0.08)';this.style.color='var(--text-main)'"
            onmouseleave="this.style.background='rgba(255,255,255,0.04)';this.style.color='var(--text-muted)'"
            onclick="document.getElementById('ticker-input')?.focus()">
                <i class="fas fa-keyboard"></i> Manuel Giriş
            </button>
        </div>
        <p style="font-size:0.75rem;color:var(--text-muted);margin:0;">
            <i class="fas fa-bolt" style="color:var(--primary);"></i>
            Örnek: <code style="background:rgba(99,102,241,0.1);padding:2px 6px;border-radius:4px;color:var(--primary);">AAPL, MSFT, THYAO</code> yaz ve analizi başlat
        </p>
    `;

    if (resultsGrid) {
        resultsGrid.parentNode.insertBefore(emptyState, resultsGrid);
    } else if (resultsSection) {
        resultsSection.appendChild(emptyState);
    }
}

/** Eğer gösterilmişse Empty State'i kaldır */
export function hideEmptyPortfolioState() {
    const el = document.getElementById("portfolio-empty-state");
    if (el) el.remove();
}

export function updateHeroCards(results, extras) {
    const cardsContainer = document.getElementById("hero-cards");
    const scoreVal = document.getElementById("hero-score-val");
    const bestVal = document.getElementById("hero-best-val");
    const riskVal = document.getElementById("hero-risk-val");

    if (!cardsContainer) return; // Guard

    if (!results || results.length === 0) {
        cardsContainer.classList.add("hidden");
        // Empty state'i kaldır (analiz başlatılmadan önceki duruma dön)
        hideEmptyPortfolioState();
        return;
    }

    // Analiz verisi geldi — empty state'i kaldır
    hideEmptyPortfolioState();

    cardsContainer.classList.remove("hidden");

    // — Stale Data Badge (Fix 6) —
    const isStale = results.some(r => r.is_stale === true);
    const existingBadge = cardsContainer.querySelector('.stale-data-badge');
    if (existingBadge) existingBadge.remove();
    if (isStale) {
        const badge = document.createElement('div');
        badge.className = 'stale-data-badge';
        badge.title = 'Canlı piyasa verisi alınamadı, önceki veriler gösteriliyor.';
        badge.innerHTML = '⚠️ <small>Veriler gecikmeli</small>';
        badge.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:0.72rem;color:var(--warning);padding:4px 10px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2);border-radius:999px;margin-bottom:0.75rem;width:fit-content;';
        cardsContainer.insertBefore(badge, cardsContainer.firstChild);
    }

    // Portföy Skoru
    if (extras && extras.weighted_return_5y !== undefined) {
        const val = parseFloat(extras.weighted_return_5y);
        const color = val >= 0 ? "var(--success)" : "var(--danger)";
        if (scoreVal) scoreVal.innerHTML = `<span style="color:${color}">%${val.toFixed(1)}</span>`;
    } else {
        if (scoreVal) scoreVal.textContent = "-";
    }

    // En İyi Hisse
    let bestStock = null;
    let maxRet = -Infinity;
    results.forEach(r => {
        if (!r.error && r.financials && r.financials.s5 !== undefined && r.financials.s5 !== null) {
            let currentRet = parseFloat(r.financials.s5);
            if (!isNaN(currentRet) && currentRet > maxRet) {
                maxRet = currentRet;
                bestStock = r.ticker;
            }
        }
    });
    
    if (bestVal) {
        if (bestStock) {
            bestVal.innerHTML = `<span class="ticker-box">${bestStock}</span> <small style="color:var(--success); font-weight:700;">(+%${maxRet.toFixed(1)})</small>`;
        } else {
            bestVal.textContent = "-";
        }
    }

    // Risk Seviyesi
    let totalRisk = 0;
    let riskCount = 0;
    results.forEach(r => {
        // Fix 4: Optional chaining to prevent crash when r.financials.risk is undefined
        const maxDD = r?.financials?.risk?.max_drawdown;
        if (!r.error && maxDD !== undefined && maxDD !== null) {
            totalRisk += Math.abs(maxDD);
            riskCount++;
        }
    });

    if (riskVal) {
        if (riskCount > 0) {
            const avgRisk = totalRisk / riskCount;
            let riskLabel = "Düşük";
            let riskColor = "var(--success)";
            let riskIcon = "fa-shield-check";
            
            if (avgRisk > 30) { 
                riskLabel = "Yüksek"; 
                riskColor = "var(--danger)"; 
                riskIcon = "fa-radiation";
            } else if (avgRisk > 15) { 
                riskLabel = "Orta"; 
                riskColor = "var(--warning)"; 
                riskIcon = "fa-exclamation-triangle";
            }
            
            riskVal.innerHTML = `<span style="color:${riskColor}">${riskLabel}</span> <small>(MaxDD: %${avgRisk.toFixed(1)})</small>`;
            
            // Update Icon based on risk
            const riskCardIcon = document.querySelector('#hero-risk-val').closest('.hero-card').querySelector('.hero-icon i');
            if (riskCardIcon) {
                riskCardIcon.className = `fas ${riskIcon}`;
                riskCardIcon.style.color = riskColor;
            }
        } else {
            riskVal.textContent = "-";
        }
    }
}
