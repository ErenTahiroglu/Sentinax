// Phase 25D1C3: LOCAL static HTTP smoke server for the owner deployment artifact. Builds the artifact, then serves it applying
// deploy/owner-bootstrap/vercel.json headers through scripts/owner_deploy_model.mjs. This exercises the real package in a real
// browser; it is NOT Vercel and proves nothing about delivered production headers.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildOwnerDeploy, DEFAULT_OUT } from './owner_deploy_build.mjs';
import { contentType, headersFor, resolveRequest } from './owner_deploy_model.mjs';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const config = JSON.parse(fs.readFileSync(path.join(REPO, 'deploy/owner-bootstrap/vercel.json'), 'utf8'));
const port = Number(process.env.OWNER_DEPLOY_PORT || 3001);

await buildOwnerDeploy({ outDir: DEFAULT_OUT });
const dist = DEFAULT_OUT;
http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://x').pathname;
    const r = resolveRequest(dist, config, pathname);
    const headers = headersFor(config, pathname);
    if (r.status === 308) { res.writeHead(308, { ...headers, location: r.location }); return res.end(); }
    if (r.status === 200 && (req.method === 'GET' || req.method === 'HEAD')) {
        res.writeHead(200, { ...headers, 'content-type': contentType(r.file) });
        return res.end(req.method === 'HEAD' ? undefined : fs.readFileSync(r.file));
    }
    res.writeHead(404, { ...headers, 'content-type': 'text/plain; charset=utf-8' });
    res.end('Not Found');
}).listen(port, '127.0.0.1', () => console.log(`owner artifact smoke server on http://127.0.0.1:${port}`));
