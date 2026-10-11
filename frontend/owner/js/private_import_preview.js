/**
 * Import-preview view of the private application (Phase 25D1E6). It can render ONE thing: a strictly validated, versioned preview envelope
 * (sentinax_import_preview.v1) produced by the pure Python adapter. In the deployed bundle nothing supplies such an envelope: no preview service is connected,
 * there is no upload control and no network, storage or SDK access in this module. Every value is written as a text node; nothing is parsed as markup.
 * A preview is never an approval: the view states that nothing was saved and that the preview confers no import authority.
 */
export const IMPORT_PREVIEW_CONTRACT = 'sentinax_import_preview.v1';

const MAX_ROWS = 200;
const STATUS_LABEL = Object.freeze({
    file_invalid: 'Dosya veya biçim geçersiz',
    empty_valid: 'Geçerli ama boş (kayıt yok)',
    has_rejected_records: 'Reddedilen kayıtlar var',
    resolution_unavailable: 'Enstrüman çözümleme kullanılamıyor',
    needs_resolution_review: 'Enstrüman çözümleme incelemesi gerekli',
    structurally_previewable: 'Yapısal olarak önizlenebilir (onaylanmış değil)',
});
const ASSESSMENT = Object.freeze(['ready', 'unresolved', 'rejected']);
const RESOLUTION = Object.freeze(['resolved', 'not_required', 'unresolved', 'ambiguous', 'not_evaluated']);
const REASON_LABEL = Object.freeze({
    file_invalid: 'Dosya geçersiz.',
    no_records: 'Dosyada kayıt yok.',
    rejected_records: 'Reddedilen kayıtlar var.',
    unresolved_records: 'Değerlendirilemeyen kayıtlar var.',
    resolver_unavailable: 'Enstrüman çözümleyici bağlı değil.',
    resolver_error: 'Enstrüman çözümleyici hata verdi.',
    unresolved_instruments: 'Çözülmemiş enstrümanlar var.',
    ambiguous_instruments: 'Belirsiz enstrümanlar var.',
    preview_only_no_commit_path: 'Bu yalnızca önizlemedir; kaydetme yolu yoktur.',
    idempotency_and_deduplication_not_evaluated: 'Tekrar ve mükerrer kayıt denetimi yapılmadı.',
    owner_approval_not_granted: 'Sahip onayı verilmedi.',
});

const TOP_KEYS = Object.freeze([
    'contract_version', 'status', 'source_key', 'parser_revision', 'file_content_sha256', 'raw_manifest_sha256', 'parsed_manifest_sha256', 'assessment_manifest_sha256',
    'draft_manifest_sha256', 'resolution_manifest_sha256', 'record_count', 'ready_count', 'rejected_count', 'unresolved_count', 'resolved_count', 'not_required_count',
    'unresolved_instrument_count', 'ambiguous_instrument_count', 'resolver_key', 'resolver_revision', 'blocking_reasons', 'commit_authorized', 'persistent_state_changed',
    'ledger_transaction_ids_generated', 'file_error_code', 'records',
].sort());
const RECORD_KEYS = Object.freeze([
    'record_ordinal', 'assessment_status', 'diagnostics', 'transaction_type', 'effective_date', 'executed_at', 'instrument_reference', 'quantity', 'unit_price', 'trade_currency',
    'cash_amount', 'cash_currency', 'from_currency', 'from_amount', 'to_currency', 'to_amount', 'resolution_status', 'resolution_diagnostic_codes', 'candidate_count',
    'identical_raw_record_of',
].sort());
const COUNT_KEYS = Object.freeze(['record_count', 'ready_count', 'rejected_count', 'unresolved_count', 'resolved_count', 'not_required_count', 'unresolved_instrument_count', 'ambiguous_instrument_count']);
const DIGEST_KEYS = Object.freeze(['file_content_sha256', 'raw_manifest_sha256', 'parsed_manifest_sha256', 'assessment_manifest_sha256', 'draft_manifest_sha256', 'resolution_manifest_sha256']);

const CODE = /^[a-z][a-z0-9_]{0,63}$/;
const KEYLIKE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const DECIMAL = /^-?\d{1,60}(\.\d{1,60})?$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
const CURRENCY = /^[A-Z]{3}$/;

const isCount = (n) => Number.isSafeInteger(n) && n >= 0;
const isPositive = (n) => Number.isSafeInteger(n) && n >= 1;
const nullOr = (v, test) => v === null || test(v);
const str = (v, re, max = 512) => typeof v === 'string' && v.length <= max && (re ? re.test(v) : true);

/** Own, plain, data-only object whose key set is exactly `keys`; any accessor or foreign prototype is rejected. */
function plainWithKeys(o, keys) {
    if (o === null || typeof o !== 'object' || Array.isArray(o) || Object.getPrototypeOf(o) !== Object.prototype) return null;
    const own = Object.keys(o).sort();
    if (own.length !== keys.length || own.some((k, i) => k !== keys[i])) return null;
    const out = {};
    for (const k of own) {
        const d = Object.getOwnPropertyDescriptor(o, k);
        if (!d || !('value' in d)) return null;
        out[k] = d.value;
    }
    return out;
}

function validateDiagnostic(d) {
    const o = plainWithKeys(d, ['code', 'field_key', 'message']);
    if (!o || !str(o.code, CODE) || !str(o.message, null, 500) || !o.message || !nullOr(o.field_key, (v) => str(v, CODE))) return null;
    return Object.freeze({ code: o.code, message: o.message, field_key: o.field_key });
}

function validateRecord(r) {
    const o = plainWithKeys(r, RECORD_KEYS);
    if (!o || !isPositive(o.record_ordinal) || !ASSESSMENT.includes(o.assessment_status) || !RESOLUTION.includes(o.resolution_status)) return null;
    if (!Array.isArray(o.diagnostics) || o.diagnostics.length > 16 || !Array.isArray(o.resolution_diagnostic_codes) || o.resolution_diagnostic_codes.length > 16) return null;
    const diagnostics = o.diagnostics.map(validateDiagnostic);
    if (diagnostics.some((d) => d === null) || !o.resolution_diagnostic_codes.every((c) => str(c, CODE))) return null;
    const ok = nullOr(o.transaction_type, (v) => str(v, CODE))
        && nullOr(o.effective_date, (v) => str(v, DATE, 10)) && nullOr(o.executed_at, (v) => str(v, INSTANT, 40))
        && nullOr(o.instrument_reference, (v) => typeof v === 'string' && v.length <= 256)
        && ['quantity', 'unit_price', 'cash_amount', 'from_amount', 'to_amount'].every((k) => nullOr(o[k], (v) => str(v, DECIMAL, 125)))
        && ['trade_currency', 'cash_currency', 'from_currency', 'to_currency'].every((k) => nullOr(o[k], (v) => str(v, CURRENCY, 3)))
        && isCount(o.candidate_count) && nullOr(o.identical_raw_record_of, isPositive);
    if (!ok) return null;
    return Object.freeze({ ...o, diagnostics: Object.freeze(diagnostics), resolution_diagnostic_codes: Object.freeze([...o.resolution_diagnostic_codes]) });
}

/** Returns a deeply frozen copy of a valid envelope, or null. Never throws, never mutates its input. */
export function validateImportPreview(raw) {
    try {
        const o = plainWithKeys(raw, TOP_KEYS);
        if (!o || o.contract_version !== IMPORT_PREVIEW_CONTRACT || !Object.hasOwn(STATUS_LABEL, o.status)) return null;
        if (o.commit_authorized !== false || o.persistent_state_changed !== false || o.ledger_transaction_ids_generated !== false) return null;
        if (!COUNT_KEYS.every((k) => isCount(o[k]))) return null;
        if (!DIGEST_KEYS.every((k) => nullOr(o[k], (v) => str(v, SHA256, 64)))) return null;
        if (!nullOr(o.source_key, (v) => str(v, KEYLIKE)) || !nullOr(o.resolver_key, (v) => str(v, KEYLIKE)) || !nullOr(o.file_error_code, (v) => str(v, CODE))) return null;
        if (!nullOr(o.parser_revision, isPositive) || !nullOr(o.resolver_revision, isPositive)) return null;
        if (!Array.isArray(o.blocking_reasons) || o.blocking_reasons.length > 32 || !o.blocking_reasons.every((c) => str(c, CODE))) return null;
        if (!Array.isArray(o.records)) return null;
        const records = o.records.map(validateRecord);
        if (records.some((r) => r === null) || records.length !== o.record_count) return null;
        if (o.ready_count + o.rejected_count + o.unresolved_count !== o.record_count) return null;
        if (o.status === 'file_invalid') {
            if (o.file_error_code === null || o.record_count !== 0) return null;
        } else if (o.file_error_code !== null || o.file_content_sha256 === null) return null;
        if (o.status === 'empty_valid' && o.record_count !== 0) return null;
        return Object.freeze({ ...o, blocking_reasons: Object.freeze([...o.blocking_reasons]), records: Object.freeze(records) });
    } catch {
        return null;
    }
}

export function createImportPreviewView(doc) {
    const $ = (id) => doc.getElementById(id);
    const el = (tag, text, cls) => {
        const e = doc.createElement(tag);
        if (text !== undefined) e.textContent = text;
        if (cls) e.className = cls;
        return e;
    };
    const dash = (v) => (v === null || v === undefined ? '—' : String(v));

    function setState(kind, text) {
        const chip = $('import-preview-chip');
        if (kind === 'preview') { chip.textContent = 'ÖNİZLEME'; chip.className = 'chip chip-preview'; } else { chip.textContent = 'UNAVAILABLE'; chip.className = 'chip chip-unavailable'; }
        $('import-preview-state-text').textContent = text;
    }
    function clearResult() {
        $('import-preview-summary').replaceChildren();
        $('import-preview-adv-body').replaceChildren();
    }
    function clear() {
        clearResult();
        setState('unavailable', 'Önizleme hizmeti henüz bağlı değil. Dosya yükleme yok; hiçbir şey kaydedilmez.');
    }

    function renderSummary(v) {
        const box = $('import-preview-summary');
        box.replaceChildren();
        box.appendChild(el('p', STATUS_LABEL[v.status], 'import-status'));
        box.appendChild(el('p', `Kayıt: ${v.record_count}; okunabilir: ${v.ready_count}; reddedilen: ${v.rejected_count}; değerlendirilemeyen: ${v.unresolved_count}; `
            + `çözülmemiş enstrüman: ${v.unresolved_instrument_count}; belirsiz enstrüman: ${v.ambiguous_instrument_count}.`));
        const list = el('ul', undefined, 'warn-list');
        for (const code of v.blocking_reasons) list.appendChild(el('li', REASON_LABEL[code] ?? 'Başka engel nedenleri var (gelişmiş görünüm).'));
        box.appendChild(list);
        box.appendChild(el('p', 'Hiçbir şey kaydedilmedi; bu önizleme içe aktarma yetkisi vermez.', 'note'));
    }

    function kvTable(rows) {
        const table = el('table', undefined, 'prov');
        const body = el('tbody');
        for (const [k, val] of rows) {
            const tr = el('tr');
            const th = el('th', k);
            th.setAttribute('scope', 'row');
            tr.appendChild(th);
            tr.appendChild(el('td', dash(val)));
            body.appendChild(tr);
        }
        table.appendChild(body);
        return table;
    }

    function renderAdvanced(v) {
        const box = $('import-preview-adv-body');
        box.replaceChildren();
        box.appendChild(kvTable([
            ['contract_version', v.contract_version], ['status', v.status], ['source_key', v.source_key], ['parser_revision', v.parser_revision], ['file_error_code', v.file_error_code],
            ['file_content_sha256', v.file_content_sha256], ['raw_manifest_sha256', v.raw_manifest_sha256], ['parsed_manifest_sha256', v.parsed_manifest_sha256],
            ['assessment_manifest_sha256', v.assessment_manifest_sha256], ['draft_manifest_sha256', v.draft_manifest_sha256], ['resolution_manifest_sha256', v.resolution_manifest_sha256],
            ['record_count', v.record_count], ['ready_count', v.ready_count], ['rejected_count', v.rejected_count], ['unresolved_count', v.unresolved_count],
            ['resolved_count', v.resolved_count], ['not_required_count', v.not_required_count], ['unresolved_instrument_count', v.unresolved_instrument_count],
            ['ambiguous_instrument_count', v.ambiguous_instrument_count], ['resolver_key', v.resolver_key], ['resolver_revision', v.resolver_revision],
            ['commit_authorized', String(v.commit_authorized)], ['persistent_state_changed', String(v.persistent_state_changed)],
            ['ledger_transaction_ids_generated', String(v.ledger_transaction_ids_generated)],
        ]));
        box.appendChild(el('h4', 'Engel nedenleri (kod)'));
        const reasons = el('ul', undefined, 'warn-list');
        for (const code of v.blocking_reasons) reasons.appendChild(el('li', code));
        box.appendChild(reasons);

        if (v.records.length === 0) return;
        box.appendChild(el('h4', 'Kayıtlar'));
        const scroller = el('div', undefined, 'import-scroll');
        const table = el('table', undefined, 'import-table');
        const head = el('tr');
        for (const h of ['sıra', 'değerlendirme', 'işlem', 'tarih', 'enstrüman', 'miktar', 'fiyat', 'para birimi', 'nakit', 'FX', 'çözümleme', 'tanı kodları', 'aynı ham kayıt']) {
            const th = el('th', h);
            th.setAttribute('scope', 'col');
            head.appendChild(th);
        }
        const thead = el('thead');
        thead.appendChild(head);
        table.appendChild(thead);
        const body = el('tbody');
        for (const r of v.records.slice(0, MAX_ROWS)) {
            const tr = el('tr');
            const codes = [...r.diagnostics.map((d) => d.code), ...r.resolution_diagnostic_codes];
            const cash = r.cash_amount === null ? null : `${r.cash_amount} ${dash(r.cash_currency)}`;
            const fx = r.from_amount === null && r.to_amount === null ? null : `${dash(r.from_amount)} ${dash(r.from_currency)} → ${dash(r.to_amount)} ${dash(r.to_currency)}`;
            for (const cell of [r.record_ordinal, r.assessment_status, r.transaction_type, r.effective_date, r.instrument_reference, r.quantity, r.unit_price, r.trade_currency, cash, fx,
                r.resolution_status, codes.length ? codes.join(', ') : null, r.identical_raw_record_of]) tr.appendChild(el('td', dash(cell)));
            body.appendChild(tr);
        }
        table.appendChild(body);
        scroller.appendChild(table);
        box.appendChild(scroller);
        if (v.records.length > MAX_ROWS) box.appendChild(el('p', `Yalnızca ilk ${MAX_ROWS} kayıt gösteriliyor; kalanlar önizlemede kısaltıldı.`, 'note'));
    }

    /** @returns {boolean} true only if a valid envelope was rendered. */
    function render(raw) {
        clearResult();
        if (raw === null || raw === undefined) { setState('unavailable', 'Önizleme hizmeti henüz bağlı değil. Dosya yükleme yok; hiçbir şey kaydedilmez.'); return false; }
        const v = validateImportPreview(raw);
        if (!v) { setState('unavailable', 'Önizleme sonucu doğrulanamadı; hiçbir sonuç gösterilmiyor ve hiçbir şey kaydedilmedi.'); return false; }
        setState('preview', 'Yetkili önizleme sonucu gösteriliyor. Hiçbir şey kaydedilmedi.');
        renderSummary(v);
        renderAdvanced(v);
        return true;
    }

    return { render, clear };
}
