// Phase 25D1C3: explicit model of how the isolated owner project serves its artifact (cleanUrls, trailingSlash:false, no
// rewrites/fallbacks, one catch-all header rule). Used by the unit contract and the local HTTP smoke server.
// This is a MODEL of documented Vercel behaviour, not Vercel: the real deployment must be observed separately.
import fs from 'node:fs';
import path from 'node:path';

export function resolveRequest(distDir, config, pathname) {
    let p;
    try { p = decodeURIComponent(pathname); } catch { return { status: 404 }; }
    if (p !== pathname && /(^|\/)\.\.?(\/|$)|\\/.test(p)) return { status: 404 };
    if (p.split('/').some((s) => s === '..' || s === '.')) return { status: 404 };
    if (config.cleanUrls && p.endsWith('/index.html')) return { status: 308, location: p.slice(0, -'/index.html'.length) || '/' };
    if (config.cleanUrls && p.endsWith('.html')) return { status: 308, location: p.slice(0, -'.html'.length) };
    if (config.trailingSlash === false && p.length > 1 && p.endsWith('/')) return { status: 308, location: p.slice(0, -1) };
    const fsPath = path.join(distDir, p);
    if (!fsPath.startsWith(distDir)) return { status: 404 };
    const stat = fs.existsSync(fsPath) ? fs.statSync(fsPath) : null;
    if (stat?.isFile()) return { status: 200, file: fsPath };
    if (stat?.isDirectory() && fs.existsSync(path.join(fsPath, 'index.html'))) return { status: 200, file: path.join(fsPath, 'index.html') };
    if (config.cleanUrls && fs.existsSync(`${fsPath}.html`)) return { status: 200, file: `${fsPath}.html` };
    return { status: 404 };
}

// Header rules: only "/(.*)" (match everything) is supported on purpose, so no precedence/merge ambiguity exists.
export function headersFor(config, _pathname) {
    const out = {};
    for (const rule of config.headers || []) {
        if (rule.source !== '/(.*)') throw new Error(`unsupported header source ${rule.source}: use a single catch-all rule`);
        for (const { key, value } of rule.headers) out[key.toLowerCase()] = value;
    }
    return out;
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
export const contentType = (file) => TYPES[path.extname(file)] || 'application/octet-stream';
