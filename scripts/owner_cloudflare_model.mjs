// Phase 25D1C5: explicit MODEL of how a Cloudflare Pages project serves the owner artifact. Built only from documented behaviour
// (developers.cloudflare.com/pages/configuration/{serving-pages,headers,redirects}); NOT Cloudflare. Real behaviour must be observed
// on a deployment before anything here is called verified.
import fs from 'node:fs';
import path from 'node:path';

export const ASSUMPTIONS = Object.freeze([
    'DOCUMENTED: without a top-level 404.html Pages assumes an SPA and serves "/" for any non-asset path (the build must always ship 404.html).',
    'DOCUMENTED: "/x.html" redirects to "/x" and "/dir/index.html" redirects to "/dir/".',
    'ASSUMED (documented for Workers Static Assets, not for Pages): "/dir" redirects to "/dir/" for a folder index; redirect status code unknown (model uses 308).',
    'ASSUMED: dot-segment and encoded-traversal paths never reach an asset (platform normalisation).',
    'ASSUMED: _headers also applies to 404 responses served from 404.html.',
    'ASSUMED: the platform never serves _headers itself (documented: "will not itself be served as a static asset").',
    'ASSUMED: .mjs is served with a JavaScript MIME type (required for the module import of the SDK).',
]);

// Headers Pages always/sometimes adds (documented); _headers overrides or detaches them.
export const PAGES_DEFAULT_HEADERS = Object.freeze({
    'access-control-allow-origin': '*',
    'referrer-policy': 'strict-origin-when-cross-origin',
    'x-content-type-options': 'nosniff',
    'cache-control': 'public, max-age=0, must-revalidate',
    server: 'cloudflare',
});

export function parseHeaders(text) {
    const rules = [];
    for (const raw of text.split('\n')) {
        if (!raw.trim()) continue;
        if (raw.startsWith('#')) continue; // comments are valid only at column 0; an indented '#' could be read as a header line by the platform
        if (raw.trim().startsWith('#')) throw new Error('_headers: indented comment is ambiguous');
        if (!/^\s/.test(raw)) { rules.push({ pattern: raw.trim(), set: [], detach: [] }); continue; }
        const rule = rules[rules.length - 1];
        if (!rule) throw new Error('_headers: header line before any URL pattern');
        const line = raw.trim();
        if (line.startsWith('!')) { rule.detach.push(line.slice(1).trim().toLowerCase()); continue; }
        const i = line.indexOf(':');
        if (i < 1) throw new Error(`_headers: malformed line "${line.slice(0, 40)}"`);
        rule.set.push([line.slice(0, i).trim().toLowerCase(), line.slice(i + 1).trim()]);
    }
    return rules;
}

// Only the catch-all "/*" is supported on purpose: no pattern precedence or merge ambiguity can exist.
export function headersFor(headersText, _pathname) {
    const out = { ...PAGES_DEFAULT_HEADERS };
    const overridden = new Set();
    for (const rule of parseHeaders(headersText)) {
        if (rule.pattern !== '/*') throw new Error(`unsupported _headers pattern ${rule.pattern}: use a single "/*" rule`);
        for (const k of rule.detach) delete out[k];
        for (const [k, v] of rule.set) {
            out[k] = overridden.has(k) ? `${out[k]}, ${v}` : v; // documented: a header applied twice in _headers is comma-joined
            overridden.add(k);
        }
    }
    return out;
}

const notFound = (distDir) => {
    const f = path.join(distDir, '404.html');
    return fs.existsSync(f) ? { status: 404, file: f } : { status: 200, spaFallback: true, file: path.join(distDir, 'index.html') };
};

export function resolveRequest(distDir, pathname) {
    let p;
    try { p = decodeURIComponent(pathname); } catch { return notFound(distDir); }
    if (p.split('/').some((s) => s === '..' || s === '.') || p.includes('\\') || !p.startsWith('/')) return notFound(distDir);
    if (p.endsWith('/index.html')) return { status: 308, location: p.slice(0, -'index.html'.length) };
    if (p.endsWith('.html')) return { status: 308, location: p.slice(0, -'.html'.length) };
    if (/^\/(404|_headers|_redirects|_routes\.json)$/.test(p)) return notFound(distDir);
    const fsPath = path.join(distDir, p);
    if (!fsPath.startsWith(distDir)) return notFound(distDir);
    const stat = fs.existsSync(fsPath) ? fs.statSync(fsPath) : null;
    if (stat?.isFile()) return { status: 200, file: fsPath };
    if (fs.existsSync(`${fsPath}.html`) && !p.endsWith('/')) return { status: 200, file: `${fsPath}.html` };
    if (stat?.isDirectory() && fs.existsSync(path.join(fsPath, 'index.html'))) {
        return p.endsWith('/') ? { status: 200, file: path.join(fsPath, 'index.html') } : { status: 308, location: `${p}/` };
    }
    return notFound(distDir);
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
export const contentType = (file) => TYPES[path.extname(file)] || 'application/octet-stream';
