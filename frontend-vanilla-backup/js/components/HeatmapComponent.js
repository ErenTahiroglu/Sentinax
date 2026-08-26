// ═══════════════════════════════════════
// HEATMAP (TREEMAP) COMPONENT
// ═══════════════════════════════════════

export function renderHeatmap(results) {
    const wrap = document.getElementById("portfolio-heatmap-wrap");
    const container = document.getElementById("portfolio-heatmap");
    if (!wrap || !container) return; // Guard

    container.innerHTML = "";

    const filterSelect = document.getElementById("heatmap-filter");
    const filterType = filterSelect ? filterSelect.value : "change";

    const validResults = results.filter(r => {
        if (r.error) return false;
        if (filterType === "pe") return r.valuation && r.valuation.pe !== undefined && r.valuation.pe !== null;
        if (filterType === "div") return r.valuation && r.valuation.div_yield !== undefined && r.valuation.div_yield !== null;
        return r.financials && r.financials.son_fiyat && r.financials.son_fiyat.degisim !== undefined && r.financials.son_fiyat.degisim !== null;
    });

    if (validResults.length === 0) {
        wrap.classList.remove("hidden");
        container.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1; width: 100%; min-height: 200px;">
                <i class="fas fa-th-large" style="font-size: 2rem;"></i>
                <p style="margin-top: 1rem;">Isı haritası için yeterli veri bulunamadı.</p>
            </div>
        `;
        return;
    }

    wrap.classList.remove("hidden");

    // Total weight sum for flex-basis calculations
    const totalWeight = validResults.reduce((sum, r) => sum + (r.weight || 1), 0);

    function getMetricValue(r) {
        if (filterType === "pe") return r.valuation.pe;
        if (filterType === "div") return r.valuation.div_yield;
        return r.financials.son_fiyat.degisim;
    }

    function getFormattedValue(val) {
        if (val === null || val === undefined) return "-";
        if (filterType === "pe") return val.toFixed(2);
        if (filterType === "div") return `%${val.toFixed(2)}`;
        return `${val > 0 ? '+' : ''}%${val.toFixed(2)}`;
    }

    // Calculate colors properly
    function getHeatmapColor(val) {
        if (val === null || val === undefined) return "#334155";
        if (filterType === "pe") {
            if (val <= 0) return "#991b1b"; // Negative PE is bad
            if (val < 10) return "#166534"; // Very good
            if (val < 15) return "#22c55e"; // Good
            if (val < 20) return "#86efac"; // Neutral-good
            if (val < 30) return "#fca5a5"; // Expensive
            return "#ef4444"; // Very expensive
        }
        if (filterType === "div") {
            if (val > 5) return "#166534";
            if (val > 3) return "#22c55e";
            if (val > 1) return "#86efac";
            return "#fca5a5";
        }
        // Change
        if (val > 3) return "#166534";
        if (val > 1) return "#22c55e";
        if (val > 0) return "#86efac";
        if (val > -1) return "#fca5a5";
        if (val > -3) return "#ef4444";
        return "#991b1b";
    }

    // Determine text color based on background luminance
    function getTextColorCustom(val) {
        if (val === null || val === undefined) return "white";
        if (filterType === "pe") {
            if (val >= 15 && val < 20) return "#064e3b"; // light green text is dark
            if (val >= 20 && val < 30) return "#7f1d1d"; // light red text is dark
            return "white";
        }
        if (filterType === "div") {
            if (val > 1 && val <= 3) return "#064e3b";
            if (val <= 1) return "#7f1d1d";
            return "white";
        }
        // Change
        if (val > 0 && val <= 1) return "#064e3b";
        if (val < 0 && val >= -1) return "#7f1d1d";
        return "white";
    }

    validResults.forEach(r => {
        const val = getMetricValue(r);
        const weight = r.weight || 1;
        const percentArea = (weight / totalWeight) * 100;

        const cell = document.createElement("div");
        cell.className = "heatmap-cell";

        cell.style.flex = `1 1 calc(${percentArea}% - 0.5rem)`;
        cell.style.backgroundColor = getHeatmapColor(val);
        cell.style.color = getTextColorCustom(val);

        if (percentArea < 5 && validResults.length > 5) {
            cell.title = `${r.ticker}: ${getFormattedValue(val)}`;
            cell.innerHTML = `
                <span class="hm-ticker" style="font-size: 0.70rem;">${r.ticker}</span>
            `;
        } else {
            cell.innerHTML = `
                <span class="hm-ticker" style="font-weight: 700; margin-bottom: 2px;">${r.ticker}</span>
                <span class="hm-val">${getFormattedValue(val)}</span>
            `;
            cell.title = `${r.ticker} (Ağırlık: ${weight})`;
        }

        container.appendChild(cell);
    });
}
