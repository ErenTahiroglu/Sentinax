import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Worker double: records protocol traffic, replies only when the test says so.
let workers;
class MockWorker {
    constructor(url, options) {
        this.url = url;
        this.options = options;
        this.listeners = {};
        this.messages = [];
        this.terminated = false;
        workers.push(this);
    }
    addEventListener(type, l) { (this.listeners[type] = this.listeners[type] || []).push(l); }
    removeEventListener(type, l) {
        this.listeners[type] = (this.listeners[type] || []).filter(x => x !== l);
    }
    terminate() { this.terminated = true; }
    postMessage(data) { this.messages.push(data); }
    reply(data) { (this.listeners.message || []).forEach(l => l({ data })); }
}

vi.mock('../../js/network/HttpClient.js', () => ({
    httpClient: { get: vi.fn(), post: vi.fn() }
}));
vi.mock('../../js/utils.js', () => ({ showToast: vi.fn() }));

import { AnalysisService } from '../../js/services/AnalysisService.js';
import { httpClient } from '../../js/network/HttpClient.js';

const streamOf = (...items) => {
    const reads = items.map(i => ({ done: false, value: new TextEncoder().encode(`data: ${JSON.stringify(i)}\n\n`) }));
    const read = vi.fn();
    reads.forEach(r => read.mockResolvedValueOnce(r));
    read.mockResolvedValueOnce({ done: true });
    return { body: { getReader: () => ({ read, releaseLock: vi.fn() }) } };
};

const tick = () => new Promise(r => setTimeout(r, 0));

describe('AnalysisService -> ApiService -> worker integration', () => {
    let state, service;
    const payload = { tickers: ['AAPL'] };

    beforeEach(() => {
        vi.clearAllMocks();
        workers = [];
        vi.stubGlobal('Worker', MockWorker);
        state = { results: [], extras: null, isAnalyzing: false, systemStatus: 'idle' };
        service = new AnalysisService(state);
    });

    it('streams results, delegates to worker, stores extras, terminates worker', async () => {
        httpClient.post.mockResolvedValueOnce(streamOf({ ticker: 'AAPL' }));

        const done = service.executeAnalysis(payload);
        await vi.waitFor(() => expect(workers).toHaveLength(1));

        expect(httpClient.post).toHaveBeenCalledWith('/api/analyze', expect.objectContaining({ tickers: ['AAPL'] }), expect.any(Object));
        expect(state.results).toEqual([{ ticker: 'AAPL' }]);

        const worker = workers[0];
        expect(worker.options).toEqual({ type: 'module' });
        expect(worker.messages).toEqual([{ type: 'CALCULATE_EXTRAS', results: state.results, payload }]);

        // Not complete until worker answers
        await tick();
        expect(state.isAnalyzing).toBe(true);
        expect(state.systemStatus).not.toBe('ready');

        const extras = { correlation: { matrix: [[1.0]] } };
        worker.reply({ type: 'EXTRAS_RESULT', extras });
        await done;

        expect(state.extras).toEqual(extras);
        expect(state.isAnalyzing).toBe(false);
        expect(state.systemStatus).toBe('ready');
        expect(worker.terminated).toBe(true);
        expect(worker.listeners.message).toHaveLength(0);
    });

    it('surfaces worker ERROR without fabricating extras or marking ready', async () => {
        httpClient.post.mockResolvedValueOnce(streamOf({ ticker: 'AAPL' }));

        const done = service.executeAnalysis(payload);
        await vi.waitFor(() => expect(workers).toHaveLength(1));
        workers[0].reply({ type: 'ERROR', message: 'boom' });
        await done;

        expect(state.extras).toBeNull();
        expect(state.systemStatus).toBe('error');
        expect(state.isAnalyzing).toBe(false);
        expect(workers[0].terminated).toBe(true);
    });

    it('api.js stays pure: no global AppState access or Worker management', () => {
        const src = readFileSync(resolve(process.cwd(), 'frontend/js/network/api.js'), 'utf8');
        expect(src).not.toMatch(/AppState/);
        expect(src).not.toMatch(/new Worker/);
        expect(src).not.toMatch(/CALCULATE_EXTRAS/);
    });
});
