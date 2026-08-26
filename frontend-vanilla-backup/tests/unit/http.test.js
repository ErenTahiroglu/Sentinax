import { describe, it, expect, vi, beforeEach } from 'vitest';
import { HttpClient } from '../../js/network/HttpClient.js';

describe('HttpClient Tests', () => {
    let client;
    const mockApiBase = 'http://api.test';

    beforeEach(() => {
        vi.stubGlobal('API_BASE', mockApiBase);
        vi.stubGlobal('fetch', vi.fn());
        vi.stubGlobal('crypto', { randomUUID: () => 'test-uuid' });
        client = new HttpClient({ maxRetries: 1 });
    });

    it('should add X-Correlation-ID to headers', async () => {
        global.fetch.mockResolvedValueOnce({
            ok: true,
            headers: new Headers({ 'content-type': 'application/json' }),
            json: () => Promise.resolve({ success: true })
        });

        await client.get('/test');

        expect(global.fetch).toHaveBeenCalledWith(
            expect.stringContaining('/test'),
            expect.objectContaining({
                headers: expect.objectContaining({
                    'X-Correlation-ID': 'test-uuid'
                })
            })
        );
    });

    it('should inject Authorization header if session exists', async () => {
        vi.stubGlobal('SupabaseAuth', {
            getValidSession: vi.fn().mockResolvedValue({ access_token: 'mock-token' })
        });

        global.fetch.mockResolvedValueOnce({
            ok: true,
            headers: new Headers({ 'content-type': 'application/json' }),
            json: () => Promise.resolve({ success: true })
        });

        await client.get('/secure');

        expect(global.fetch).toHaveBeenCalledWith(
            expect.anything(),
            expect.objectContaining({
                headers: expect.objectContaining({
                    'Authorization': 'Bearer mock-token'
                })
            })
        );
    });

    it('should retry on 429 errors', async () => {
        global.fetch
            .mockResolvedValueOnce({ ok: false, status: 429, statusText: 'Too Many Requests', headers: new Headers() })
            .mockResolvedValueOnce({
                ok: true,
                headers: new Headers({ 'content-type': 'application/json' }),
                json: () => Promise.resolve({ successAfterRetry: true })
            });

        vi.useFakeTimers();
        
        const promise = client.get('/retry');
        
        await vi.runAllTimersAsync();
        
        const result = await promise;
        
        expect(global.fetch).toHaveBeenCalledTimes(2);
        expect(result.successAfterRetry).toBe(true);
        vi.useRealTimers();
    });

    it('should throw standardized error on failed request', async () => {
        global.fetch.mockResolvedValueOnce({
            ok: false,
            status: 400,
            statusText: 'Bad Request',
            headers: new Headers({ 'content-type': 'application/json' }),
            json: () => Promise.resolve({ detail: 'Custom error message' })
        });

        try {
            await client.get('/fail');
            expect.fail('Should have thrown an error');
        } catch (err) {
            expect(err.status).toBe(400);
            expect(err.message).toBe('Custom error message');
            expect(err.correlationId).toBe('test-uuid');
        }
    });

    it('should handle timeout correctly', async () => {
        client = new HttpClient({ timeout: 10, maxRetries: 0 });
        
        // Mock fetch to reject when aborted
        global.fetch.mockImplementationOnce((url, { signal }) => {
            return new Promise((resolve, reject) => {
                const timeout = setTimeout(() => resolve({ ok: true }), 100);
                if (signal) {
                    if (signal.aborted) {
                        clearTimeout(timeout);
                        const err = new Error('Aborted');
                        err.name = 'AbortError';
                        reject(err);
                    } else {
                        signal.addEventListener('abort', () => {
                            clearTimeout(timeout);
                            const err = new Error('Aborted');
                            err.name = 'AbortError';
                            reject(err);
                        });
                    }
                }
            });
        });

        try {
            await client.get('/timeout');
            expect.fail('Should have timed out');
        } catch (err) {
            expect(err.status).toBe(408);
            expect(err.message).toBe('Request Timeout');
        }
    });
});
