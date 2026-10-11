// TEST-ONLY helper (Phase 25D1E4): forwards a same-origin browser request to a loopback test API and relays the answer unchanged.
// It refuses any non-loopback target and any method other than GET/HEAD, and forwards only the headers the browser would send to the same-origin API.
import http from 'node:http';

const FORWARD = ['authorization', 'accept', 'cookie', 'referer', 'origin', 'cache-control', 'idempotency-key'];

export function proxyToTestApi(req, res, apiOrigin, rewritePrefix = null) {
    const target = new URL(apiOrigin);
    if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(target.hostname)) {
        res.writeHead(502, { 'content-type': 'text/plain' });
        return res.end('test API must be loopback');
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { allow: 'GET, HEAD', 'content-type': 'text/plain' });
        return res.end('method not allowed');
    }
    const headers = {};
    for (const k of FORWARD) if (req.headers[k] !== undefined) headers[k] = req.headers[k];
    const upstream = http.request({ host: target.hostname, port: target.port, path: req.url, method: req.method, headers }, (up) => {
        res.writeHead(up.statusCode, up.headers);
        up.pipe(res);
    });
    upstream.on('error', () => {
        if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain' });
        res.end('test API unreachable');
    });
    upstream.end();
}
