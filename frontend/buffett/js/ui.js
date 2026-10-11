// Public Buffett renderer (Phase 25D1E5).
//
// Every value that comes from the API is treated as untrusted DATA: it is validated, then written ONLY as text (textContent) into elements this file creates
// itself, with static CSS classes. No markup is ever built from data, no style/attribute/href/src is taken from data, and nothing is evaluated.
// Missing or malformed evidence is never turned into zero, NaN or a made-up value: the record is shown as "invalid evidence" without any figure.
// The UNAVAILABLE path is unchanged. Safely rendering a record is NOT a claim that any source is qualified or that data is available.

const MAX_TICKER = 64;
const MAX_NAME = 200;
const MAX_REASON = 1000;
const MAX_DATE = 64;
const MAX_ANALYSIS = 5000;
const MAX_ABS = 1e12;
const HISTORY_ACTIONS = ['ADDED', 'REMOVED'];
const INVALID_NOTE = 'Geçersiz kanıt: bu kayıt gösterilemiyor.';

const isText = (v, max) => typeof v === 'string' && v.length > 0 && v.length <= max;
const isNum = (v) => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= MAX_ABS;
const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const plain = (n) => Number(n.toFixed(4)).toString(); // deterministic, never exponent form for the ranges accepted above

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function renderUnavailable(container, state) {
    container.replaceChildren();
    const box = el('div', 'unavailable-state');
    box.appendChild(el('h3', '', 'Veri kullanılamıyor'));
    box.appendChild(el('p', '', isText(state && state.reason, MAX_REASON) ? state.reason : 'Veri kullanılamıyor.'));
    const warnings = state && Array.isArray(state.warnings) ? state.warnings : [];
    warnings.filter((w) => isText(w, MAX_REASON)).forEach((w) => box.appendChild(el('p', 'unavailable-warning', w)));
    container.appendChild(box);
}

function invalidEvidence(container, state) {
    renderUnavailable(container, { reason: 'Geçersiz kanıt: sunucu yanıtı doğrulanamadı.', warnings: state && state.warnings });
}

// ---- portfolio ------------------------------------------------------------------------------------------------------------------------------------------------

function validStock(s) {
    return isObject(s) && isText(s.ticker, MAX_TICKER) && isText(s.name, MAX_NAME)
        && isNum(s.total_score) && isNum(s.current_price) && isNum(s.margin_of_safety_pct)
        && Array.isArray(s.target_entry) && s.target_entry.length === 2 && s.target_entry.every(isNum);
}

function validDetails(d) {
    return isObject(d) && ['moat_score', 'profitability_score', 'balance_sheet_score', 'valuation_score'].every((k) => isNum(d[k]))
        && isObject(d.dcf) && ['base_case', 'bear_case', 'bull_case', 'intrinsic_value'].every((k) => isNum(d.dcf[k]))
        && isText(d.llm_analysis, MAX_ANALYSIS);
}

function metricRow(label, value, valueClass) {
    const row = el('div', 'metric-row');
    row.appendChild(el('span', 'metric-label', label));
    row.appendChild(el('span', valueClass ? `metric-value ${valueClass}` : 'metric-value', value));
    return row;
}

function stockCard(stock) {
    const card = el('div', 'stock-card');
    if (!validStock(stock)) {
        card.classList.add('invalid-evidence');
        const header = el('div', 'card-header');
        if (isObject(stock) && isText(stock.ticker, MAX_TICKER)) header.appendChild(el('span', 'ticker', stock.ticker));
        card.appendChild(header);
        card.appendChild(el('p', 'invalid-note', INVALID_NOTE));
        return card;
    }
    const scorePct = Math.min(100, Math.max(0, stock.total_score));

    const header = el('div', 'card-header');
    const id = el('div');
    id.appendChild(el('span', 'ticker', stock.ticker));
    id.appendChild(el('span', 'company-name', stock.name));
    header.appendChild(id);
    header.appendChild(el('div', 'score-badge', stock.total_score.toFixed(1)));
    card.appendChild(header);

    const metrics = el('div', 'card-metrics');
    metrics.appendChild(metricRow('Current Price', `₺${stock.current_price.toFixed(2)}`));
    metrics.appendChild(metricRow('Target Entry', `₺${plain(stock.target_entry[0])} - ₺${plain(stock.target_entry[1])}`));
    metrics.appendChild(metricRow('Margin of Safety', `${stock.margin_of_safety_pct.toFixed(2)}%`, stock.margin_of_safety_pct > 0 ? 'value-success' : 'value-danger'));

    const score = el('div', 'score-meta');
    const scoreHead = el('div', 'score-meta-head');
    scoreHead.appendChild(el('span', '', 'Buffett Score'));
    scoreHead.appendChild(el('span', '', `${scorePct.toFixed(1)}/100`));
    const barBg = el('div', 'score-bar-bg');
    const fill = el('div', 'score-bar-fill');
    fill.style.width = `${scorePct.toFixed(1)}%`; // numeric, validated and clamped: the only dynamic style, set through the CSS object model
    barBg.appendChild(fill);
    score.append(scoreHead, barBg);
    metrics.appendChild(score);
    card.appendChild(metrics);

    card.addEventListener('click', () => openDetailDrawer(stock));
    return card;
}

function renderPortfolioGrid(state) {
    const grid = document.getElementById('portfolio-grid');
    if (!state || state.data_status !== 'AVAILABLE') {
        renderUnavailable(grid, state);
        return;
    }
    if (!Array.isArray(state.portfolio)) {
        invalidEvidence(grid, state);
        return;
    }
    grid.replaceChildren(...state.portfolio.map(stockCard));
}

function scoreItem(label, value, extraClass) {
    const item = el('div', extraClass ? `score-item ${extraClass}` : 'score-item');
    item.appendChild(el('span', extraClass ? 'accent-text' : '', label));
    item.appendChild(el('span', extraClass ? 'accent-text strong' : '', value));
    return item;
}

function drawerSection(title, ...children) {
    const section = el('div', 'drawer-section');
    section.appendChild(el('h3', '', title));
    children.forEach((c) => section.appendChild(c));
    return section;
}

function openDetailDrawer(stock) {
    const drawer = document.getElementById('detail-drawer');
    const overlay = document.getElementById('drawer-overlay');
    const content = document.getElementById('drawer-content');
    if (!validStock(stock)) return;
    const d = stock.details;

    const head = el('div', 'drawer-header');
    head.appendChild(el('h2', '', stock.ticker));
    head.appendChild(el('p', '', stock.name));
    const parts = [head];

    if (!validDetails(d)) {
        parts.push(drawerSection('Ayrıntılar', el('p', 'invalid-note', 'Ayrıntılar doğrulanamadı: kanıt eksik veya geçersiz.')));
    } else {
        const subs = el('div', 'alt-scores');
        subs.append(
            scoreItem('Moat', `${plain(d.moat_score)}/20`), scoreItem('Profitability', `${plain(d.profitability_score)}/30`),
            scoreItem('Balance Sheet', `${plain(d.balance_sheet_score)}/20`), scoreItem('Valuation (MoS)', `${plain(d.valuation_score)}/30`),
        );
        const dcf = el('div', 'alt-scores');
        dcf.append(
            scoreItem('Base Case', `₺${plain(d.dcf.base_case)}`), scoreItem('Bear Case', `₺${plain(d.dcf.bear_case)}`), scoreItem('Bull Case', `₺${plain(d.dcf.bull_case)}`),
            scoreItem('Intrinsic Value (Avg)', `₺${plain(d.dcf.intrinsic_value)}`, 'dcf-total'),
        );
        parts.push(
            drawerSection('Alt Skorlar (Moat & Profitability)', subs),
            drawerSection('Değerleme (DCF Senaryoları)', dcf),
            drawerSection('Nitel Analiz (LLM)', el('p', 'llm-analysis', `"${d.llm_analysis}"`)),
        );
    }
    const veto = isObject(d) && typeof d.passed_veto === 'boolean'
        ? el('p', d.passed_veto ? 'veto-line veto-pass' : 'veto-line veto-fail', d.passed_veto ? '✓ Veto testlerinden başarıyla geçti.' : '✗ Veto testlerine takıldı.')
        : el('p', 'veto-line', 'Veto durumu doğrulanamadı.');
    parts.push(drawerSection('Veto Durumu', veto));

    content.replaceChildren(...parts);
    drawer.classList.remove('hidden');
    overlay.classList.remove('hidden');
}

function closeDetailDrawer() {
    document.getElementById('detail-drawer').classList.add('hidden');
    document.getElementById('drawer-overlay').classList.add('hidden');
}

document.getElementById('close-drawer').addEventListener('click', closeDetailDrawer);
document.getElementById('drawer-overlay').addEventListener('click', closeDetailDrawer);

// ---- history --------------------------------------------------------------------------------------------------------------------------------------------------

function validChange(c) {
    return isObject(c) && HISTORY_ACTIONS.includes(c.action) && isText(c.ticker, MAX_TICKER) && isText(c.reason, MAX_REASON) && isText(c.date, MAX_DATE);
}

function historyItem(change) {
    const item = el('div', 'history-item');
    if (!validChange(change)) {
        item.classList.add('invalid-evidence');
        item.appendChild(el('p', 'invalid-note', INVALID_NOTE));
        return item;
    }
    item.appendChild(el('div', change.action === 'ADDED' ? 'history-badge badge-added' : 'history-badge badge-removed', change.action));
    const body = el('div', 'history-content');
    body.appendChild(el('h4', '', change.ticker));
    body.appendChild(el('p', '', change.reason));
    item.appendChild(body);
    item.appendChild(el('div', 'history-date', change.date));
    return item;
}

function renderHistoryList(state) {
    const list = document.getElementById('history-list');
    if (!state || state.data_status !== 'AVAILABLE') {
        renderUnavailable(list, state);
        return;
    }
    if (!Array.isArray(state.changes)) {
        invalidEvidence(list, state);
        return;
    }
    if (state.changes.length === 0) {
        list.replaceChildren(el('p', '', 'No recent portfolio changes.'));
        return;
    }
    list.replaceChildren(...state.changes.map(historyItem));
}

window.ui = {
    renderPortfolioGrid,
    renderHistoryList
};
