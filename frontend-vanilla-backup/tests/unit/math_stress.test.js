import { describe, it, expect, vi } from 'vitest';
import { MathEngine } from '../../js/core/MathEngine.js';

describe('📉 Frontend Financial Math Stress Tests', () => {

    describe('Precision & Rounding Accumulation', () => {
        it('should maintain IEEE 754 precision over 10,000 iterations', () => {
            let value = 1.0;
            const factor = 1.0001;
            
            for (let i = 0; i < 10000; i++) {
                value = MathEngine.round(value * factor, 8);
            }
            
            // Expected: 1.0 * (1.0001^10000) approx 2.7181
            expect(value).toBeGreaterThan(2.71);
            expect(value).toBeLessThan(2.73);
            expect(Number.isNaN(value)).toBe(false);
        });

        it('should handle extremely small numbers without underflow to zero', () => {
            const small = 0.00000000000001;
            const rounded = MathEngine.round(small, 15);
            expect(rounded).toBe(small);
        });
    });

    describe('Edge Cases (Zero/Empty Data)', () => {
        it('should handle zero-volatility (constant price) in Sharpe Ratio', () => {
            const constantPrices = new Float64Array([100, 100, 100, 100]);
            const returns = MathEngine.calculateReturns(constantPrices);
            const sharpe = MathEngine.calculateSharpeRatio(returns, 0.05, 252);
            
            // Volatility is 0, division by zero safely returns 0
            expect(sharpe).toBe(0);
        });

        it('should not crash with empty Float64Array in Monte Carlo', () => {
            const empty = new Float64Array(0);
            const result = MathEngine.runMonteCarlo(empty);
            expect(result).toBeNull();
        });

        it('should handle returns containing infinity or NaN safely', () => {
             const badData = new Float64Array([1.0, NaN, Infinity]);
             const mean = MathEngine.calculateMean(badData);
             // NaN behavior check
             expect(Number.isNaN(mean)).toBe(true);
        });
    });

    describe('Monte Carlo Simulation Stability', () => {
        it('should generate valid distribution (p5 < p50 < p95)', () => {
            // Random walk returns
            const returns = new Float64Array(50).map(() => (Math.random() - 0.5) * 0.1);
            const result = MathEngine.runMonteCarlo(returns, 12, 500);
            
            expect(result).not.toBeNull();
            const { p5, p50, p95 } = result.percentiles;
            
            // At T=12, check ordering
            const lastIdx = 12;
            expect(p5[lastIdx]).toBeLessThanOrEqual(p50[lastIdx]);
            expect(p50[lastIdx]).toBeLessThanOrEqual(p95[lastIdx]);
        });
    });
});
