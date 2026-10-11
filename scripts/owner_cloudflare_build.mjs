// Phase 25D1C5: Cloudflare Pages artifact for the isolated owner bootstrap page.
//
//   Pages project settings (repo-root build, no external-root dependency):
//     Root directory: (empty = repository root)   Build command: node scripts/owner_cloudflare_build.mjs
//     Build output directory: deploy/owner-cloudflare/dist
//
// Reuses buildOwnerDeploy() (allowlist, SDK integrity, content scan) for the seven owner files, then adds exactly two platform files:
// 404.html (prevents Pages' default SPA fallback) and _headers (strict, catch-all). No _redirects, no Functions, no Worker.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildOwnerDeploy, DEFAULT_SOURCE } from './owner_deploy_build.mjs';
import { parseHeaders } from './owner_cloudflare_model.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const CF_DIR = path.join(REPO, 'deploy/owner-cloudflare');
export const DEFAULT_OUT = path.join(CF_DIR, 'dist');
export const PLATFORM_FILES = Object.freeze(['404.html', '_headers']);

function validateHeaders(text) {
    const rules = parseHeaders(text);
    if (rules.length !== 1 || rules[0].pattern !== '/*') throw new Error('_headers must contain exactly one catch-all "/*" rule');
    const h = Object.fromEntries(rules[0].set);
    const need = (k, ok, msg) => { if (!ok) throw new Error(`_headers invalid: ${msg || k}`); };
    need('cache-control', h['cache-control'] === 'no-store', 'Cache-Control must be no-store');
    need('x-content-type-options', h['x-content-type-options'] === 'nosniff');
    need('x-frame-options', h['x-frame-options'] === 'DENY');
    need('referrer-policy', h['referrer-policy'] === 'no-referrer');
    need('cross-origin-opener-policy', h['cross-origin-opener-policy'] === 'same-origin');
    need('cross-origin-resource-policy', h['cross-origin-resource-policy'] === 'same-origin');
    const csp = h['content-security-policy'] || '';
    need('csp', /script-src 'self'(;|$)/.test(csp) && /frame-ancestors 'none'/.test(csp) && /default-src 'none'/.test(csp), 'strict CSP missing');
    need('csp', !/unsafe-inline|unsafe-eval|\*/.test(csp), 'CSP contains unsafe-inline/unsafe-eval/wildcard');
    need('cors', rules[0].detach.includes('access-control-allow-origin'), 'must detach Pages default Access-Control-Allow-Origin');
}

export async function buildOwnerCloudflare({ outDir = DEFAULT_OUT, sourceDir = DEFAULT_SOURCE, cfDir = CF_DIR } = {}) {
    for (const f of PLATFORM_FILES) {
        if (!fs.existsSync(path.join(cfDir, f))) throw new Error(`missing required Cloudflare file: ${f}`);
    }
    const headers = fs.readFileSync(path.join(cfDir, '_headers'), 'utf8');
    try { validateHeaders(headers); } catch (e) { throw new Error(e.message.startsWith('_headers') ? e.message : `_headers: ${e.message}`); }
    const notFoundPage = fs.readFileSync(path.join(cfDir, '404.html'), 'utf8');
    if (/<script|<link|<iframe|<form|<input|src=|href=|https?:\/\//i.test(notFoundPage)) throw new Error('404.html must be a self-contained dead end (no script/link/reference)');

    const r = await buildOwnerDeploy({ outDir, sourceDir });
    for (const f of PLATFORM_FILES) fs.copyFileSync(path.join(cfDir, f), path.join(outDir, f));
    return { outDir, files: [...r.files, ...PLATFORM_FILES] };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    buildOwnerCloudflare().then(
        (r) => console.log(`owner cloudflare artifact: ${r.files.length} files -> ${path.relative(REPO, r.outDir)}`),
        (e) => { console.error(`OWNER CLOUDFLARE BUILD FAILED: ${e.message}`); process.exit(1); },
    );
}
