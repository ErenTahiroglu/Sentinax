import { createOwnerMfaFlow, STATES } from './mfa_flow.js';
import { OWNER_AUTH_OPTIONS, SUPABASE_SDK_PATH, resolveOwnerClientConfig } from './client_config.js';
import { OWNER_PUBLIC_CONFIG } from './owner_public_config.js';

const MESSAGES = {
    SIGN_IN_FAILED: 'Giriş başarısız. Bilgileri kontrol edin.',
    INVALID_INPUT: 'Geçersiz giriş.',
    NOT_ALLOWED: 'Bu işlem şu an yapılamaz.',
    ENROLL_FAILED: 'Kurulum başlatılamadı.',
    CODE_REJECTED: 'Kod kabul edilmedi.',
    ENROLLMENT_CANCELLED: 'Çok fazla hatalı deneme; kurulum iptal edildi.',
    SUPERSEDED: 'Oturum durumu değişti; işlem iptal edildi. Tekrar deneyin.',
    BUSY: 'Önceki işlem sürüyor; bekleyin.',
    LOCKED: 'Oturum durumu doğrulanamadı; erişim kapalı.',
};

let pendingFactorId = null;
let addingFactor = false;

const $ = (id) => document.getElementById(id);
const VIEWS = {
    [STATES.SIGNED_OUT]: 'view-signin',
    [STATES.NEEDS_ENROLLMENT]: 'view-enroll',
    [STATES.NEEDS_CHALLENGE]: 'view-challenge',
    [STATES.UNLOCKED]: 'view-unlocked',
};

function showError(code) {
    const el = $('error');
    el.textContent = code ? (MESSAGES[code] || MESSAGES.LOCKED) : '';
    el.hidden = !code;
}

function clearEnrollmentSecrets() {
    $('secret').textContent = '';
    $('qr').removeAttribute('src');
    $('enroll-code').value = '';
    $('enroll-pending').hidden = true;
    $('enroll-start').hidden = false;
}

function render(view) {
    for (const id of Object.values(VIEWS)) $(id).hidden = true;
    const target = VIEWS[view.state];
    if (target) $(target).hidden = false;
    if (view.state === STATES.UNLOCKED && addingFactor) $('view-enroll').hidden = false;
    $('status').textContent = view.state === STATES.ERROR_LOCKED ? MESSAGES.LOCKED : '';
    $('btn-signout').hidden = view.state === STATES.SIGNED_OUT;
    $('btn-signout-all').hidden = view.state === STATES.SIGNED_OUT;
    if (view.state !== STATES.NEEDS_ENROLLMENT && view.state !== STATES.UNLOCKED) clearEnrollmentSecrets();

    const select = $('challenge-factor');
    select.replaceChildren();
    if (view.state === STATES.NEEDS_CHALLENGE) {
        for (const f of view.factors) {
            const opt = document.createElement('option');
            opt.value = f.id;
            opt.textContent = f.friendlyName || 'TOTP';
            select.appendChild(opt);
        }
    }
    if (view.state === STATES.UNLOCKED) {
        $('factor-summary').textContent = `Doğrulanmış TOTP faktörü: ${view.verifiedFactorCount}`;
        $('backup-hint').hidden = view.backupFactorReady;
        $('btn-add-factor').hidden = view.backupFactorReady;
    }
}

async function main() {
    const cfg = resolveOwnerClientConfig(OWNER_PUBLIC_CONFIG);
    if (!cfg) { $('status').textContent = MESSAGES.LOCKED; return; }
    let createClient;
    try {
        ({ createClient } = await import(SUPABASE_SDK_PATH));
    } catch {
        $('status').textContent = MESSAGES.LOCKED;
        return;
    }
    // Publishable key only. The session is in memory only (see client_config.js): nothing durable, nothing the legacy client can inherit.
    const client = createClient(cfg.url, cfg.key, { auth: { ...OWNER_AUTH_OPTIONS } });
    const flow = createOwnerMfaFlow(client.auth);
    flow.onChange(render);

    $('signin-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const r = await flow.signIn($('email').value.trim(), $('password').value);
        $('password').value = '';
        showError(r.ok ? null : r.code);
    });

    $('btn-enroll').addEventListener('click', async () => {
        const r = await flow.beginEnrollment($('factor-name').value);
        if (!r.ok) { showError(r.code); return; }
        showError(null);
        if (r.qrDataUri) $('qr').src = r.qrDataUri;
        $('secret').textContent = r.secret;
        pendingFactorId = r.factorId;
        $('enroll-pending').hidden = false;
        $('enroll-start').hidden = true;
    });

    $('enroll-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const snap = flow.snapshot();
        if (!snap.enrollmentPending) { showError('NOT_ALLOWED'); return; }
        const r = await flow.confirmEnrollment(pendingFactorId, $('enroll-code').value);
        $('enroll-code').value = '';
        if (r.ok) { addingFactor = false; pendingFactorId = null; clearEnrollmentSecrets(); showError(null); render(flow.snapshot()); } else { showError(r.code); if (r.code === 'ENROLLMENT_CANCELLED') { pendingFactorId = null; clearEnrollmentSecrets(); } }
    });

    $('btn-enroll-cancel').addEventListener('click', async () => {
        await flow.cancelEnrollment();
        addingFactor = false;
        pendingFactorId = null;
        clearEnrollmentSecrets();
        render(flow.snapshot());
        showError(null);
    });

    $('challenge-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const r = await flow.challenge($('challenge-factor').value, $('challenge-code').value);
        $('challenge-code').value = '';
        showError(r.ok ? null : r.code);
    });

    $('btn-add-factor').addEventListener('click', () => {
        addingFactor = true;
        render(flow.snapshot());
    });

    $('btn-signout').addEventListener('click', () => flow.signOut('local'));
    $('btn-signout-all').addEventListener('click', () => flow.signOut('global'));

    // Synchronous callback that never calls the SDK (reentrancy/deadlock safe); the flow schedules a coalesced refresh.
    client.auth.onAuthStateChange((event) => flow.notifyAuthEvent(event));
    await flow.refresh();
    // Re-verify periodically so a revoked session / removed factor cannot stay UNLOCKED.
    setInterval(() => { flow.refresh(); }, 30000);
}

main().catch(() => { $('status').textContent = MESSAGES.LOCKED; });
