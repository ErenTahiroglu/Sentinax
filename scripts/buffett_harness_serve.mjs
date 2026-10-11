// TEST-ONLY (Phase 25D1E4/25D1E5): serves the public Buffett frontend as its OWN origin.
//   node scripts/buffett_harness_serve.mjs            serves the SOURCE tree frontend/buffett (connected suite; forwards `/buffett/*` to SENTINAX_TEST_API_ORIGIN when set)
//   node scripts/buffett_harness_serve.mjs artifact   BUILDS deploy/buffett-cloudflare/dist and serves it with the documented Cloudflare Pages routing and the _headers model
//                                                     (strict CSP, 404.html dead end, no SPA fallback). A local model, NOT Cloudflare: nothing here proves platform behaviour.
// Neither mode contains an owner file or a private route.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { proxyToTestApi } from './test_api_proxy.mjs';
import { buildBuffettCloudflare, DEFAULT_OUT, CF_DIR } from './buffett_cloudflare_build.mjs';
import { contentType, headersFor, resolveRequest } from './owner_cloudflare_model.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.join(REPO, 'frontend/buffett');
const artifactMode = process.argv[2] === 'artifact';
const port = Number(process.env.BUFFETT_HARNESS_PORT || (artifactMode ? 3007 : 3006));
const api = process.env.SENTINAX_TEST_API_ORIGIN;
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

if (artifactMode) {
    await buildBuffettCloudflare({ outDir: DEFAULT_OUT, ...(process.env.BUFFETT_OWNER_ORIGIN ? { ownerOrigin: process.env.BUFFETT_OWNER_ORIGIN } : {}) });
    const headersText = fs.readFileSync(path.join(CF_DIR, '_headers'), 'utf8');
    http.createServer((req, res) => {
        const pathname = new URL(req.url, 'http://x').pathname;
        const r = resolveRequest(DEFAULT_OUT, pathname);
        const h = headersFor(headersText, pathname);
        if (r.status === 308) { res.writeHead(308, { ...h, location: r.location }); return res.end(); }
        if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405, { ...h, allow: 'GET, HEAD' }); return res.end(); }
        res.writeHead(r.status, { ...h, 'content-type': contentType(r.file) });
        res.end(req.method === 'HEAD' ? undefined : fs.readFileSync(r.file));
    }).listen(port, '127.0.0.1', () => console.log(`buffett artifact (local Cloudflare model) on http://127.0.0.1:${port}`));
} else http.createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    if (api && pathname.startsWith('/buffett/')) return proxyToTestApi(req, res, api);
    const rel = pathname === '/' ? 'index.html' : pathname.replace(/^\//, '');
    const file = path.join(ROOT, rel);
    if (!file.startsWith(ROOT + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        res.writeHead(404, { 'content-type': 'text/plain' });
        return res.end('Not Found');
    }
    res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream', 'cache-control': 'no-store' });
    res.end(fs.readFileSync(file));
}).listen(port, '127.0.0.1', () => console.log(`buffett harness on http://127.0.0.1:${port}`));
