/**
 * 📡 ApiService - Pure Network Layer
 * ================================
 * Responsibilities: HTTP requests, SSE streaming, polling, and cache retrieval.
 * Constraints: NO DOM manipulation, NO global state access.
 */

import { httpClient } from './HttpClient.js';

/**
 * Orchestrates analysis flow: Cache check -> Server Request -> Stream/Polling
 */
export async function runAnalysis(payload, endpoint, callbacks) {
    const { onProgress, onResult, onComplete, onError } = callbacks;
    
    try {
        const { cachedItems, tickersToFetch } = await checkLocalCache(payload);
        
        // 1. Deliver cached items immediately
        if (cachedItems.length > 0) {
            cachedItems.forEach(item => onResult({ ...item, _fromCache: true }));
        }

        if (tickersToFetch.length === 0) return onComplete?.();

        // 2. Request remaining from server
        onProgress?.({ status: 'streaming', message: 'requesting_server' });
        const response = await httpClient.post(endpoint, { ...payload, tickers: tickersToFetch }, {
            onRetry: (attempt, total, backoff) => {
                onProgress?.({ 
                    status: 'retrying', 
                    message: `retrying_connection`, 
                    details: { attempt, total, backoff } 
                });
            }
        });
        
        if (response.status === 409) return handle409(payload, endpoint, callbacks);
        
        await consumeStream(response, onResult);
        onComplete?.();

    } catch (err) {
        if (err.status === 409) return handle409(payload, endpoint, callbacks);
        onError?.(err);
    }
}

/**
 * Consumes SSE stream and parses JSON chunks
 */
async function consumeStream(response, onResult) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            buffer = processBuffer(buffer, onResult);
        }
    } finally {
        reader.releaseLock();
    }
}

/**
 * Splits buffer by SSE delimiters and parses items
 */
function processBuffer(buffer, onResult) {
    const lines = buffer.split("\n\n");
    const remainder = lines.pop(); // Keep partial line for next chunk

    for (const line of lines) {
        if (line.startsWith("data: ")) {
            try {
                const item = JSON.parse(line.substring(6));
                if (item.ticker) onResult(item);
            } catch (e) { 
                console.warn("[ApiService] Parse error:", e); 
            }
        }
    }
    return remainder;
}

/**
 * Handles 409 Conflict (Idempotency) with recursive retry
 */
async function handle409(payload, endpoint, callbacks) {
    callbacks.onProgress?.({ status: 'conflict', message: 'processing_background' });
    
    // Exponential backoff or fixed wait for 409
    await new Promise(r => setTimeout(r, 5000));
    return runAnalysis(payload, endpoint, callbacks);
}

/**
 * Pure logic for cache separation
 */
async function checkLocalCache(payload) {
    const tickersToFetch = [];
    const cachedItems = [];
    
    if (typeof getCache !== 'function') return { cachedItems, tickersToFetch: payload.tickers };

    for (const ticker of payload.tickers) {
        const key = `analysis_${ticker.toUpperCase()}_ai${payload.use_ai}_isl${payload.check_islamic}`;
        const data = await getCache(key);
        if (data) cachedItems.push(data);
        else tickersToFetch.push(ticker);
    }
    
    return { cachedItems, tickersToFetch };
}

// Re-export other pure functions
export { pollJobResult, checkServerHealth } from './api_helpers.js';
