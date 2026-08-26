/**
 * 📦 State Manager v2 - Granular Reactive Store
 * ===========================================
 * Features:
 * - Property-based subscriptions (Pub/Sub)
 * - Proxy-based automatic mutation tracking
 * - Memory-safe unsubscription
 */

export function createStore(initialState) {
    const listeners = new Map(); // Map<string, Function[]>
    const globalListeners = [];

    const state = new Proxy(initialState, {
        set(target, property, value) {
            if (target[property] === value) return true;
            
            const oldValue = target[property];
            target[property] = value;
            
            // 1. Notify property-specific listeners
            if (listeners.has(property)) {
                listeners.get(property).forEach(fn => fn(value, oldValue, target));
            }
            
            // 2. Notify global listeners (backward compatibility)
            globalListeners.forEach(fn => fn(property, value, oldValue, target));
            
            return true;
        }
    });

    /**
     * Subscribe to state changes
     * @param {string|Function} keyOrFn - Property name to watch, or global listener function
     * @param {Function} [fn] - Callback function if key is provided
     */
    Object.defineProperty(state, 'subscribe', {
        value: function(keyOrFn, fn) {
            let targetKey = null;
            let callback = null;

            if (typeof keyOrFn === 'string' && typeof fn === 'function') {
                targetKey = keyOrFn;
                callback = fn;
                
                if (!listeners.has(targetKey)) listeners.set(targetKey, []);
                listeners.get(targetKey).push(callback);
                
                // Initial trigger for the specific key
                callback(state[targetKey], undefined, state);
            } else if (typeof keyOrFn === 'function') {
                callback = keyOrFn;
                globalListeners.push(callback);
                
                // Initial trigger for all keys (backward compat)
                Object.keys(initialState).forEach(key => callback(key, state[key], undefined, state));
            }

            // Return unsubscribe function
            return () => {
                if (targetKey) {
                    const list = listeners.get(targetKey);
                    if (list) {
                        const idx = list.indexOf(callback);
                        if (idx > -1) list.splice(idx, 1);
                    }
                } else {
                    const idx = globalListeners.indexOf(callback);
                    if (idx > -1) globalListeners.splice(idx, 1);
                }
            };
        },
        enumerable: false,
        writable: false
    });

    return state;
}
