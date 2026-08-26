/**
 * 📦 AppState - Reactive Application Store (Singleton)
 * ==================================================
 * Use imports to access the state: `import { appState } from './core/appState.js';`
 */

import { createStore } from './state.js';

const initialViewMode = localStorage.getItem("viewMode") || "beginner";

const initialState = {
    viewMode: initialViewMode,
    lang: localStorage.getItem("lang") || "tr",
    isHalalOnly: localStorage.getItem("isHalalOnly") === "true",
    commissionRate: parseFloat(localStorage.getItem("commissionRate")) || 0.2,
    slippageRate: parseFloat(localStorage.getItem("slippageRate")) || 0.1,
    results: [],
    extras: null,
    systemStatus: 'ready',
    isAnalyzing: false,
    analysisProgress: 0,
    analysisStatusMessage: '',
    analysisPayload: {
        tickers: [],
        use_ai: false,
        check_islamic: false,
        check_financials: false,
        model: "gemini-2.5-flash"
    }
};

export const appState = createStore(initialState);
