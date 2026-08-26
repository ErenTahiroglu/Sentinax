// ═══════════════════════════════════════
// REACTIVE WEB COMPONENT - PROGRESS BAR
// ═══════════════════════════════════════

class ReactiveProgressBar extends HTMLElement {
    constructor() {
        super();
        this.attachShadow({ mode: 'open' });
        this.unsubscribe = null;
    }

    connectedCallback() {
        this.render();
        
        // State Proxy'ye Abone Ol
        if (typeof AppState !== "undefined" && AppState.subscribe) {
            this.unsubscribe = AppState.subscribe((key, val) => {
                if (key === 'analysisProgress') {
                    const fill = this.shadowRoot.querySelector('#progress-fill');
                    if (fill) fill.style.width = `${val}%`;
                }
                if (key === 'analysisMessage') {
                    const text = this.shadowRoot.querySelector('#progress-text');
                    if (text) text.textContent = val;
                }
            });
        }
    }

    disconnectedCallback() {
        // Bellek sızıntısını önlemek (Memory Leak Prevention)
        if (this.unsubscribe) {
            this.unsubscribe();
        }
    }

    render() {
        this.shadowRoot.innerHTML = `
            <style>
                :host {
                    display: block;
                    width: 100%;
                }
                .progress-container {
                    background-color: var(--card-bg, #ffffff);
                    border: 1px solid var(--border-color, #e2e8f0);
                    border-radius: 0.75rem;
                    padding: 1rem;
                    margin-bottom: 2rem;
                    box-shadow: var(--shadow-sm, 0 1px 2px 0 rgba(0, 0, 0, 0.05));
                }
                .flex-between { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
                .text-sm { font-size: 0.875rem; font-weight: 500; color: var(--text-color, #1e293b); }
                .progress-bar {
                    width: 100%;
                    height: 0.5rem;
                    background-color: #f1f5f9;
                    border-radius: 9999px;
                    overflow: hidden;
                }
                .progress-fill {
                    height: 100%;
                    background: linear-gradient(90deg, #3b82f6, #8b5cf6);
                    width: 0%;
                    transition: width 0.4s ease-out;
                }
            </style>
            <div class="progress-container">
                <div class="flex-between">
                    <span id="progress-text" class="text-sm">Analiz Başlıyor...</span>
                </div>
                <div class="progress-bar">
                    <div id="progress-fill" class="progress-fill"></div>
                </div>
            </div>
        `;
    }
}

customElements.define('x-progress-bar', ReactiveProgressBar);
