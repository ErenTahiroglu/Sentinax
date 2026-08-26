/**
 * 🧮 MathEngine - High-Performance Financial Computing Core
 * ========================================================
 * Features:
 * - ESM (ES Modules) support
 * - Float64Array for memory-efficient calculations
 * - Pure functions for Web Worker compatibility (Zero-copy ready)
 * - IEEE 754 Precision helpers
 * - Statistical and Financial kernels (GBM, Correlation, Sharpe)
 */

export class MathEngine {
    /**
     * 🛡️ Precision Helper: Round to fixed decimal to avoid IEEE 754 artifacts
     */
    static round(value, decimals = 4) {
        if (typeof value !== 'number' || isNaN(value)) return 0;
        const factor = Math.pow(10, decimals);
        return Math.round((value + Number.EPSILON) * factor) / factor;
    }

    /**
     * 📈 Calculate Arithmetic Returns
     * @param {Float64Array} prices 
     * @returns {Float64Array}
     */
    static calculateReturns(prices) {
        if (prices.length < 2) return new Float64Array(0);
        const returns = new Float64Array(prices.length - 1);
        for (let i = 0; i < returns.length; i++) {
            if (prices[i] === 0) {
                returns[i] = 0;
            } else {
                returns[i] = (prices[i + 1] / prices[i]) - 1;
            }
        }
        return returns;
    }

    /**
     * 📊 Calculate Mean (Expected Return)
     */
    static calculateMean(data) {
        if (data.length === 0) return 0;
        let sum = 0;
        for (let i = 0; i < data.length; i++) {
            sum += data[i];
        }
        return sum / data.length;
    }

    /**
     * 📉 Calculate Annualized Volatility (Standard Deviation)
     * @param {Float64Array} returns - Periodic returns
     * @param {number} periodsPerYear - e.g., 252 for daily, 52 for weekly
     */
    static calculateVolatility(returns, periodsPerYear = 52) {
        if (returns.length < 2) return 0;
        const mean = this.calculateMean(returns);
        let sqDiffSum = 0;
        for (let i = 0; i < returns.length; i++) {
            sqDiffSum += Math.pow(returns[i] - mean, 2);
        }
        const variance = sqDiffSum / returns.length;
        const periodicVol = Math.sqrt(variance);
        return periodicVol * Math.sqrt(periodsPerYear);
    }

    /**
     * 🎯 Calculate Sharpe Ratio
     * @param {Float64Array} returns 
     * @param {number} riskFreeRate - Annual risk-free rate (e.g., 0.05 for 5%)
     * @param {number} periodsPerYear 
     */
    static calculateSharpeRatio(returns, riskFreeRate = 0, periodsPerYear = 52) {
        const meanReturn = this.calculateMean(returns);
        const annualReturn = meanReturn * periodsPerYear;
        const annualVol = this.calculateVolatility(returns, periodsPerYear);
        
        if (annualVol === 0) return 0;
        return this.round((annualReturn - riskFreeRate) / annualVol, 2);
    }

    /**
     * 🔗 Pearson Correlation Coefficient
     * @param {Float64Array} seriesA 
     * @param {Float64Array} seriesB 
     */
    static calculateCorrelation(seriesA, seriesB) {
        const n = Math.min(seriesA.length, seriesB.length);
        if (n < 2) return 0;

        let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0, sumY2 = 0;
        for (let i = 0; i < n; i++) {
            const x = seriesA[i];
            const y = seriesB[i];
            sumX += x;
            sumY += y;
            sumXY += x * y;
            sumX2 += x * x;
            sumY2 += y * y;
        }

        const num = (n * sumXY) - (sumX * sumY);
        const den = Math.sqrt(((n * sumX2) - (sumX * sumX)) * ((n * sumY2) - (sumY * sumY)));
        
        if (den === 0) return 0;
        return this.round(num / den, 2);
    }

    /**
     * 🎲 Geometric Brownian Motion (GBM) Monte Carlo Kernel
     * @param {Object} params 
     * @returns {Object} { percentiles: Float64Array[], buffer: ArrayBuffer }
     */
    static runMonteCarlo(returns, steps = 12, numSims = 1000) {
        if (returns.length < 4) return null;

        const mean = this.calculateMean(returns);
        let sqDiffSum = 0;
        for (let i = 0; i < returns.length; i++) {
            sqDiffSum += Math.pow(returns[i] - mean, 2);
        }
        const variance = sqDiffSum / returns.length;
        const stdDev = Math.sqrt(variance);

        // Pre-allocate memory for all simulations: numSims * (steps + 1)
        const totalSize = numSims * (steps + 1);
        const data = new Float64Array(totalSize);

        for (let s = 0; s < numSims; s++) {
            let current = 1.0;
            const offset = s * (steps + 1);
            data[offset] = current; // t=0

            for (let t = 1; t <= steps; t++) {
                // Box-Muller transform for normal distribution
                const u1 = Math.random();
                const u2 = Math.random();
                const randStdNorm = Math.sqrt(-2.0 * Math.log(u1 || 0.000001)) * Math.sin(2.0 * Math.PI * u2);
                
                // GBM formula: S_t = S_{t-1} * exp((mu - 0.5*sigma^2) + sigma * Z)
                // Simplified walk used in previous JS implementation:
                const walk = mean + stdDev * randStdNorm;
                current *= (1 + walk);
                data[offset + t] = current;
            }
        }

        // Calculate Percentiles (p5, p25, p50, p75, p95)
        const percentiles = {
            p5: new Float64Array(steps + 1),
            p25: new Float64Array(steps + 1),
            p50: new Float64Array(steps + 1),
            p75: new Float64Array(steps + 1),
            p95: new Float64Array(steps + 1)
        };

        for (let t = 0; t <= steps; t++) {
            const stepValues = new Float64Array(numSims);
            for (let s = 0; s < numSims; s++) {
                stepValues[s] = data[s * (steps + 1) + t];
            }
            stepValues.sort();

            percentiles.p5[t] = this.round(stepValues[Math.floor(numSims * 0.05)], 3);
            percentiles.p25[t] = this.round(stepValues[Math.floor(numSims * 0.25)], 3);
            percentiles.p50[t] = this.round(stepValues[Math.floor(numSims * 0.50)], 3);
            percentiles.p75[t] = this.round(stepValues[Math.floor(numSims * 0.75)], 3);
            percentiles.p95[t] = this.round(stepValues[Math.floor(numSims * 0.95)], 3);
        }

        return {
            percentiles,
            rawData: data.buffer // Return buffer for transferability
        };
    }
}
