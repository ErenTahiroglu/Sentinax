// Phase 25D1E5: the public Buffett renderer must never turn source-derived text into HTML. Every AVAILABLE payload below is a SYNTHETIC MOCK fixture that exists only
// to exercise the renderer; live financial evidence remains UNAVAILABLE (the backend returns nothing else) and nothing here qualifies any source or any company.
import { describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';

const root = path.resolve(__dirname, '../../buffett');
const load = (f) => fs.readFileSync(path.join(root, f), 'utf8');

function boot() {
  document.body.innerHTML = `
    <div id="portfolio-grid"></div><div id="history-list"></div>
    <div id="detail-drawer" class="hidden"></div><div id="drawer-overlay" class="hidden"></div>
    <div id="drawer-content"></div><button id="close-drawer"></button>`;
  delete window.ui; delete window.api; delete window.__xss;
  (0, eval)(load('js/ui.js'));
}

const MOCK_STOCK = (over = {}) => ({
  ticker: 'MOCKA', name: 'MOCK Holding A.S.', total_score: 72.5, current_price: 10.25, target_entry: [8, 9.5], margin_of_safety_pct: 12.345,
  details: {
    moat_score: 15, profitability_score: 20, balance_sheet_score: 12, valuation_score: 25,
    dcf: { base_case: 11, bear_case: 8, bull_case: 14, intrinsic_value: 11 }, llm_analysis: 'MOCK analysis text', passed_veto: true,
  },
  ...over,
});
const available = (portfolio) => ({ data_status: 'AVAILABLE', reason_code: 'MOCK', reason: 'mock', portfolio, changes: [], warnings: [], as_of: '2026-01-01T00:00:00+00:00', generated_at: '2026-01-01T00:00:00+00:00' });
const availableHistory = (changes) => ({ data_status: 'AVAILABLE', reason_code: 'MOCK', reason: 'mock', portfolio: [], changes, warnings: [], as_of: '2026-01-01T00:00:00+00:00', generated_at: '2026-01-01T00:00:00+00:00' });

const PAYLOADS = [
  '<img src=x onerror="window.__xss=1">',
  '<script>window.__xss=1</script>',
  '"><svg onload=window.__xss=1>',
  "' onmouseover='window.__xss=1' x='",
  '<a href="javascript:window.__xss=1">x</a>',
  '</div><iframe srcdoc="<script>window.__xss=1</script>"></iframe>',
  '<style>*{display:none}</style>',
];
const ALLOWED_TAGS = new Set(['DIV', 'SPAN', 'H2', 'H3', 'H4', 'P']);
const walk = (root) => [...root.querySelectorAll('*')];
function assertInert(container) {
  for (const el of walk(container)) {
    expect(ALLOWED_TAGS.has(el.tagName), `unexpected <${el.tagName.toLowerCase()}> created from data`).toBe(true);
    for (const a of el.attributes) {
      expect(a.name.startsWith('on'), `event handler attribute ${a.name}`).toBe(false);
      expect(['class', 'style'].includes(a.name), `unexpected attribute ${a.name}`).toBe(true);
      if (a.name === 'style') expect(a.value).toMatch(/^width:\s*[0-9.]+%;?$/);
    }
  }
  expect(window.__xss).toBeUndefined();
}
const cardText = () => document.getElementById('portfolio-grid').textContent;
const openDrawer = (i = 0) => document.querySelectorAll('#portfolio-grid .stock-card')[i].click();

beforeEach(boot);

describe('adversarial text is rendered literally, never as HTML (SAFE rendering of MOCK records)', () => {
  it.each(PAYLOADS)('company name %# appears as literal text in the card and the drawer', (p) => {
    window.ui.renderPortfolioGrid(available([MOCK_STOCK({ name: p })]));
    const grid = document.getElementById('portfolio-grid');
    assertInert(grid);
    expect(grid.textContent).toContain(p);
    openDrawer();
    const drawer = document.getElementById('drawer-content');
    assertInert(drawer);
    expect(drawer.textContent).toContain(p);
  });

  it.each(PAYLOADS)('ticker %# appears as literal text', (p) => {
    window.ui.renderPortfolioGrid(available([MOCK_STOCK({ ticker: p })]));
    assertInert(document.getElementById('portfolio-grid'));
    expect(cardText()).toContain(p);
    openDrawer();
    assertInert(document.getElementById('drawer-content'));
    expect(document.getElementById('drawer-content').textContent).toContain(p);
  });

  it.each(PAYLOADS)('LLM analysis %# appears as literal text inside the quotes', (p) => {
    const s = MOCK_STOCK();
    s.details.llm_analysis = p;
    window.ui.renderPortfolioGrid(available([s]));
    openDrawer();
    const drawer = document.getElementById('drawer-content');
    assertInert(drawer);
    expect(drawer.querySelector('.llm-analysis').textContent).toBe(`"${p}"`);
  });

  it.each(PAYLOADS)('history reason, ticker and date %# appear as literal text', (p) => {
    window.ui.renderHistoryList(availableHistory([{ action: 'ADDED', ticker: p, reason: p, date: p }]));
    const list = document.getElementById('history-list');
    assertInert(list);
    expect(list.querySelector('h4').textContent).toBe(p);
    expect(list.querySelector('.history-content p').textContent).toBe(p);
    expect(list.querySelector('.history-date').textContent.trim()).toBe(p);
  });

  it('a hostile history action cannot choose a CSS class or inject markup; unknown actions are invalid evidence', () => {
    window.ui.renderHistoryList(availableHistory([{ action: '"><img src=x onerror=1>', ticker: 'MOCKA', reason: 'r', date: '2026-01-01' }]));
    const list = document.getElementById('history-list');
    assertInert(list);
    expect(list.querySelector('.invalid-evidence')).not.toBeNull();
    expect(list.querySelector('.badge-added, .badge-removed')).toBeNull();
  });

  it('data can never create an executable element, script, handler or hyperlink anywhere in the page', () => {
    window.ui.renderPortfolioGrid(available(PAYLOADS.map((p) => MOCK_STOCK({ name: p, ticker: p }))));
    window.ui.renderHistoryList(availableHistory(PAYLOADS.map((p) => ({ action: 'REMOVED', ticker: p, reason: p, date: p }))));
    openDrawer(0);
    expect(document.querySelectorAll('script, img, svg, iframe, object, embed, link, style, form, input, a, [onclick], [onerror], [onload], [onmouseover]')).toHaveLength(0);
    expect(window.__xss).toBeUndefined();
  });
});

describe('deterministic, validated formatting of SYNTHETIC valid records', () => {
  it('formats score, price, entry band, margin and bar width with fixed rules and static classes', () => {
    window.ui.renderPortfolioGrid(available([MOCK_STOCK(), MOCK_STOCK({ ticker: 'MOCKB', margin_of_safety_pct: -3 })]));
    const [a, b] = document.querySelectorAll('#portfolio-grid .stock-card');
    expect(a.querySelector('.score-badge').textContent).toBe('72.5');
    const values = [...a.querySelectorAll('.metric-value')].map((n) => n.textContent);
    expect(values).toEqual(['₺10.25', '₺8 - ₺9.5', '12.35%']);
    expect(a.querySelector('.metric-value.value-success')).not.toBeNull();
    expect(b.querySelector('.metric-value.value-danger')).not.toBeNull();
    expect(a.querySelector('.score-bar-fill').style.width).toBe('72.5%');
  });

  it('scores are clamped to the bar range without changing the displayed value', () => {
    window.ui.renderPortfolioGrid(available([MOCK_STOCK({ total_score: 140 })]));
    expect(document.querySelector('.score-bar-fill').style.width).toBe('100%');
  });
});

describe('malformed AVAILABLE payloads fail closed: no crash, no fabricated or zero values, no NaN/Infinity', () => {
  const BAD_NUMBERS = [undefined, null, 'abc', '12', NaN, Infinity, -Infinity, {}, [], true];
  it.each(['total_score', 'current_price', 'margin_of_safety_pct'])('invalid %s yields an invalid-evidence card and no number', (field) => {
    for (const bad of BAD_NUMBERS) {
      boot();
      const s = MOCK_STOCK();
      if (bad === undefined) delete s[field]; else s[field] = bad;
      expect(() => window.ui.renderPortfolioGrid(available([s, MOCK_STOCK({ ticker: 'MOCKOK' })]))).not.toThrow();
      const cards = document.querySelectorAll('#portfolio-grid .stock-card');
      expect(cards).toHaveLength(2);
      expect(cards[0].classList.contains('invalid-evidence'), String(bad)).toBe(true);
      const t = cards[0].textContent;
      expect(t).not.toMatch(/NaN|Infinity|undefined|null|\[object|₺|%|\d\.\d/);
      expect(cards[1].classList.contains('invalid-evidence')).toBe(false);                    // the valid neighbour still renders
      expect(cardText()).toContain('MOCKOK');
    }
  });

  it.each([undefined, null, [], [1], [1, 2, 3], ['a', 'b'], [NaN, 1], [1, Infinity], { 0: 1, 1: 2 }, 'x'])('invalid target_entry %j is invalid evidence', (bad) => {
    const s = MOCK_STOCK();
    if (bad === undefined) delete s.target_entry; else s.target_entry = bad;
    window.ui.renderPortfolioGrid(available([s]));
    const card = document.querySelector('#portfolio-grid .stock-card');
    expect(card.classList.contains('invalid-evidence')).toBe(true);
    expect(card.textContent).not.toMatch(/NaN|Infinity|undefined|null|₺/);
  });

  it.each([undefined, null, 'x', 5, [], {}, { dcf: null }, { dcf: {} }, { moat_score: 'a', dcf: { base_case: 1, bear_case: 1, bull_case: 1, intrinsic_value: 1 }, llm_analysis: 'x', passed_veto: true }])(
    'missing or malformed nested valuation details %j never crash the drawer and never show invented numbers', (bad) => {
      const s = MOCK_STOCK();
      if (bad === undefined) delete s.details; else s.details = bad;
      window.ui.renderPortfolioGrid(available([s]));
      expect(() => openDrawer()).not.toThrow();
      const drawer = document.getElementById('drawer-content');
      expect(drawer.textContent).toContain('doğrulanamadı');
      expect(drawer.textContent).not.toMatch(/NaN|Infinity|undefined|null|₺|\d+\/\d+/);
      expect(document.getElementById('detail-drawer').classList.contains('hidden')).toBe(false);
    });

  it.each([undefined, 'yes', 1, 0, null])('a non-boolean veto flag (%j) is reported as unverified, not as passed or failed', (v) => {
    const s = MOCK_STOCK();
    if (v === undefined) delete s.details.passed_veto; else s.details.passed_veto = v;
    window.ui.renderPortfolioGrid(available([s]));
    openDrawer();
    const t = document.getElementById('drawer-content').textContent;
    expect(t).toContain('Veto durumu doğrulanamadı');
    expect(t).not.toMatch(/başarıyla geçti|takıldı/);
  });

  it.each([null, undefined, 'x', 5, {}, { a: 1 }, true])('a portfolio that is not an array (%j) is rendered as unavailable (invalid evidence), never as an empty success grid', (bad) => {
    const st = available(bad);
    if (bad === undefined) delete st.portfolio;
    expect(() => window.ui.renderPortfolioGrid(st)).not.toThrow();
    const grid = document.getElementById('portfolio-grid');
    expect(grid.querySelector('.unavailable-state')).not.toBeNull();
    expect(grid.querySelector('.stock-card')).toBeNull();
  });

  it.each([null, undefined, 5, 'x', [], [1], true])('portfolio entry %j is invalid evidence and does not break its neighbours', (bad) => {
    expect(() => window.ui.renderPortfolioGrid(available([bad, MOCK_STOCK({ ticker: 'MOCKOK' })]))).not.toThrow();
    const cards = document.querySelectorAll('#portfolio-grid .stock-card');
    expect(cards).toHaveLength(2);
    expect(cards[0].classList.contains('invalid-evidence')).toBe(true);
    expect(cardText()).toContain('MOCKOK');
  });

  it('non-string names/tickers are invalid evidence, not stringified', () => {
    window.ui.renderPortfolioGrid(available([MOCK_STOCK({ name: { toString: () => '<img src=x>' } }), MOCK_STOCK({ ticker: 123 }), MOCK_STOCK({ name: '' })]));
    const cards = document.querySelectorAll('#portfolio-grid .stock-card');
    expect([...cards].every((c) => c.classList.contains('invalid-evidence'))).toBe(true);
    expect(document.querySelectorAll('img')).toHaveLength(0);
  });

  it.each([null, undefined, 'x', 5, {}, true])('history changes that are not an array (%j) are unavailable (invalid evidence)', (bad) => {
    const st = availableHistory(bad);
    if (bad === undefined) delete st.changes;
    expect(() => window.ui.renderHistoryList(st)).not.toThrow();
    expect(document.getElementById('history-list').querySelector('.unavailable-state')).not.toBeNull();
  });

  it.each([null, 5, 'x', [], { action: 'ADDED' }, { action: 'ADDED', ticker: 5, reason: 'r', date: 'd' }, { action: 'ADDED', ticker: 't', reason: {}, date: 'd' }, { action: 'ADDED', ticker: 't', reason: 'r', date: null }])(
    'malformed history entry %j is invalid evidence with no invented text', (bad) => {
      expect(() => window.ui.renderHistoryList(availableHistory([bad]))).not.toThrow();
      const item = document.querySelector('#history-list .history-item');
      expect(item.classList.contains('invalid-evidence')).toBe(true);
      expect(item.textContent).not.toMatch(/undefined|null|NaN|\[object/);
    });

  it('an empty AVAILABLE history shows a plain static message (text node), not markup', () => {
    window.ui.renderHistoryList(availableHistory([]));
    const list = document.getElementById('history-list');
    expect(list.querySelector('p').textContent).toBe('No recent portfolio changes.');
    assertInert(list);
  });

  it('the unavailable path is unchanged: a hostile reason or warning is text and no card is created', () => {
    window.ui.renderPortfolioGrid({ data_status: 'UNAVAILABLE', reason: PAYLOADS[0], warnings: [PAYLOADS[1]], portfolio: [] });
    const grid = document.getElementById('portfolio-grid');
    assertInert(grid);
    expect(grid.textContent).toContain(PAYLOADS[0]);
    expect(grid.querySelector('.stock-card')).toBeNull();
  });
});

describe('source-level guard on every public script', () => {
  const SINKS = /\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write|createContextualFragment|\beval\(|new Function|setTimeout\(\s*['"`]|setInterval\(\s*['"`]|\.srcdoc|setAttribute\(\s*['"`]on|setAttribute\(\s*['"`]style|\.cssText|javascript:/;
  it.each(['js/ui.js', 'js/api.js', 'js/app.js', 'js/owner_entry.js', 'js/owner_entry_config.js'])('%s contains no HTML-injection or code-execution sink', (f) => {
    expect(load(f)).not.toMatch(SINKS);
  });
  it('the page has no inline script, inline handler or inline style, so a strict CSP can forbid them', () => {
    const html = load('index.html');
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)/i);
    expect(html).not.toMatch(/\son[a-z]+\s*=|\sstyle\s*=/i);
  });
});
