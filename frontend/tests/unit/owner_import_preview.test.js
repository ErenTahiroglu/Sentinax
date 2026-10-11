// Phase 25D1E6: Deep Ocean "Portföy" import-preview foundation. All data is SYNTHETIC. In the deployed bundle NO preview service is connected: the view is explicitly unavailable
// and only a strictly validated authoritative preview envelope (sentinax_import_preview.v1) could ever be rendered. The renderer writes text nodes only.
import { describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  IMPORT_PREVIEW_CONTRACT, validateImportPreview, createImportPreviewView,
} from '../../owner/js/private_import_preview.js';
import { createPrivateShell } from '../../owner/js/private_shell.js';
import { AUTHZ } from '../../owner/js/private_session.js';

const repo = path.resolve(__dirname, '../../..');
const owner = path.join(repo, 'frontend/owner');
const read = (p) => fs.readFileSync(path.join(owner, p), 'utf8');
const html = read('index.html');
const SHA = (c) => c.repeat(64);

const record = (over = {}) => ({
  record_ordinal: 1, assessment_status: 'ready', diagnostics: [], transaction_type: 'buy', effective_date: '2026-08-28', executed_at: '2026-08-28T10:15:30+00:00',
  instrument_reference: 'AAPL', quantity: '10', unit_price: '150.00', trade_currency: 'USD', cash_amount: null, cash_currency: null, from_currency: null, from_amount: null,
  to_currency: null, to_amount: null, resolution_status: 'resolved', resolution_diagnostic_codes: [], candidate_count: 0, identical_raw_record_of: null, ...over,
});
const preview = (over = {}) => ({
  contract_version: IMPORT_PREVIEW_CONTRACT, status: 'structurally_previewable', source_key: 'sentinax_canonical_csv', parser_revision: 1, file_content_sha256: SHA('a'),
  raw_manifest_sha256: SHA('b'), parsed_manifest_sha256: SHA('c'), assessment_manifest_sha256: SHA('d'), draft_manifest_sha256: SHA('e'), resolution_manifest_sha256: SHA('f'),
  record_count: 1, ready_count: 1, rejected_count: 0, unresolved_count: 0, resolved_count: 1, not_required_count: 0, unresolved_instrument_count: 0, ambiguous_instrument_count: 0,
  resolver_key: 'synthetic.resolver', resolver_revision: 1, blocking_reasons: ['preview_only_no_commit_path', 'idempotency_and_deduplication_not_evaluated', 'owner_approval_not_granted'],
  commit_authorized: false, persistent_state_changed: false, ledger_transaction_ids_generated: false, file_error_code: null, records: [record()], ...over,
});

let doc;
beforeEach(() => {
  document.documentElement.innerHTML = html.replace(/<script[\s\S]*?<\/script>/gi, '');
  doc = document;
});

describe('contract validation fails closed', () => {
  it('accepts exactly the versioned envelope and returns a frozen copy', () => {
    const v = validateImportPreview(preview());
    expect(v).not.toBeNull();
    expect(Object.isFrozen(v)).toBe(true);
    expect(v.records[0].quantity).toBe('10');
    expect(IMPORT_PREVIEW_CONTRACT).toBe('sentinax_import_preview.v1');
  });
  it.each([
    ['null', null], ['array', []], ['string', 'x'], ['wrong version', preview({ contract_version: 'sentinax_import_preview.v2' })],
    ['commit authorised', preview({ commit_authorized: true })], ['state changed', preview({ persistent_state_changed: true })], ['ledger ids generated', preview({ ledger_transaction_ids_generated: true })],
    ['unknown status', preview({ status: 'ready' })], ['READY alias', preview({ status: 'committed' })], ['extra top-level key', { ...preview(), commit_url: '/x' }],
    ['missing key', (() => { const p = preview(); delete p.status; return p; })()], ['float count', preview({ record_count: 1.5 })], ['negative count', preview({ rejected_count: -1 })],
    ['string count', preview({ ready_count: '1' })], ['bad sha', preview({ file_content_sha256: 'xyz' })], ['upper-case sha', preview({ file_content_sha256: 'A'.repeat(64) })],
    ['count/records mismatch', preview({ record_count: 2 })], ['records not array', preview({ records: {} })],
    ['record extra key', preview({ records: [{ ...record(), transaction_id: 'x' }] })], ['record number quantity', preview({ records: [record({ quantity: 10 })] })],
    ['record float-like text', preview({ records: [record({ quantity: '1e3' })] })], ['record unknown assessment', preview({ records: [record({ assessment_status: 'approved' })] })],
    ['record unknown resolution', preview({ records: [record({ resolution_status: 'matched' })] })], ['record bad ordinal', preview({ records: [record({ record_ordinal: 0 })] })],
    ['record bad date', preview({ records: [record({ effective_date: '28/08/2026' })] })], ['blocking reason with markup', preview({ blocking_reasons: ['<b>x</b>'] })],
    ['blocking reasons not array', preview({ blocking_reasons: 'x' })], ['diagnostics bad shape', preview({ records: [record({ diagnostics: [{ code: 'x' }] })] })],
  ])('rejects %s', (_n, raw) => {
    expect(validateImportPreview(raw)).toBeNull();
  });
  it('never mutates its input and tolerates hostile getters/prototypes', () => {
    const raw = preview();
    const before = JSON.stringify(raw);
    validateImportPreview(raw);
    expect(JSON.stringify(raw)).toBe(before);
    expect(validateImportPreview(Object.create({ ...preview() }))).toBeNull();
    const hostile = preview();
    Object.defineProperty(hostile, 'status', { get() { throw new Error('boom'); }, enumerable: true });
    expect(validateImportPreview(hostile)).toBeNull();
  });
});

describe('default state: no preview service is connected', () => {
  it('the page declares the not-connected state, the supported format and the four-step flow, without any upload control', () => {
    const view = createImportPreviewView(doc);
    view.clear();
    const panel = doc.getElementById('panel-portfolio');
    const t = panel.textContent;
    expect(t).toMatch(/Önizleme hizmeti henüz bağlı değil/);
    expect(t).toMatch(/işlem geçmişi/i);
    expect(t).toMatch(/Mevcut pozisyon dökümü[^.]*desteklenmiyor/);
    expect(t).toMatch(/uydurulmaz/);
    expect(t).toMatch(/hiçbir şey kaydedilmez/i);
    const steps = [...panel.querySelectorAll('ol.import-steps > li')].map((li) => li.textContent);
    expect(steps).toHaveLength(4);
    expect(steps[0]).toMatch(/biçim/i);
    expect(steps[1]).toMatch(/geçerliliğ/i);
    expect(steps[2]).toMatch(/reddedilen|çözülmemiş/i);
    expect(steps[3]).toMatch(/kaydedilmediğini/i);
    expect(panel.querySelector('input, form, textarea, button[type=submit], [type=file], [draggable], [contenteditable]')).toBeNull();
    expect(doc.getElementById('import-preview-summary').textContent).toBe('');
    expect(doc.getElementById('import-preview-adv-body').children).toHaveLength(0);
  });
  it('the simple steps are outside .adv; the advanced panel is inside .adv (hidden in simple mode)', () => {
    expect(doc.querySelector('.adv #import-preview-advanced, .adv#import-preview-advanced')).not.toBeNull();
    expect(doc.querySelector('.adv ol.import-steps')).toBeNull();
    expect(doc.querySelector('ol.import-steps')).not.toBeNull();
  });
  it('rendering null or a malformed envelope shows an explicit unverifiable state, never a result', () => {
    const view = createImportPreviewView(doc);
    expect(view.render(null)).toBe(false);
    expect(doc.getElementById('import-preview-state-text').textContent).toMatch(/henüz bağlı değil/);
    expect(view.render({ ...preview(), commit_authorized: true })).toBe(false);
    expect(doc.getElementById('import-preview-state-text').textContent).toMatch(/doğrulanamadı/);
    expect(doc.getElementById('import-preview-summary').textContent).toBe('');
    expect(doc.getElementById('import-preview-adv-body').children).toHaveLength(0);
  });
});

describe('rendering an authoritative (here: synthetic) preview', () => {
  it('shows the simple summary and the advanced provenance without claiming approval or import', () => {
    const view = createImportPreviewView(doc);
    expect(view.render(preview())).toBe(true);
    const simple = doc.getElementById('import-preview-summary').textContent;
    expect(simple).toMatch(/Yapısal olarak önizlenebilir/);
    expect(simple).toMatch(/Hiçbir şey kaydedilmedi/);
    const panelText = doc.getElementById('panel-portfolio').textContent;
    expect(panelText).not.toMatch(/\bREADY\b|hazır|onaylandı|aktarıldı|içe aktarıldı|tamamlandı/i);
    const adv = doc.getElementById('import-preview-adv-body').textContent;
    for (const needle of ['sentinax_import_preview.v1', 'sentinax_canonical_csv', SHA('a'), SHA('d'), 'preview_only_no_commit_path', 'idempotency_and_deduplication_not_evaluated', 'owner_approval_not_granted', 'synthetic.resolver', 'resolved']) {
      expect(adv, needle).toContain(needle);
    }
    expect(adv).toContain('150.00');
    expect(doc.getElementById('import-preview-chip').textContent).not.toMatch(/AVAILABLE|EMPTY/);
  });
  it.each([
    ['file_invalid', /Dosya veya biçim geçersiz/], ['empty_valid', /Geçerli ama boş/], ['has_rejected_records', /Reddedilen kayıtlar var/],
    ['resolution_unavailable', /çözümleme kullanılamıyor/], ['needs_resolution_review', /incelemesi gerekli/], ['structurally_previewable', /onaylanmış değil/],
  ])('labels status %s distinctly and never as approved', (status, re) => {
    const view = createImportPreviewView(doc);
    const invalid = status === 'file_invalid' ? { file_error_code: 'csv_schema_invalid', file_content_sha256: null } : {};
    expect(view.render(preview({ status, record_count: 0, ready_count: 0, resolved_count: 0, records: [], ...invalid }))).toBe(true);
    expect(doc.getElementById('import-preview-summary').textContent).toMatch(re);
  });
  it('renders every adversarial string as inert text', () => {
    const evil = '<img src=x onerror="window.__pwn=1"><script>window.__pwn=2</script>';
    const view = createImportPreviewView(doc);
    const raw = preview({
      records: [record({ instrument_reference: evil, diagnostics: [{ code: 'invalid_decimal', message: evil, field_key: 'quantity' }], quantity: '1', resolution_status: 'unresolved', resolution_diagnostic_codes: ['instrument_not_found'] })],
    });
    expect(view.render(raw)).toBe(true);
    const root = doc.getElementById('panel-portfolio');
    expect(root.querySelector('img, script, [onerror], iframe, svg')).toBeNull();
    expect(root.textContent).toContain(evil);
    expect(globalThis.__pwn).toBeUndefined();
    expect([...root.querySelectorAll('*')].every((el) => [...el.attributes].every((a) => !/^on/i.test(a.name)))).toBe(true);
  });
  it('re-rendering replaces the previous result; clear() removes everything', () => {
    const view = createImportPreviewView(doc);
    view.render(preview());
    view.render(preview({ status: 'empty_valid', record_count: 0, ready_count: 0, resolved_count: 0, records: [] }));
    expect(doc.getElementById('import-preview-summary').textContent).toMatch(/Geçerli ama boş/);
    expect(doc.querySelectorAll('#import-preview-adv-body table.import-table tbody tr')).toHaveLength(0);
    view.clear();
    expect(doc.getElementById('import-preview-summary').textContent).toBe('');
    expect(doc.getElementById('import-preview-adv-body').children).toHaveLength(0);
    expect(doc.getElementById('import-preview-state-text').textContent).toMatch(/henüz bağlı değil/);
  });
  it('bounds the number of rendered rows and says so', () => {
    const many = Array.from({ length: 250 }, (_, i) => record({ record_ordinal: i + 1 }));
    const view = createImportPreviewView(doc);
    expect(view.render(preview({ record_count: 250, ready_count: 250, resolved_count: 250, records: many }))).toBe(true);
    expect(doc.querySelectorAll('#import-preview-adv-body table.import-table tbody tr')).toHaveLength(200);
    expect(doc.getElementById('import-preview-adv-body').textContent).toMatch(/ilk 200 kayıt/);
  });
  it('shows duplicates as separate rows with the informational pointer, not merged', () => {
    const view = createImportPreviewView(doc);
    view.render(preview({ record_count: 2, ready_count: 2, resolved_count: 2, records: [record(), record({ record_ordinal: 2, identical_raw_record_of: 1 })] }));
    const rows = doc.querySelectorAll('#import-preview-adv-body table.import-table tbody tr');
    expect(rows).toHaveLength(2);
    expect(rows[1].textContent).toContain('1');
  });
});

describe('shell integration', () => {
  const fakeSession = () => { const s = { cb: null, onChange(fn) { s.cb = fn; } }; return s; };
  const summary = { reason: 'r', contractVersion: 'private_portfolio_summary.v1', dataStatus: 'UNAVAILABLE', reasonCode: 'x', generatedAt: 't', warnings: [] };
  it('exposes renderImportPreview, and hiding the application (sign-out, identity change, denial) clears a rendered preview', () => {
    const session = fakeSession();
    const shell = createPrivateShell(doc, session, { onSignOut() {}, onSignOutAll() {} });
    expect(typeof shell.renderImportPreview).toBe('function');
    session.cb({ status: AUTHZ.AUTHORIZED, summary });
    expect(shell.renderImportPreview(preview())).toBe(true);
    expect(doc.getElementById('import-preview-summary').textContent).not.toBe('');
    session.cb({ status: AUTHZ.DENIED, summary: null });
    expect(doc.getElementById('import-preview-summary').textContent).toBe('');
    expect(doc.getElementById('import-preview-adv-body').children).toHaveLength(0);
    expect(doc.getElementById('app-root').hidden).toBe(true);
  });
  it('authorising the application alone shows no preview (nothing feeds it in the deployed bundle)', () => {
    const session = fakeSession();
    createPrivateShell(doc, session, { onSignOut() {}, onSignOutAll() {} });
    session.cb({ status: AUTHZ.AUTHORIZED, summary });
    expect(doc.getElementById('import-preview-summary').textContent).toBe('');
    expect(doc.getElementById('import-preview-state-text').textContent).toMatch(/henüz bağlı değil/);
  });
});

describe('static boundaries of the deployed bundle', () => {
  const mod = read('js/private_import_preview.js');
  it('the renderer module uses no storage, network, SDK, logging, HTML-injection or location API', () => {
    for (const b of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', 'postMessage', 'BroadcastChannel', 'window.open',
      'window.name', 'eval(', 'new Function', 'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'serviceWorker', 'console.', 'import(', 'fetch(', 'createClient', 'FileReader',
      'File(', 'Blob', 'URL.createObjectURL', 'FormData', 'Math.random', 'Date.now', 'new Date']) {
      expect(mod, `must not contain ${b}`).not.toContain(b);
    }
    expect(mod).not.toMatch(/location\.|history\.|URLSearchParams|document\.referrer|supabase|Bearer|Authorization|parseFloat|Number\(|parseInt/i);
  });
  it('nothing in the deployed bundle feeds a preview: owner_app.js never references the preview renderer', () => {
    const app = read('js/owner_app.js');
    expect(app).not.toMatch(/import_preview|ImportPreview|renderImportPreview|createImportPreviewView/);
    const session = read('js/private_session.js');
    expect(session).not.toMatch(/import_preview|ImportPreview|import-preview/);
  });
  it('the owner document has no upload mechanism, no preview endpoint and no mock transactions', () => {
    expect(html).not.toMatch(/type="file"|<input[^>]+file|enctype|FormData|dragover|\/private\/portfolio\/import|\/import-preview|multipart/i);
    expect(read('js/private_session.js')).not.toMatch(/\/private\/portfolio\/import|\/import-preview/);
    expect(html).not.toMatch(/AAPL|MSFT|synthetic|sahte işlem/i);
    for (const f of ['js/private_import_preview.js', 'js/private_shell.js']) expect(read(f), f).not.toMatch(/AAPL|MSFT|BIST:|THYAO/);
  });
  it('the backend exposes no import or preview route and no new private write surface', () => {
    const router = fs.readFileSync(path.join(repo, 'backend/api/routers/private_portfolio.py'), 'utf8');
    expect(router).not.toMatch(/@router\.(post|put|patch|delete)|import_preview|commit_import/);
    expect(fs.readFileSync(path.join(repo, 'backend/engine/private/portfolio/import_preview.py'), 'utf8')).not.toMatch(/APIRouter|FastAPI|@router/);
  });
  it('no fabricated portfolio value appears in the static portfolio panel', () => {
    const panel = html.match(/<section id="panel-portfolio"[\s\S]*?<\/section>/)[0];
    const t = panel.replace(/<[^>]+>/g, ' ');
    expect(t).not.toMatch(/\d/);
    expect(t).not.toMatch(/[%₺$€£]/);
    expect(t).toContain('UNAVAILABLE');
  });
});
