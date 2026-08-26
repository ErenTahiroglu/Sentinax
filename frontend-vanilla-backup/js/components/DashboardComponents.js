import { BaseComponent } from './BaseComponent.js';

/**
 * 🏆 <x-hero-cards>
 * Portföy özet metriklerini (Skor, En İyi Hisse, Risk) gösteren otonom bileşen.
 */
export class HeroCards extends BaseComponent {
    constructor() {
        super();
        this.innerHTML = `
            <div class="hero-cards-grid hidden" id="hero-cards-inner">
                <div class="hero-card">
                    <div class="hero-icon"><i class="fas fa-star"></i></div>
                    <div class="hero-info">
                        <span class="hero-label">Portföy Skoru</span>
                        <div class="hero-value" id="hero-score-val">-</div>
                    </div>
                </div>
                <div class="hero-card">
                    <div class="hero-icon" style="color: var(--success)"><i class="fas fa-arrow-trend-up"></i></div>
                    <div class="hero-info">
                        <span class="hero-label">En İyi Hisse</span>
                        <div class="hero-value" id="hero-best-val">-</div>
                    </div>
                </div>
                <div class="hero-card">
                    <div class="hero-icon" style="color: var(--warning)"><i class="fas fa-shield-alt"></i></div>
                    <div class="hero-info">
                        <span class="hero-label">Risk Seviyesi</span>
                        <div class="hero-value" id="hero-risk-val">-</div>
                    </div>
                </div>
            </div>
        `;
        this.container = this.querySelector('#hero-cards-inner');
    }

    connectedCallback() {
        if (window.AppState) {
            this.subscribe(window.AppState);
        }
    }

    onStateChange(prop, _val) {
        if (prop === "results" || prop === "extras") {
            this.update(window.AppState.results, window.AppState.extras);
        }
    }

    update(results, extras) {
        if (!results || results.length === 0) {
            this.container.classList.add("hidden");
            return;
        }

        this.container.classList.remove("hidden");

        const scoreVal = this.querySelector('#hero-score-val');
        const bestVal = this.querySelector('#hero-best-val');
        const riskVal = this.querySelector('#hero-risk-val');

        // 1. Portföy Skoru
        if (extras && extras.weighted_return_5y !== undefined) {
            const valNum = parseFloat(extras.weighted_return_5y);
            const color = valNum >= 0 ? "var(--success)" : "var(--danger)";
            scoreVal.innerHTML = `<span style="color:${color}">%${valNum.toFixed(1)}</span>`;
        } else {
            scoreVal.textContent = "-";
        }

        // 2. En İyi Hisse
        let bestStock = null;
        let maxRet = -Infinity;
        results.forEach(r => {
            if (!r.error && r.financials && r.financials.s5 !== undefined) {
                let currentRet = parseFloat(r.financials.s5);
                if (!isNaN(currentRet) && currentRet > maxRet) {
                    maxRet = currentRet;
                    bestStock = r.ticker;
                }
            }
        });
        
        if (bestStock) {
            bestVal.innerHTML = `<span class="ticker-box">${bestStock}</span> <small style="color:var(--success); font-weight:700;">(+%${maxRet.toFixed(1)})</small>`;
        } else {
            bestVal.textContent = "-";
        }

        // 3. Risk Seviyesi
        let totalRisk = 0;
        let riskCount = 0;
        results.forEach(r => {
            const maxDD = r?.financials?.risk?.max_drawdown;
            if (!r.error && maxDD !== undefined && maxDD !== null) {
                totalRisk += Math.abs(maxDD);
                riskCount++;
            }
        });

        if (riskCount > 0) {
            const avgRisk = totalRisk / riskCount;
            let riskLabel = "Düşük";
            let riskColor = "var(--success)";
            let riskIcon = "fa-shield-check";
            
            if (avgRisk > 30) { 
                riskLabel = "Yüksek"; riskColor = "var(--danger)"; riskIcon = "fa-radiation";
            } else if (avgRisk > 15) { 
                riskLabel = "Orta"; riskColor = "var(--warning)"; riskIcon = "fa-exclamation-triangle";
            }
            riskVal.innerHTML = `<span style="color:${riskColor}">${riskLabel}</span> <small>(AvgDD: %${avgRisk.toFixed(1)})</small>`;
            
            const iconEl = riskVal.closest('.hero-card').querySelector('.hero-icon i');
            if (iconEl) {
                iconEl.className = `fas ${riskIcon}`;
                iconEl.style.color = riskColor;
            }
        } else {
            riskVal.textContent = "-";
        }
    }
}

if (!customElements.get('x-hero-cards')) customElements.define('x-hero-cards', HeroCards);
