import { BaseComponent } from './BaseComponent.js';
import { appState } from '../core/appState.js';
import { showToast } from '../utils.js';

export class ToolbarComponent extends BaseComponent {
    constructor(analysisService) {
        super();
        this.analysisService = analysisService;
        this.bindEvents();
    }

    bindEvents() {
        const analyzeBtn = document.getElementById("analyze-btn");
        const fileInput = document.getElementById("file-upload-input");
        const tickerInput = document.getElementById("ticker-input");

        analyzeBtn?.addEventListener("click", () => this.handleAnalyze());
        
        fileInput?.addEventListener("change", (e) => this.handleFileSelect(e));
        
        tickerInput?.addEventListener("keypress", (e) => {
            if (e.key === "Enter") this.handleAnalyze();
        });

        // Reactive switches
        document.getElementById("ui-mode-toggle")?.addEventListener("change", (e) => {
            appState.viewMode = e.target.checked ? "pro" : "beginner";
        });

        document.getElementById("check-islamic-toggle")?.addEventListener("change", (e) => {
            appState.isHalalOnly = e.target.checked;
        });

        this.subscribe(appState);
    }

    onStateChange(prop, val) {
        console.log(`[ToolbarComponent] State Change: ${prop} =`, val);
        const progressContainer = document.getElementById("progress-container");
        const progressText = document.getElementById("progress-text");
        const progressFill = document.getElementById("progress-fill");

        if (prop === 'isAnalyzing') {
            document.getElementById("analyze-btn").disabled = val;
            progressContainer?.classList.toggle("hidden", !val);
        }

        if (prop === 'analysisStatusMessage' && progressText) {
            progressText.textContent = val;
        }

        if (prop === 'analysisProgress' && progressFill) {
            progressFill.style.width = `${val}%`;
        }
    }

    async handleAnalyze() {
        const text = document.getElementById("ticker-input")?.value.trim();
        if (!text) return showToast("Hisse sembolü giriniz", "warning");

        const tickers = text.split(/[\s,;]+/).filter(t => t).map(t => t.toUpperCase());
        const payload = {
            tickers,
            use_ai: document.getElementById("use-ai-toggle")?.checked,
            api_key: document.getElementById("api-key")?.value,
            check_islamic: appState.isHalalOnly,
            check_financials: document.getElementById("check-financials-toggle")?.checked,
            model: document.getElementById("model-select")?.value || "gemini-2.5-flash",
            lang: appState.lang
        };

        this.analysisService.executeAnalysis(payload);
    }

    async handleFileSelect(e) {
        const file = e.target.files[0];
        if (!file) return;

        // Delegate to a service or implement here
        // For simplicity and 50-line rule, we'll keep it here or in a helper
        const { runFileAnalysis } = await import('../network/api_file.js');
        runFileAnalysis(file, (tickers) => {
            document.getElementById("ticker-input").value = tickers.join(", ");
            this.handleAnalyze();
        });
    }
}
