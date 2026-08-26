import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createStore } from '../../js/core/state.js';

describe('Frontend State Resilience Tests', () => {
    describe('createStore Reactivity', () => {
        it('should trigger property-specific subscriptions', () => {
            const state = createStore({ count: 0, name: 'Initial' });
            const spy = vi.fn();
            
            state.subscribe('count', spy);
            // Initial trigger on subscribe
            expect(spy).toHaveBeenCalledWith(0, undefined, expect.any(Object));
            
            state.count = 10;
            expect(spy).toHaveBeenCalledWith(10, 0, expect.any(Object));
            expect(spy).toHaveBeenCalledTimes(2);
        });

        it('should handle global subscriptions for backward compatibility', () => {
            const state = createStore({ a: 1, b: 2 });
            const spy = vi.fn();
            
            state.subscribe(spy);
            // Initial trigger for each key
            expect(spy).toHaveBeenCalledWith('a', 1, undefined, expect.any(Object));
            expect(spy).toHaveBeenCalledWith('b', 2, undefined, expect.any(Object));
            
            state.a = 100;
            expect(spy).toHaveBeenLastCalledWith('a', 100, 1, expect.any(Object));
        });

        it('should unsubscribe correctly to prevent memory leaks', () => {
            const state = createStore({ data: [] });
            const spy = vi.fn();
            const unsub = state.subscribe('data', spy);
            
            state.data = [1];
            expect(spy).toHaveBeenCalledTimes(2);
            
            unsub();
            state.data = [1, 2];
            expect(spy).toHaveBeenCalledTimes(2); // No new call
        });
    });

    describe('WebSocket Reconnection Logic (LivePrices)', () => {
        // Since LivePrices.js is a module with side effects and uses global variables,
        // we mock the WebSocket to test the reconnection trigger.
        
        it('should attempt reconnection after socket closure', async () => {
            vi.useFakeTimers();
            const { initLivePrices } = await import('../../js/network/livePrices.js');
            
            // Mock global WebSocket
            const mockWS = {
                onopen: vi.fn(),
                onmessage: vi.fn(),
                onclose: vi.fn(),
                onerror: vi.fn(),
                readyState: 0,
                close: vi.fn()
            };
            
            vi.stubGlobal('WebSocket', vi.fn(() => mockWS));
            vi.stubGlobal('location', { protocol: 'http:', host: 'localhost' });

            initLivePrices();
            
            // Simulate socket close
            mockWS.readyState = 3; // CLOSED
            mockWS.onclose();
            
            // Advance timers by 7.5 seconds (5s + max 2s jitter + buffer)
            vi.advanceTimersByTime(7500);
            
            // Check if initLivePrices was called again (indicated by a new WebSocket instance)
            expect(globalThis.WebSocket).toHaveBeenCalledTimes(2);
            
            vi.useRealTimers();
        });
    });
});
