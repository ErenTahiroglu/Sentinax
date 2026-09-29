import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const read = (p) => readFileSync(resolve(process.cwd(), p), 'utf8');

describe('custom element contracts', () => {
    it.each(['x-analysis-card', 'x-analysis-grid', 'x-analysis-table'])(
        '%s is a valid HTMLElement custom element',
        async (tag) => {
            await import('../../js/components/AnalysisComponents.js');
            const ctor = customElements.get(tag);
            expect(ctor).toBeDefined();
            expect(ctor.prototype instanceof HTMLElement).toBe(true);

            let el;
            expect(() => { el = document.createElement(tag); }).not.toThrow();
            expect(el).toBeInstanceOf(HTMLElement);
            expect(el).toBeInstanceOf(ctor);
        }
    );

    it('x-analysis-table renders canonical appState results when connected', async () => {
        await import('../../js/components/AnalysisComponents.js');
        const { appState } = await import('../../js/core/appState.js');
        window.fmtNum = (v) => String(v ?? '');
        const table = document.createElement('x-analysis-table');
        document.body.appendChild(table);
        try {
            appState.results = [{ ticker: 'AAPL' }];
            expect(table.querySelector('tbody').textContent).toContain('AAPL');
        } finally {
            table.remove();
            appState.results = [];
        }
    });

    it('x-analysis-table stops reacting after disconnect', async () => {
        await import('../../js/components/AnalysisComponents.js');
        const { appState } = await import('../../js/core/appState.js');
        const table = document.createElement('x-analysis-table');
        document.body.appendChild(table);
        table.remove();
        appState.results = [{ ticker: 'MSFT' }];
        try {
            expect(table.textContent).not.toContain('MSFT');
        } finally {
            appState.results = [];
        }
    });
});

describe('skeleton card contract', () => {
    it('createSkeletonCard(ticker) yields the id ResultsComponent looks up', async () => {
        const { createSkeletonCard } = await import('../../js/components/CardComponent.js');
        const el = createSkeletonCard('AAPL');
        expect(el.id).toBe('skeleton-AAPL');
        expect(el.classList.contains('skeleton-card')).toBe(true);
    });
});

describe('auth DOM and bootstrap contracts', () => {
    const html = read('frontend/index.html');

    it('index.html has exactly one #login-modal, and it is the full auth modal', () => {
        const ids = html.match(/id="login-modal"/g) || [];
        expect(ids).toHaveLength(1);

        const start = html.indexOf('id="login-modal"');
        const modal = html.slice(start);
        for (const id of ['auth-form', 'auth-email', 'auth-password', 'auth-pw-eye', 'auth-google-btn', 'auth-submit-btn']) {
            expect(modal).toContain(`id="${id}"`);
        }
    });

    it('app bootstrap wires setupAuthModal exactly once', () => {
        const app = read('frontend/js/app.js');
        expect(app).toMatch(/import\s*\{[^}]*setupAuthModal[^}]*\}\s*from\s*['"]\.\/network\/supabaseClient\.js['"]/);
        expect(app.match(/setupAuthModal\(\)/g) || []).toHaveLength(1);
    });
});
