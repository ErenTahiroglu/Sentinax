/**
 * 🧩 App Orchestrator (app.js) - Component-Based Bootstrap
 * ======================================================
 */

import { appState } from './core/appState.js';
import { AnalysisService } from './services/AnalysisService.js';
import { ToolbarComponent } from './components/ToolbarComponent.js';
import { ResultsComponent } from './components/ResultsComponent.js';
import { 
    initTheme, 
    toggleTheme, 
    loadApiKeys, 
    saveApiKeys, 
    setupAutocomplete 
} from './utils.js';
import { checkServerHealth } from './network/api.js';
import { toggleNotifications } from './components/Notifications.js';

class App {
    constructor() {
        this.analysisService = new AnalysisService(appState);
        this.components = {};
    }

    async init() {
        console.log("🚀 Starting App (Component Mode)...");

        // 1. Initialize Non-Reactive Core
        initTheme();
        await loadApiKeys();
        setupAutocomplete();

        // 2. Instantiate Components (They handle their own DOM bindings)
        this.components.toolbar = new ToolbarComponent(this.analysisService);
        this.components.results = new ResultsComponent(this.analysisService);

        // 3. Global Static Bindings (Header/Sidebar)
        this.bindStaticUI();

        // 4. Background Services
        this.checkHealth();
        
        console.log("✅ App Initialized Successfully.");
    }

    bindStaticUI() {
        document.getElementById("theme-toggle-btn")?.addEventListener("click", toggleTheme);
        document.getElementById("notifications-btn")?.addEventListener("click", toggleNotifications);
        document.getElementById("api-key")?.addEventListener("blur", saveApiKeys);
        
        // Mobile Sidebar
        const mobBtn = document.getElementById("mobile-menu-btn");
        const sidebar = document.getElementById("sidebar");
        mobBtn?.addEventListener("click", (e) => {
            e.stopPropagation();
            sidebar?.classList.toggle("open");
        });
    }

    async checkHealth() {
        const h = await checkServerHealth();
        const dot = document.getElementById("server-status-dot");
        if (dot) {
            dot.className = h.online ? "status-dot dot-green" : "status-dot dot-red";
            dot.title = h.online ? "Online" : "Offline";
        }
    }
}

// 🚀 Boot
const app = new App();
document.addEventListener("DOMContentLoaded", () => app.init());
