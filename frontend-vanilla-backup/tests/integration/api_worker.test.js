import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock Worker
class MockWorker {
    constructor(url, options) {
        this.url = url;
        this.options = options;
        this.onmessage = null;
        this.listeners = {};
    }
    addEventListener(type, listener) {
        this.listeners[type] = this.listeners[type] || [];
        this.listeners[type].push(listener);
    }
    removeEventListener(type, listener) {
        if (this.listeners[type]) {
            this.listeners[type] = this.listeners[type].filter(l => l !== listener);
        }
    }
    terminate() {
        // Dummy terminate for tests
    }
    postMessage(data) {
        // Simulate async worker response
        setTimeout(() => {
            const response = {
                data: {
                    type: 'EXTRAS_RESULT',
                    extras: { 
                        correlation: { matrix: [[1.0]] },
                        monte_carlo: { percentiles: { p50: [1.0] } }
                    }
                }
            };
            if (this.listeners['message']) {
                this.listeners['message'].forEach(l => l(response));
            }
        }, 10);
    }
}

vi.stubGlobal('Worker', MockWorker);

// Mock HttpClient
vi.mock('../../js/network/HttpClient.js', () => ({
    httpClient: {
        get: vi.fn(),
        post: vi.fn()
    }
}));

import { runAnalysis } from '../../js/network/api.js';
import { httpClient } from '../../js/network/HttpClient.js';

describe('API and Worker Integration', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        // Setup minimal DOM for api.js
        document.body.innerHTML = `
            <div id="progress-container" class="hidden">
                <div id="progress-fill"></div>
                <div id="progress-text"></div>
            </div>
            <div id="results" class="hidden">
                <div id="results-grid"></div>
            </div>
            <button id="analyze-btn"></button>
        `;
        
        global.AppState = { results: [], extras: null };
        global.getLang = () => 'tr';
        global.t = (k) => k;
        global.showToast = vi.fn();
        global.saveApiKeys = vi.fn();
    });

    it('should delegate calculations to worker after fetching data', async () => {
        // Mock successful API stream response
        httpClient.post.mockResolvedValueOnce({
            body: {
                getReader: () => ({
                    read: vi.fn()
                        .mockResolvedValueOnce({ done: false, value: new TextEncoder().encode('data: {"ticker": "AAPL"}\n\n') })
                        .mockResolvedValueOnce({ done: true })
                })
            }
        });

        await runAnalysis({ tickers: ['AAPL'] }, '/api/analyze');

        // Check if data was "fetched"
        expect(httpClient.post).toHaveBeenCalledWith('/api/analyze', expect.any(Object));
        
        // Check if AppState was updated with worker results
        // Since we use setTimeout in MockWorker, we might need to wait
        await vi.waitFor(() => {
            expect(global.AppState.extras).not.toBeNull();
            expect(global.AppState.extras.correlation.matrix).toEqual([[1.0]]);
        });
    });
});
