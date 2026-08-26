/**
 * 📡 HttpClient - Centralized & Resilient Network Layer
 * ===================================================
 * Features:
 * - ESM (ES Modules) support
 * - Automatic X-Correlation-ID injection
 * - Exponential Backoff Retries (429, 50x)
 * - Timeout support via AbortController
 * - Supabase Auth Interceptor (JWT injection)
 * - Standardized Error Handling
 */

export class HttpClient {
    constructor(options = {}) {
        this.baseUrl = options.baseUrl || window.API_BASE || '';
        this.timeout = options.timeout || 120000; // 120s for complex TEFAS + AI analyses
        this.maxRetries = options.maxRetries || 3;
        this.defaultHeaders = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            ...options.headers
        };
        this.hasWokenUp = false;
    }

    /**
     * Core request method with retry logic and interceptors
     */
    async request(endpoint, options = {}) {
        const url = endpoint.startsWith('http') ? endpoint : `${this.baseUrl}${endpoint}`;
        const correlationId = crypto.randomUUID ? crypto.randomUUID() : this._generateUUID();
        
        // 🛡️ Idempotency: Create one key per request for mutations and preserve across retries
        const method = (options.method || 'GET').toUpperCase();
        let idempotencyKey = null;
        if (['POST', 'PUT', 'PATCH'].includes(method)) {
            if (options.headers && options.headers['Idempotency-Key']) {
                idempotencyKey = options.headers['Idempotency-Key'];
            } else if (options.body && typeof options.body === 'string') {
                let hash = 0;
                for (let i = 0; i < options.body.length; i++) {
                    const char = options.body.charCodeAt(i);
                    hash = ((hash << 5) - hash) + char;
                    hash |= 0;
                }
                idempotencyKey = `idemp-${Math.abs(hash)}`;
            } else {
                idempotencyKey = crypto.randomUUID ? crypto.randomUUID() : this._generateUUID();
            }
        }
        
        let attempt = 0;
        const retriableStatuses = [429, 502, 503, 504];

        while (attempt <= this.maxRetries) {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), this.timeout);

            try {
                const authHeaders = await this._getAuthHeaders();
                const combinedHeaders = {
                    ...this.defaultHeaders,
                    'X-Correlation-ID': correlationId,
                    ...authHeaders,
                    ...options.headers
                };

                if (idempotencyKey) combinedHeaders['Idempotency-Key'] = idempotencyKey;

                const response = await fetch(url, {
                    ...options,
                    headers: combinedHeaders,
                    signal: controller.signal
                });

                clearTimeout(timeoutId);

                if (response.ok) {
                    const contentType = response.headers.get('content-type');
                    return contentType?.includes('application/json') ? await response.json() : response;
                }

                // 🛡️ Retry Logic for specific statuses
                if (retriableStatuses.includes(response.status) && attempt < this.maxRetries) {
                    const backoff = Math.pow(2, attempt + 1) * 1000; // 2s, 4s, 8s...
                    console.warn(`[HttpClient] Retrying ${url} in ${backoff}ms (Attempt ${attempt + 1}/${this.maxRetries})`);
                    
                    // Notify UI via global event or specific callback if provided
                    if (options.onRetry) options.onRetry(attempt + 1, this.maxRetries, backoff);

                    await new Promise(r => setTimeout(r, backoff));
                    attempt++;
                    continue;
                }

                const errorData = await response.json().catch(() => ({ detail: response.statusText }));
                throw { status: response.status, message: errorData.detail || errorData.message || 'API Error', correlationId };

            } catch (err) {
                clearTimeout(timeoutId);
                
                // 🛡️ Handle Retriable Errors (Network Failure OR Timeout)
                const isTimeout = err.name === 'AbortError';
                const isNetworkError = !err.status && !isTimeout;

                if ((isTimeout || isNetworkError) && attempt < this.maxRetries) {
                    const backoff = Math.pow(2, attempt + 1) * 1000;
                    console.warn(`[HttpClient] ${isTimeout ? 'Timeout' : 'Network Error'}. Retrying in ${backoff}ms (Attempt ${attempt + 1}/${this.maxRetries})`);
                    
                    if (options.onRetry) options.onRetry(attempt + 1, this.maxRetries, backoff);
                    
                    await new Promise(r => setTimeout(r, backoff));
                    attempt++;
                    continue;
                }

                // If we've reached maxRetries or it's a non-retriable error, throw it
                if (isTimeout) throw { status: 408, message: 'Request Timeout (Max Retries Exceeded)', correlationId };
                if (err.status) throw err; // Already standardized API error

                throw { status: 0, message: err.message || 'Network Error', correlationId };
            }
        }
    }

    async get(endpoint, options = {}) {
        return this.request(endpoint, { ...options, method: 'GET' });
    }

    async post(endpoint, body, options = {}) {
        return this.request(endpoint, { 
            ...options, 
            method: 'POST', 
            body: JSON.stringify(body) 
        });
    }

    async put(endpoint, body, options = {}) {
        return this.request(endpoint, { 
            ...options, 
            method: 'PUT', 
            body: JSON.stringify(body) 
        });
    }

    async delete(endpoint, options = {}) {
        return this.request(endpoint, { ...options, method: 'DELETE' });
    }

    /**
     * Interceptor to get active Supabase session
     */
    async _getAuthHeaders() {
        if (window.SupabaseAuth && typeof window.SupabaseAuth.getValidSession === 'function') {
            try {
                const session = await window.SupabaseAuth.getValidSession();
                if (session && session.access_token) {
                    return { 'Authorization': `Bearer ${session.access_token}` };
                }
            } catch (e) {
                console.warn('[HttpClient] Auth Interceptor Error:', e);
            }
        }
        return {};
    }

    /**
     * Fallback UUID generator if crypto.randomUUID is not available
     */
    _generateUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            var r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }
}

// Export singleton instance for global use
export const httpClient = new HttpClient();
window.httpClient = httpClient;
