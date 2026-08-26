/**
 * 🧩 AnalysisComponents.js — Reactive & Autonomous Web Components
 * =============================================================
 * Bu dosya, sistemin yeni otonom kart ve tablo yapılarını içerir.
 */

import { BaseComponent } from './BaseComponent.js';

/**
 * 🎴 <x-analysis-card>
 * Otonom, "Pure Model" prensibiyle çalışan analiz kartı.
 * Sadece kendisine verilen 'data' property'sine göre render alır.
 */
export class AnalysisCard extends HTMLElement {
    constructor() {
        super();
        this._data = null;
    }

    set data(val) {
        this._data = val;
        this.render();
    }

    render() {
        if (!this._data) return;
        const res = this._data;
        const fin = res.financials || {};
        const val = res.valuation || {};
        const isIslamicMode = res.check_financials === false;

        // Status logic
        let statusText = res.status || "-";
        if (window.getLang && window.getLang() === "en") {
            if (statusText === "Uygun") statusText = "Compliant";
            else if (statusText === "Uygun Değil") statusText = "Non-Compliant";
            else if (statusText === "Katılım Fonu Değil") statusText = "Non-Participation";
        }
        const statusClass = res.status === "Uygun" ? "status-approved" : (res.status === "Uygun Değil" || res.status === "Katılım Fonu Değil" ? "status-rejected" : "");

        this.className = `result-card glass-panel ${statusClass} stagger-enter ${isIslamicMode ? 'is-islamic' : ''}`;
        
        // Handle Error State
        if (res.error) {
            this.renderError(res);
            return;
        }

        const chartId = `chart-${Math.random().toString(36).substr(2, 9)}`;
        const nameBadge = (res.full_name && res.full_name !== res.ticker) ? `<span class="ticker-fullname">${res.full_name}</span>` : "";
        const marketText = (window.getLang && window.getLang() === "tr" && res.market === "US") ? "ABD" : (res.market || "?");
        const statusBadgeFinal = statusText !== "-" ? `<span class="${statusClass}">${statusText}</span>` : "";

        // 1. Compliance Bars (Always visible if check_islamic)
        let compHTML = "";
        if (res.compliance_details) {
            const det = res.compliance_details;
            const hVal = Number(det.haram_income?.value || 0);
            const dVal = Number(det.debt?.value || 0);

            compHTML = `
                <div class="compliance-bars">
                    <div class="comp-bar-row"><span>Haram Gelir (%${hVal.toFixed(2)})</span><span>Sınır: %5</span></div>
                    <div class="comp-bar-bg"><div class="comp-bar-fill ${det.haram_income?.pass ? 'comp-pass' : 'comp-fail'}" style="width:${Math.min((hVal / 5) * 100, 100)}%"></div></div>
                    <div class="comp-bar-row" style="margin-top:0.4rem"><span>Faizli Borç (%${dVal.toFixed(2)})</span><span>Sınır: %30</span></div>
                    <div class="comp-bar-bg"><div class="comp-bar-fill ${det.debt?.pass ? 'comp-pass' : 'comp-fail'}" style="width:${Math.min((dVal / 30) * 100, 100)}%"></div></div>
                </div>`;
        }

        // 2. Metrics Grid (Only if financials enabled)
        let metricsHTML = "";
        if (!isIslamicMode) {
            metricsHTML = `<div class="metrics-grid">`;
            if (val.pe) metricsHTML += this.createMetricBox("P/E", window.fmtNum(val.pe), "pe");
            if (val.pb) metricsHTML += this.createMetricBox("P/B", window.fmtNum(val.pb), "pb");
            if (fin.son_fiyat) metricsHTML += `<div class="metric-box no-modal"><div class="metric-label">Son Fiyat</div><div class="metric-value">${window.fmtNum(fin.son_fiyat.fiyat)}</div></div>`;
            if (fin.s5 !== undefined) metricsHTML += this.createMetricBox("5Y Getiri", window.fmtNum(fin.s5, "%"), "s5", window.colorClass(fin.s5));
            metricsHTML += `</div>`;
        }

        // 3. AI Comment
        let aiHTML = "";
        if (res.ai_comment) {
            const parsedAI = typeof marked !== "undefined" ? marked.parse(res.ai_comment) : res.ai_comment;
            const aiTitle = typeof t === "function" ? t("card.ai") : "AI Yorumu";
            aiHTML = `
                <div class="collapsible-header open" onclick="toggleCollapsible(this)">
                    <h4><i class="fas fa-robot"></i> ${aiTitle}</h4>
                    <i class="fas fa-chevron-down collapse-icon"></i>
                </div>
                <div class="collapsible-body open">
                    <div class="ai-content markdown-body">${parsedAI}</div>
                </div>`;
        }

        this.innerHTML = `
            <style>
                :host(.is-islamic) .compliance-bars { grid-column: span 2; width: 100%; }
                :host(.is-islamic) .ai-content { font-size: 1.05rem; line-height: 1.7; }
                .result-card.is-islamic .compliance-bars { width: 100%; border-bottom: 2px solid rgba(255,255,255,0.05); padding-bottom: 1.5rem; margin: 1rem 0; }
                .result-card.is-islamic .ai-content { border-top: none; padding-top: 0; }
                .result-card.is-islamic .card-body { display: block; }
            </style>
            <div class="card-header">
                <div><span class="ticker-name">${res.ticker}</span>${nameBadge}</div>
                <div style="display:flex; align-items:center; gap:0.5rem">
                    <span class="market-badge">${marketText}</span>
                    ${statusBadgeFinal}
                </div>
            </div>
            <div class="card-body">
                ${compHTML}
                ${metricsHTML}
                ${!isIslamicMode ? `<div class="chart-container" id="${chartId}" style="height: 200px; margin-top: 1rem;"></div>` : ''}
                ${aiHTML}
            </div>
        `;

        // Initialize Charts if Full Mode
        if (!isIslamicMode && window.createTVChart) {
            setTimeout(() => window.createTVChart(chartId, res), 100);
        }
    }

    renderError(res) {
        this.innerHTML = `
            <div class="card-header"><span class="ticker-name">${res.ticker}</span></div>
            <div class="error-alert">
                <p><i class="fas fa-exclamation-circle"></i> ${res.error}</p>
                <button class="btn btn-outline" onclick="retryAnalysis('${res.ticker}')">Yeniden Dene</button>
            </div>
        `;
    }

    createMetricBox(label, value, key, classes = "") {
        return `
            <div class="metric-box ${classes}" onclick='openMetricModal("${this._data.ticker}", "${key}", "${label}", ${JSON.stringify(this._data.ai_comment || "")})'>
                <div class="metric-label">${label} <i class="fas fa-info-circle" style="font-size:0.65rem;opacity:0.6"></i></div>
                <div class="metric-value">${value}</div>
            </div>`;
    }
}

/**
 * 📦 <x-analysis-grid>
 * Tüm kartları yöneten konteyner.
 */
export class AnalysisGrid extends BaseComponent {
    constructor() {
        super();
        this.innerHTML = '<div class="results-grid" id="comp-results-grid"></div>';
        this.grid = this.querySelector('#comp-results-grid');
    }

    connectedCallback() {
        if (window.AppState) {
            this._unsubscribe = window.AppState.subscribe("results", (val) => {
                this.render(val);
            });
        }
    }

    disconnectedCallback() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
    }

    render(results) {
        this.grid.innerHTML = '';
        if (!results || results.length === 0) {
            this.grid.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-folder-open"></i>
                    <h3>Henüz Analiz Yok</h3>
                    <p>Portföyünüzü oluşturun veya yukarıdan hisse sembolü girerek ilk analizinizi başlatın.</p>
                </div>
            `;
            return;
        }
        results.forEach(res => {
            const card = document.createElement('x-analysis-card');
            card.data = res;
            this.grid.appendChild(card);
        });
    }
}

/**
 * 📊 <x-analysis-table>
 * Rasyonel tablo görünümü.
 */
export class AnalysisTable extends BaseComponent {
    constructor() {
        super();
        this.innerHTML = `
            <div class="summary-container glass-panel table-responsive">
                <table class="summary-table">
                    <thead>
                        <tr>
                            <th>Sembol</th>
                            <th class="pro-only">Piyasa</th>
                            <th class="pro-only">Ağırlık</th>
                            <th>Fiyat</th>
                            <th>Değişim</th>
                            <th>Arınma</th>
                            <th>Durum</th>
                            <th class="pro-only">P/E</th>
                            <th class="pro-only">P/B</th>
                            <th class="pro-only">Beta</th>
                        </tr>
                    </thead>
                    <tbody id="comp-summary-body"></tbody>
                </table>
            </div>
        `;
        this.body = this.querySelector('#comp-summary-body');
    }

    connectedCallback() {
        if (window.AppState) {
            this._unsubscribe = window.AppState.subscribe("results", (val) => {
                this.render(val);
            });
        }
    }

    disconnectedCallback() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
    }

    render(results) {
        this.body.innerHTML = '';
        if (!results || results.length === 0) {
            this.body.innerHTML = `
                <tr>
                    <td colspan="10">
                        <div class="empty-state" style="min-height: 150px; padding: 2rem;">
                            <i class="fas fa-table" style="font-size: 2rem;"></i>
                            <p>Tablo verisi bulunamadı.</p>
                        </div>
                    </td>
                </tr>
            `;
            return;
        }
        results.forEach(res => {
            const tr = document.createElement('tr');
            const fin = res.financials || {};
            const val = res.valuation || {};
            const isIslamicMode = res.check_financials === false;

            tr.innerHTML = `
                <td><div style="font-weight:700">${res.ticker}</div></td>
                <td class="pro-only"><span class="market-badge">${res.market || '?'}</span></td>
                <td class="pro-only">${res.weight || 1}</td>
                <td>${!isIslamicMode ? (window.fmtNum(fin.son_fiyat?.fiyat) || '-') : '-'}</td>
                <td>${!isIslamicMode ? (window.fmtNum(fin.son_fiyat?.degisim, '%') || '-') : '-'}</td>
                <td>%${parseFloat(res.purification_ratio || 0).toFixed(2)}</td>
                <td>${res.status || '-'}</td>
                <td class="pro-only">${!isIslamicMode ? window.fmtNum(val.pe) : '-'}</td>
                <td class="pro-only">${!isIslamicMode ? window.fmtNum(val.pb) : '-'}</td>
                <td class="pro-only">${!isIslamicMode ? window.fmtNum(val.beta) : '-'}</td>
            `;
            if (res.status === "Uygun") tr.className = "status-approved";
            else if (res.status?.includes("Değil")) tr.className = "status-rejected";
            this.body.appendChild(tr);
        });
    }
}

// Custom Elementleri Kaydet
if (!customElements.get('x-analysis-card')) customElements.define('x-analysis-card', AnalysisCard);
if (!customElements.get('x-analysis-grid')) customElements.define('x-analysis-grid', AnalysisGrid);
if (!customElements.get('x-analysis-table')) customElements.define('x-analysis-table', AnalysisTable);
