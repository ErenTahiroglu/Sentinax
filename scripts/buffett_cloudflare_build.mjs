// Phase 25D1E5: deterministic Cloudflare Pages artifact of the PUBLIC Buffett interface (a project separate from the owner project).
//
//   Pages settings (repo-root build, no external-root dependency):
//     Root directory: (empty = repository root)   Build command: node scripts/buffett_cloudflare_build.mjs
//     Build output directory: deploy/buffett-cloudflare/dist
//     Optional environment variables (NOT secrets): BUFFETT_OWNER_ORIGIN (exact https origin of the isolated owner project), BUFFETT_PUBLIC_ORIGIN (this site's own https origin)
//
// Files are named explicitly (no recursive copy of frontend/). The owner-entry configuration is GENERATED from a validated value (build variable, else the reviewed source
// configuration, else null = disabled link) so the emitted file is small and inspectable. Any invalid value fails the build; it never silently falls back.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { parseHeaders } from './owner_cloudflare_model.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const SOURCE_DIR = path.join(REPO, 'frontend/buffett');
export const CF_DIR = path.join(REPO, 'deploy/buffett-cloudflare');
export const DEFAULT_OUT = path.join(CF_DIR, 'dist');
export const PUBLIC_ALLOWLIST = Object.freeze(['index.html', 'css/styles.css', 'js/owner_entry.js', 'js/api.js', 'js/ui.js', 'js/app.js']);
export const CONFIG_FILE = 'js/owner_entry_config.js';
export const PLATFORM_FILES = Object.freeze(['404.html', '_headers']);
const APPROVED_PATH = '/owner/';

const FORBIDDEN = [
    [/\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write|createContextualFragment|\beval\(|new Function|\.srcdoc|setAttribute\(\s*['"`](?:on|style)/, 'HTML-injection or code-execution sink'],
    [/<script(?![^>]*\bsrc=)/i, 'inline script'],
    [/\son[a-z]+\s*=|\sstyle\s*=|<style\b/i, 'inline handler or inline style'],
    [/https?:\/\/(?!www\.w3\.org\/)/i, 'external URL'],
    [/fonts\.(?:googleapis|gstatic)|@import/i, 'external font or stylesheet import'],
    [/Authorization|Bearer|supabase|createClient|sb_(?:secret|publishable)_|service_role|SUPABASE|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./i, 'authentication or credential material'],
    [/owner_app|mfa_flow|private_session|private_shell|persistSession|signInWithPassword|\/owner\/js\//, 'owner or private-application reference'],
    [/<(?:form|input|textarea|select|dialog)\b/i, 'form or input element'],
];

function loadConfig(text) {
    const sandbox = {};
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(text, sandbox, { timeout: 1000 });
    return sandbox.OWNER_ENTRY_CONFIG;
}

function validateOwnerOrigin(origin, publicOrigin, validatorSource) {
    if (origin === null) return null;
    const sandbox = {};
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(validatorSource, sandbox, { timeout: 1000 });
    // The very validator the page runs at load time decides, against a stand-in public origin (or the supplied one): https only, exact shape, different origin.
    const loc = publicOrigin ? { protocol: 'https:', hostname: new URL(publicOrigin).hostname, origin: publicOrigin } : { protocol: 'https:', hostname: 'public.invalid', origin: 'https://public.invalid' };
    if (typeof origin !== 'string' || !origin.startsWith('https://') || sandbox.ownerEntry.resolve({ origin, path: APPROVED_PATH }, loc) === null) {
        throw new Error(`invalid owner origin ${JSON.stringify(String(origin)).slice(0, 80)}: expected an exact https origin (host only), different from the public origin`);
    }
    return origin;
}

function validatePublicOrigin(publicOrigin) {
    if (publicOrigin === undefined || publicOrigin === null || publicOrigin === '') return null;
    if (typeof publicOrigin !== 'string' || !/^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(publicOrigin)) {
        throw new Error(`invalid public origin ${JSON.stringify(String(publicOrigin)).slice(0, 80)}: expected an exact https origin`);
    }
    return publicOrigin;
}

function validateHeaders(text) {
    const rules = parseHeaders(text);
    if (rules.length !== 1 || rules[0].pattern !== '/*') throw new Error('_headers must contain exactly one catch-all "/*" rule');
    const h = Object.fromEntries(rules[0].set);
    const need = (ok, msg) => { if (!ok) throw new Error(`_headers invalid: ${msg}`); };
    need(h['x-content-type-options'] === 'nosniff', 'X-Content-Type-Options must be nosniff');
    need(h['x-frame-options'] === 'DENY', 'X-Frame-Options must be DENY');
    need(h['referrer-policy'] === 'no-referrer', 'Referrer-Policy must be no-referrer');
    need(h['cross-origin-opener-policy'] === 'same-origin' && h['cross-origin-resource-policy'] === 'same-origin', 'COOP/CORP must be same-origin');
    need(/max-age=\d{8,}/.test(h['strict-transport-security'] || ''), 'HSTS missing');
    const csp = h['content-security-policy'] || '';
    const dirs = Object.fromEntries(csp.split(';').map((d) => d.trim().split(/\s+/)).filter((d) => d[0]).map(([k, ...v]) => [k, v.join(' ')]));
    need(dirs['default-src'] === "'none'" && dirs['script-src'] === "'self'" && dirs['style-src'] === "'self'" && dirs['connect-src'] === "'self'", "strict CSP (default 'none', script/style/connect 'self') missing");
    need(dirs['frame-ancestors'] === "'none'" && dirs['object-src'] === "'none'" && dirs['base-uri'] === "'none'" && dirs['form-action'] === "'none'", 'CSP must forbid framing, objects, base and form targets');
    need(!/unsafe-inline|unsafe-eval|unsafe-hashes|strict-dynamic|\*|https?:|blob:|wss?:/.test(csp), 'CSP contains unsafe, wildcard or external sources');
    need(rules[0].detach.includes('access-control-allow-origin'), 'must detach the Pages default Access-Control-Allow-Origin');
}

export async function buildBuffettCloudflare({ outDir = DEFAULT_OUT, sourceDir = SOURCE_DIR, cfDir = CF_DIR, ownerOrigin = process.env.BUFFETT_OWNER_ORIGIN, publicOrigin = process.env.BUFFETT_PUBLIC_ORIGIN } = {}) {
    for (const rel of [...PUBLIC_ALLOWLIST, CONFIG_FILE]) {
        if (!fs.existsSync(path.join(sourceDir, rel))) throw new Error(`missing required public file: ${rel}`);
    }
    for (const f of PLATFORM_FILES) {
        if (!fs.existsSync(path.join(cfDir, f))) throw new Error(`missing required Cloudflare file: ${f}`);
    }
    const headers = fs.readFileSync(path.join(cfDir, '_headers'), 'utf8');
    try { validateHeaders(headers); } catch (e) { throw new Error(e.message.startsWith('_headers') ? e.message : `_headers: ${e.message}`); }
    const notFound = fs.readFileSync(path.join(cfDir, '404.html'), 'utf8');
    if (/<script|<link|<iframe|<form|<input|src=|href=|https?:\/\//i.test(notFound) || /Buffett|Sahip|owner|Sentinax/i.test(notFound)) throw new Error('404.html must be a self-contained dead end (no script/link/reference/application text)');

    for (const rel of PUBLIC_ALLOWLIST) {
        const text = fs.readFileSync(path.join(sourceDir, rel), 'utf8');
        for (const [re, what] of FORBIDDEN) if (re.test(text)) throw new Error(`forbidden content in ${rel}: ${what}`);
    }

    const sourceConfig = loadConfig(fs.readFileSync(path.join(sourceDir, CONFIG_FILE), 'utf8'));
    if (!sourceConfig || typeof sourceConfig !== 'object' || sourceConfig.path !== APPROVED_PATH || Object.keys(sourceConfig).length !== 2) throw new Error('invalid owner origin configuration in the source file');
    const pub = validatePublicOrigin(publicOrigin);
    const requested = ownerOrigin !== undefined && ownerOrigin !== '' ? ownerOrigin : sourceConfig.origin;
    const origin = validateOwnerOrigin(requested === undefined ? null : requested, pub, fs.readFileSync(path.join(sourceDir, 'js/owner_entry.js'), 'utf8'));
    if (pub && origin && new URL(origin).origin === pub) throw new Error('invalid owner origin: equals the public origin');

    fs.rmSync(outDir, { recursive: true, force: true });
    const put = (rel, content) => {
        const dest = path.join(outDir, rel);
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.writeFileSync(dest, content);
    };
    for (const rel of PUBLIC_ALLOWLIST) put(rel, fs.readFileSync(path.join(sourceDir, rel)));
    put(CONFIG_FILE, `// Generated by scripts/buffett_cloudflare_build.mjs (Phase 25D1E5): pinned, validated owner-login destination; null = the link stays disabled.\nwindow.OWNER_ENTRY_CONFIG = Object.freeze({ origin: ${JSON.stringify(origin)}, path: '${APPROVED_PATH}' });\n`);
    for (const f of PLATFORM_FILES) put(f, fs.readFileSync(path.join(cfDir, f)));
    return { outDir, files: [...PUBLIC_ALLOWLIST, CONFIG_FILE, ...PLATFORM_FILES].sort(), ownerOrigin: origin, digest: crypto.createHash('sha256').update(fs.readFileSync(path.join(outDir, 'index.html'))).digest('hex') };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    const arg = process.argv.find((a) => a.startsWith('--owner-origin='));
    buildBuffettCloudflare(arg ? { ownerOrigin: arg.slice('--owner-origin='.length) } : {}).then(
        (r) => console.log(`buffett cloudflare artifact: ${r.files.length} files -> ${path.relative(REPO, r.outDir)} (owner origin: ${r.ownerOrigin ?? 'none, link disabled'})`),
        (e) => { console.error(`BUFFETT CLOUDFLARE BUILD FAILED: ${e.message}`); process.exit(1); },
    );
}
