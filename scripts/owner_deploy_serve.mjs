// Phase 25D1C3/25D1C5: LOCAL static HTTP smoke server for the owner deployment artifacts. Builds the artifact, then serves it applying
// the headers/routing MODEL of the target (Vercel legacy package: deploy/owner-bootstrap/vercel.json; Cloudflare Pages package:
// deploy/owner-cloudflare/_headers). It exercises the real package in a real browser; it is NOT Vercel or Cloudflare and proves
// nothing about delivered production headers.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildOwnerDeploy, DEFAULT_OUT } from './owner_deploy_build.mjs';
import { buildOwnerCloudflare, DEFAULT_OUT as CF_OUT } from './owner_cloudflare_build.mjs';
import * as vercel from './owner_deploy_model.mjs';
import * as cloudflare from './owner_cloudflare_model.mjs';

// `node scripts/owner_deploy_serve.mjs [vercel|cloudflare]` (default vercel, port 3001; cloudflare uses port 3002).
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const target = process.argv[2] === 'cloudflare' ? 'cloudflare' : 'vercel';
const port = Number(process.env.OWNER_DEPLOY_PORT || (target === 'cloudflare' ? 3002 : 3001));

let dist, resolve, headers, contentType;
if (target === 'cloudflare') {
    await buildOwnerCloudflare({ outDir: CF_OUT });
    dist = CF_OUT;
    const text = fs.readFileSync(path.join(REPO, 'deploy/owner-cloudflare/_headers'), 'utf8');
    resolve = (p) => cloudflare.resolveRequest(dist, p);
    headers = (p) => cloudflare.headersFor(text, p);
    contentType = cloudflare.contentType;
} else {
    const config = JSON.parse(fs.readFileSync(path.join(REPO, 'deploy/owner-bootstrap/vercel.json'), 'utf8'));
    await buildOwnerDeploy({ outDir: DEFAULT_OUT });
    dist = DEFAULT_OUT;
    resolve = (p) => vercel.resolveRequest(dist, config, p);
    headers = (p) => vercel.headersFor(config, p);
    contentType = vercel.contentType;
}
http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://x').pathname;
    const r = resolve(pathname);
    const h = headers(pathname);
    if (r.status === 308) { res.writeHead(308, { ...h, location: r.location }); return res.end(); }
    if ((r.status === 200 || r.status === 404) && r.file && (req.method === 'GET' || req.method === 'HEAD')) {
        res.writeHead(r.status, { ...h, 'content-type': contentType(r.file) });
        return res.end(req.method === 'HEAD' ? undefined : fs.readFileSync(r.file));
    }
    res.writeHead(404, { ...h, 'content-type': 'text/plain; charset=utf-8' });
    res.end('Not Found');
}).listen(port, '127.0.0.1', () => console.log(`owner ${target} artifact smoke server on http://127.0.0.1:${port}`));
