import { BaseComponent } from './BaseComponent.js';
import { appState } from '../core/appState.js';
import { createSkeletonCard } from './CardComponent.js';

export class ResultsComponent extends BaseComponent {
    constructor(analysisService) {
        super();
        this.analysisService = analysisService;
        this.container = document.getElementById("results-grid");
        this.subscribe(appState);
        this.bindEvents();
    }

    /**
     * ⚡ Event Delegation: One listener for all retry buttons
     */
    bindEvents() {
        this.container?.addEventListener("click", (e) => {
            const retryBtn = e.target.closest(".retry-btn");
            if (!retryBtn) return;

            const ticker = retryBtn.dataset.ticker;
            if (ticker) {
                console.log(`[ResultsComponent] Retrying analysis for: ${ticker}`);
                this.analysisService.executeAnalysis({ 
                    ...appState.analysisPayload, 
                    tickers: [ticker] 
                });
            }
        });
    }

    onStateChange(prop, val) {
        if (prop === 'results') this.renderResults(val);
        if (prop === 'isAnalyzing' && val) this.showSkeleton();
    }

    showSkeleton() {
        const grid = document.getElementById("results-grid");
        const resultsSection = document.getElementById("results");
        if (!grid || !resultsSection) return;

        resultsSection.classList.remove("hidden");
        grid.innerHTML = "";
        
        const tickers = appState.analysisPayload?.tickers || [];
        tickers.forEach(t => grid.appendChild(createSkeletonCard(t)));
    }

    renderResults(results) {
        const resultsSection = document.getElementById("results");
        if (!results || results.length === 0) {
            resultsSection?.classList.add("hidden");
            return;
        }

        resultsSection?.classList.remove("hidden");
        
        // Find newly added items that are not in the DOM yet
        results.forEach(item => {
            const skeleton = document.getElementById(`skeleton-${item.ticker}`);
            if (skeleton) {
                // Replace skeleton with real card
                // Note: The actual card rendering logic should be here or in a CardComponent
                this.renderSingleCard(item, skeleton);
            }
        });
    }

    renderSingleCard(item, skeleton) {
        // Create the custom element defined in AnalysisComponents.js
        const card = document.createElement('x-analysis-card');
        card.data = item;
        
        skeleton.replaceWith(card);
        
        // Manual retry binding if item has error
        if (item.error) {
            const retryBtn = card.querySelector(".retry-btn");
            retryBtn?.addEventListener("click", () => {
                this.analysisService.executeAnalysis({ 
                    ...appState.analysisPayload, 
                    tickers: [item.ticker] 
                });
            });
        }
    }
}
