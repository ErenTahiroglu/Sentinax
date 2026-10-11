import { describe, it, expect, beforeEach, vi } from 'vitest';
import fs from 'fs';
import path from 'path';

const root = path.resolve(__dirname, '../../buffett');
const load = (f) => fs.readFileSync(path.join(root, f), 'utf8');

function boot() {
  document.body.innerHTML = `
    <div id="portfolio-grid"></div><div id="history-list"></div>
    <div id="detail-drawer"></div><div id="drawer-overlay"></div>
    <div id="drawer-content"></div><button id="close-drawer"></button>`;
  delete window.ui; delete window.api;
  (0, eval)(load('js/ui.js'));
  (0, eval)(load('js/api.js'));
}

const unavailable = {
  data_status: 'UNAVAILABLE', reason_code: 'NO_QUALIFIED_EVIDENCE',
  reason: 'Nitelikli kanıt yok', portfolio: [], changes: [], warnings: ['w'],
  as_of: null, generated_at: '2026-10-11T00:00:00+00:00',
};

describe('Buffett public UI fail-closed', () => {
  beforeEach(boot);

  it('renders explicit unavailable state, not an empty success grid', () => {
    window.ui.renderPortfolioGrid(unavailable);
    const grid = document.getElementById('portfolio-grid');
    expect(grid.querySelector('.unavailable-state')).not.toBeNull();
    expect(grid.textContent).toContain('Nitelikli kanıt yok');
    expect(grid.querySelector('.stock-card')).toBeNull();
    expect(grid.innerHTML).not.toMatch(/value-success/);
  });

  it('renders unavailable for history too', () => {
    window.ui.renderHistoryList(unavailable);
    const list = document.getElementById('history-list');
    expect(list.querySelector('.unavailable-state')).not.toBeNull();
    expect(list.textContent).not.toContain('No recent portfolio changes');
  });

  it('api failure yields unavailable state, never fake data', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('down'));
    const p = await window.api.fetchPortfolio();
    expect(p.data_status).toBe('UNAVAILABLE');
    expect(p.portfolio).toEqual([]);
    const h = await window.api.fetchHistory();
    expect(h.data_status).toBe('UNAVAILABLE');
    expect(h.changes).toEqual([]);
  });

  it('hostile reason string is not injected as HTML', () => {
    window.ui.renderPortfolioGrid({ ...unavailable, reason: '<img src=x onerror=alert(1)>' });
    expect(document.getElementById('portfolio-grid').querySelector('img')).toBeNull();
  });

  it('static page copy does not claim a live Top 10 selection', () => {
    expect(load('index.html')).not.toMatch(/Top 10 Value Stocks/);
  });
});
