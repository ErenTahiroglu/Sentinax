/**
 * DOM binding of the private application (Phase 25D1E3). The application root exists in the owner document and is shown ONLY while the private session
 * controller reports AUTHORIZED; every other status clears and hides it (text nodes only: no HTML-injection sink, no browser storage, no logging).
 * Navigation and the simple/advanced mode are in-memory view state and are reset whenever the view is hidden, so nothing carries across sessions.
 */
import { AUTHZ } from './private_session.js';

const STATUS_TEXT = Object.freeze({
    [AUTHZ.IDLE]: '',
    [AUTHZ.AUTHORIZING]: 'Sunucu sahip yetkilendirmesi doğrulanıyor…',
    [AUTHZ.AUTHORIZED]: '',
    [AUTHZ.DENIED]: 'Sunucu sahip yetkisini onaylamadı; özel alan kapalı.',
    [AUTHZ.SERVER_UNAVAILABLE]: 'Sunucu sahip yapılandırması yok veya hizmet kullanılamıyor; özel alan kapalı.',
    [AUTHZ.UNREACHABLE]: 'Özel API’ye ulaşılamadı; özel alan kapalı.',
    [AUTHZ.UNCONFIGURED]: 'Özel API yapılandırılmamış; özel alan kapalı.',
    [AUTHZ.MALFORMED]: 'Sunucu yanıtı doğrulanamadı; özel alan kapalı.',
    [AUTHZ.ERROR]: 'Sunucu yetkilendirmesi tamamlanamadı; özel alan kapalı.',
});

export function createPrivateShell(doc, session, { onSignOut, onSignOutAll }) {
    const $ = (id) => doc.getElementById(id);
    const root = $('app-root');
    const panels = [...root.querySelectorAll('[data-panel]')];
    const navButtons = [...root.querySelectorAll('[data-target]')];
    const modeButtons = [...root.querySelectorAll('[data-mode-set]')];

    function show(name) {
        panels.forEach((p) => { p.hidden = p.getAttribute('data-panel') !== name; });
        navButtons.forEach((b) => {
            if (b.getAttribute('data-target') === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
        });
    }
    function setMode(mode) {
        root.setAttribute('data-mode', mode);
        modeButtons.forEach((b) => b.setAttribute('aria-pressed', String(b.getAttribute('data-mode-set') === mode)));
    }
    function clearFields() {
        for (const id of ['live-reason', 'prov-version', 'prov-status', 'prov-reason-code', 'prov-generated']) $(id).textContent = '';
        $('prov-warnings').replaceChildren();
    }
    function hide() {
        root.hidden = true;
        doc.body.classList.remove('app-active');
        const shell = doc.querySelector('.owner-shell');
        if (shell) shell.hidden = false;
        clearFields();
        show('overview');
        setMode('simple');
    }
    function reveal(summary) {
        $('live-reason').textContent = summary.reason;
        $('prov-version').textContent = summary.contractVersion;
        $('prov-status').textContent = summary.dataStatus;
        $('prov-reason-code').textContent = summary.reasonCode;
        $('prov-generated').textContent = summary.generatedAt;
        const list = $('prov-warnings');
        list.replaceChildren();
        for (const w of summary.warnings) {
            const li = doc.createElement('li');
            li.textContent = w;
            list.appendChild(li);
        }
        const shell = doc.querySelector('.owner-shell');
        if (shell) shell.hidden = true;
        doc.body.classList.add('app-active');
        root.hidden = false;
    }

    function render({ status, summary }) {
        $('authz-status').textContent = STATUS_TEXT[status] ?? STATUS_TEXT[AUTHZ.ERROR];
        if (status === AUTHZ.AUTHORIZED && summary) reveal(summary); else hide();
    }

    navButtons.forEach((b) => b.addEventListener('click', () => show(b.getAttribute('data-target'))));
    modeButtons.forEach((b) => b.addEventListener('click', () => setMode(b.getAttribute('data-mode-set'))));
    $('btn-app-signout').addEventListener('click', () => onSignOut());
    $('btn-app-signout-all').addEventListener('click', () => onSignOutAll());

    session.onChange(render);
    hide();
    return { render };
}
