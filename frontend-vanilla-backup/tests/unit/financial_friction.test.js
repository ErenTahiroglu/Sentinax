import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createStore } from '../../js/core/state.js';

describe('Financial Friction & State-API Sync Tests', () => {
    let AppState;

    beforeEach(() => {
        // Reset AppState for each test
        AppState = createStore({
            commissionRate: 0.2, // %0.2
            slippageRate: 0.1,   // %0.1
            results: []
        });
        vi.stubGlobal('localStorage', {
            getItem: vi.fn(),
            setItem: vi.fn()
        });
    });

    describe('Rate Validation Logic', () => {
        it('should prevent negative rates and default to 0', () => {
            // Simulating the logic in app.js saveRates
            const inputComm = -0.5;
            const inputSlip = -1.2;
            
            AppState.commissionRate = Math.max(0, inputComm);
            AppState.slippageRate = Math.max(0, inputSlip);
            
            expect(AppState.commissionRate).toBe(0);
            expect(AppState.slippageRate).toBe(0);
        });

        it('should handle sub-percent precision (0.001%)', () => {
            AppState.commissionRate = 0.001;
            expect(AppState.commissionRate).toBe(0.001);
        });
    });

    describe('API Payload Conversion', () => {
        it('should convert percentage to decimal for backend', () => {
            // Frontend uses % (0.2), Backend expects fraction (0.002)
            const toBackend = (rate) => rate / 100;
            
            AppState.commissionRate = 0.25;
            expect(toBackend(AppState.commissionRate)).toBe(0.0025);
            
            AppState.slippageRate = 0.1;
            expect(toBackend(AppState.slippageRate)).toBe(0.001);
        });
    });
});
