/**
 * 🧵 Web Worker - High-Performance Analytics Task
 * ===============================================
 * This worker handles heavy mathematical computations using MathEngine.
 * Uses ES Modules and Transferable Objects for zero-copy data transfer.
 */

import { MathEngine } from './core/MathEngine.js';

self.onmessage = async function(e) {
    try {
        const { type, payload, results } = e.data;

        if (type === 'CALCULATE_EXTRAS') {
            const extras = processExtras(results, payload);
            
            // Collect buffers for zero-copy transfer
            const buffers = [];
            if (extras && extras.monte_carlo && extras.monte_carlo.rawData) {
                buffers.push(extras.monte_carlo.rawData);
            }

            self.postMessage({ type: 'EXTRAS_RESULT', extras }, buffers);
        }
    } catch (err) {
        self.postMessage({ type: 'ERROR', message: err.message });
    }
};

/**
 * Process heavy calculations using MathEngine
 */
function processExtras(results, payload) {
    const validResults = results.filter(r => !r.error && r.technicals && r.technicals.relative_performance);
    
    if (validResults.length === 0) return null;

    // 1. Correlation Matrix
    const tickers = validResults.map(r => r.ticker);
    const matrix = [];

    // Convert histories to Float64Arrays once
    const histories = validResults.map(r => new Float64Array(r.technicals.relative_performance.stock_history || []));

    for (let i = 0; i < tickers.length; i++) {
        const row = new Float64Array(tickers.length);
        for (let j = 0; j < tickers.length; j++) {
            if (i === j) {
                row[j] = 1.0;
            } else {
                row[j] = MathEngine.calculateCorrelation(histories[i], histories[j]);
            }
        }
        matrix.push(row);
    }

    // 2. Monte Carlo Simulation
    // Create a portfolio returns series
    const minLength = Math.min(...histories.map(h => h.length));
    const portfolioReturns = new Float64Array(minLength - 1);
    const totalWeight = validResults.reduce((sum, r) => sum + (r.weight || 1.0), 0);

    for (let i = 1; i < minLength; i++) {
        let periodRet = 0;
        validResults.forEach((r, idx) => {
            const h = histories[idx];
            const w = r.weight || 1.0;
            const singleRet = (h[i] / h[i-1]) - 1;
            periodRet += (singleRet * (w / totalWeight));
        });
        portfolioReturns[i-1] = periodRet;
    }

    const monte_carlo = MathEngine.runMonteCarlo(portfolioReturns, 12, 1000);

    // 3. PV Simulation (Keeping simplified for now, could be moved to MathEngine later)
    const pv_simulation = runPVSimulationInternal(validResults, histories, payload);

    return {
        correlation: { tickers, matrix },
        monte_carlo,
        pv_simulation
    };
}

/**
 * Internal PV Simulation Logic (Ported to use Float64Arrays)
 */
function runPVSimulationInternal(results, histories, payload) {
    const initialBalance = Number(payload.initial_balance || 10000);
    const monthlyContribution = Number(payload.monthly_contribution || 0);
    
    const minLength = Math.min(...histories.map(h => h.length));
    if (minLength < 2) return null;

    const totalWeight = results.reduce((sum, r) => sum + (r.weight || 1.0), 0);
    const initialWeights = results.map(r => (r.weight || 1.0) / totalWeight);

    let currentBalance = initialBalance;
    let currentBenchmark = initialBalance;

    const balanceHistory = new Float64Array(minLength);
    const benchmarkHistory = new Float64Array(minLength);
    const drawdownSeries = new Float64Array(minLength);

    balanceHistory[0] = currentBalance;
    benchmarkHistory[0] = currentBenchmark;
    drawdownSeries[0] = 0;

    let maxBalance = currentBalance;
    let maxDrawdown = 0;

    const dates = results[0].technicals.relative_performance.dates || [];

    for (let t = 1; t < minLength; t++) {
        let periodReturn = 0;
        let periodBmReturn = 0;

        results.forEach((r, idx) => {
            const h = histories[idx];
            const bmHist = r.technicals.relative_performance.bm_history; // Benchmark might not be Float64 yet

            if (h[t-1] > 0) {
                periodReturn += ((h[t] / h[t-1]) - 1) * initialWeights[idx];
            }
            
            if (bmHist && bmHist[t-1] > 0) {
                periodBmReturn += ((bmHist[t] / bmHist[t-1]) - 1) * initialWeights[idx];
            }
        });

        currentBalance = currentBalance * (1 + periodReturn) + monthlyContribution;
        currentBenchmark = currentBenchmark * (1 + periodBmReturn) + monthlyContribution;

        balanceHistory[t] = currentBalance;
        benchmarkHistory[t] = currentBenchmark;

        if (currentBalance > maxBalance) maxBalance = currentBalance;
        const dd = maxBalance > 0 ? ((maxBalance - currentBalance) / maxBalance) * 100 : 0;
        drawdownSeries[t] = -dd;
        if (dd > maxDrawdown) maxDrawdown = dd;
    }

    const totalInvested = initialBalance + (monthlyContribution * (minLength - 1));
    const totalReturn = totalInvested > 0 ? (currentBalance - totalInvested) / totalInvested : 0;
    const weeksToYear = (minLength - 1) / 52;
    const cagr = weeksToYear > 0 ? (Math.pow(1 + totalReturn, 1 / weeksToYear) - 1) * 100 : 0;

    // Sharpe calculation (Simple)
    const periodReturns = new Float64Array(minLength - 1);
    for (let t = 1; t < minLength; t++) {
        periodReturns[t-1] = (balanceHistory[t] / balanceHistory[t-1]) - 1;
    }
    const sharpe = MathEngine.calculateSharpeRatio(periodReturns, 0, 52);

    return {
        metrics: {
            cagr: MathEngine.round(cagr, 1), 
            max_drawdown: MathEngine.round(maxDrawdown, 1),
            sharpe: sharpe,
            drawdown_series: Array.from(drawdownSeries) // Convert back for JSON safety if not using buffer
        },
        final_balance: Math.round(currentBalance),
        dates: dates.slice(0, minLength),
        balance_history: Array.from(balanceHistory.map(v => Math.round(v))),
        benchmark_history: Array.from(benchmarkHistory.map(v => Math.round(v)))
    };
}
