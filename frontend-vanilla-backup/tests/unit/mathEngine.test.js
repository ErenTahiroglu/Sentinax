import { describe, it, expect } from 'vitest';
import { MathEngine } from '../../js/core/MathEngine.js';

describe('MathEngine Core Tests', () => {
    describe('Precision Helpers', () => {
        it('should round correctly to avoid float artifacts', () => {
            expect(MathEngine.round(0.1 + 0.2, 2)).toBe(0.3);
            expect(MathEngine.round(1.005, 2)).toBe(1.01);
        });
    });

    describe('Statistical Calculations', () => {
        const prices = new Float64Array([100, 110, 121, 133.1]); // 10% returns
        const returns = MathEngine.calculateReturns(prices);

        it('should calculate arithmetic returns correctly', () => {
            expect(returns).toHaveLength(3);
            expect(MathEngine.round(returns[0], 2)).toBe(0.1);
        });

        it('should calculate annualized volatility', () => {
            const vol = MathEngine.calculateVolatility(returns, 52);
            expect(vol).toBeGreaterThan(0);
            // Constant 10% returns has 0 volatility
            const constantReturns = new Float64Array([0.1, 0.1, 0.1]);
            expect(MathEngine.calculateVolatility(constantReturns)).toBeCloseTo(0, 10);
        });

        it('should calculate correlation using Float64Array', () => {
            const a = new Float64Array([1, 2, 3, 4, 5]);
            const b = new Float64Array([2, 4, 6, 8, 10]);
            expect(MathEngine.calculateCorrelation(a, b)).toBe(1);
        });
    });

    describe('Monte Carlo Simulation', () => {
        it('should generate valid percentiles using GBM', () => {
            const returns = new Float64Array([0.01, -0.01, 0.02, -0.02, 0.03]);
            const result = MathEngine.runMonteCarlo(returns, 12, 100);
            
            expect(result).not.toBeNull();
            expect(result.percentiles.p50).toHaveLength(13);
            expect(result.rawData).toBeInstanceOf(ArrayBuffer);
            
            // p50 should be around 1.0 since mean is near 0
            expect(result.percentiles.p50[0]).toBe(1.0);
            expect(result.percentiles.p50[12]).toBeGreaterThan(0.5);
        });
    });
});
