/**
 * 🛠️ AnalysisService - The Bridge (Business Logic)
 * ==============================================
 * Connects ApiService with AppState and UI.
 */

import * as ApiService from '../network/api.js';
import { showToast } from '../utils.js';

export class AnalysisService {
    constructor(state) {
        this.state = state;
    }

    /**
     * Entry point for running a full portfolio analysis
     */
    async executeAnalysis(payload) {
        this.resetState();
        this.state.analysisPayload = payload;
        this.state.isAnalyzing = true;
        this.state.analysisProgress = 0;

        const callbacks = {
            onProgress: (p) => this.handleProgress(p),
            onResult: (item) => this.handleResult(item),
            onComplete: () => this.handleComplete(payload),
            onError: (err) => this.handleError(err)
        };

        try {
            await ApiService.runAnalysis(payload, '/api/analyze', callbacks);
        } catch (err) {
            this.handleError(err);
        }
    }

    resetState() {
        this.state.results = [];
        this.state.extras = null;
    }

    handleProgress(progress) {
        if (progress.status === 'conflict') {
            this.state.systemStatus = 'syncing';
            this.state.analysisStatusMessage = 'processing_background';
        } else if (progress.status === 'retrying') {
            this.state.systemStatus = 'warning';
            this.state.analysisStatusMessage = `retrying_connection (${progress.details.attempt}/${progress.details.total})`;
        } else {
            this.state.analysisStatusMessage = progress.message;
        }
    }

    handleResult(item) {
        // Reactive update: UI will re-render automatically via AppState subscription
        this.state.results = [...this.state.results, item];
        
        // Update progress percentage
        const total = this.state.analysisPayload?.tickers?.length || 1;
        this.state.analysisProgress = (this.state.results.length / total) * 100;
    }

    async handleComplete(payload) {
        // Heavy math runs in the worker; failure propagates to the error path.
        if (this.state.results.length > 0) {
            this.state.extras = await this.runWorkerTask('CALCULATE_EXTRAS', this.state.results, payload);
        }

        this.state.isAnalyzing = false;
        this.state.systemStatus = 'ready';
        showToast("Analiz tamamlandı", "success");
    }

    /**
     * One-shot worker task: create, await terminal message, always terminate.
     */
    runWorkerTask(type, results, payload) {
        return new Promise((resolve, reject) => {
            const worker = new Worker(new URL('../worker.js', import.meta.url), { type: 'module' });

            const finish = (settle, value) => {
                worker.removeEventListener('message', handler);
                worker.terminate();
                settle(value);
            };

            const handler = (e) => {
                if (e.data.type === 'EXTRAS_RESULT') finish(resolve, e.data.extras);
                else if (e.data.type === 'ERROR') finish(reject, new Error(e.data.message));
            };

            worker.addEventListener('message', handler);
            worker.postMessage({ type, results, payload });
        });
    }

    handleError(err) {
        this.state.isAnalyzing = false;
        this.state.systemStatus = 'error';
        const msg = err.status === 429 ? "Çok fazla istek" : (err.message || "Bağlantı hatası");
        showToast(msg, "error");
    }
}
