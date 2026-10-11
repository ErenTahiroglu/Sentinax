// TEST-ONLY (Phase 25D1E4): serves the public Buffett frontend (frontend/buffett) as its OWN origin and forwards its same-origin `/buffett/*` API calls to the loopback
// test API (the real FastAPI application). Models the future public Cloudflare Pages project; it contains no owner file and no private route.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { proxyToTestApi } from './test_api_proxy.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.join(REPO, 'frontend/buffett');
const port = Number(process.env.BUFFETT_HARNESS_PORT || 3006);
const api = process.env.SENTINAX_TEST_API_ORIGIN;
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

http.createServer((req, res) => {
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
