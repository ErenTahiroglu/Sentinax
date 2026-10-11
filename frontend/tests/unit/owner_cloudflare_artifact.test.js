// @vitest-environment node
// Phase 25D1C5: Cloudflare Pages package for the isolated owner bootstrap page (separate Pages project, repo-root build).
// Build: `node scripts/owner_cloudflare_build.mjs` -> deploy/owner-cloudflare/dist (ignored). Routing/headers are MODELLED from
// Cloudflare's documented Pages behaviour (route matching, 404.html vs SPA fallback, _headers defaults/override/detach).
// Nothing here proves behaviour on real Cloudflare; every platform-specific assumption is listed in ASSUMPTIONS and in the runbook.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

const repo = path.resolve(__dirname, '../../..');
const ownerSrc = path.join(repo, 'frontend/owner');
const cfDir = path.join(repo, 'deploy/owner-cloudflare');
const OWNER_FILES = [
  'owner/index.html',
  'owner/css/owner.css',
  'owner/css/deep_ocean.css',
  'owner/js/owner_app.js',
  'owner/js/client_config.js',
  'owner/js/owner_public_config.js',
  'owner/js/mfa_flow.js',
  'owner/js/private_api_config.js',
  'owner/js/private_session.js',
  'owner/js/private_shell.js',
  'owner/vendor/supabase-js.2.117.1.mjs',
];
const PLATFORM_FILES = ['404.html', '_headers'];
const REQUIRED_URLS = [
  '/owner', '/owner/', '/owner/index.html', '/owner/css/owner.css', '/owner/js/owner_app.js', '/owner/js/client_config.js',
  '/owner/js/owner_public_config.js', '/owner/js/mfa_flow.js', '/owner/vendor/supabase-js.2.117.1.mjs',
  '/owner/css/deep_ocean.css', '/owner/js/private_api_config.js', '/owner/js/private_session.js', '/owner/js/private_shell.js',
];

let build, model, out, dist;
const listAll = (d, base = d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listAll(path.join(d, e.name), base) : [path.relative(base, path.join(d, e.name))]));
const follow = (pathname) => {
  for (let i = 0; i < 4; i++) {
    const r = model.resolveRequest(dist, pathname);
    if (r.status === 308) { pathname = r.location; continue; }
    return { ...r, finalPath: pathname };
  }
  throw new Error('redirect loop');
};
const headersText = () => fs.readFileSync(path.join(cfDir, '_headers'), 'utf8');

beforeAll(async () => {
  build = await import('../../../scripts/owner_cloudflare_build.mjs');
  model = await import('../../../scripts/owner_cloudflare_model.mjs');
  out = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-cf-'));
  dist = path.join(out, 'dist');
  await build.buildOwnerCloudflare({ outDir: dist });
});
afterAll(() => out && fs.rmSync(out, { recursive: true, force: true }));

describe('Cloudflare artifact contents', () => {
  it('is exactly the 11 owner files plus the two platform files (404.html, _headers)', () => {
    expect(listAll(dist).sort()).toEqual([...OWNER_FILES, ...PLATFORM_FILES].sort());
  });
  it('owner files are byte-identical to the authoritative sources (SDK not duplicated or altered)', () => {
    for (const f of OWNER_FILES) {
      expect(fs.readFileSync(path.join(dist, f)).equals(fs.readFileSync(path.join(ownerSrc, f.replace(/^owner\//, '')))), f).toBe(true);
    }
  });
  it('has no _redirects file and no legacy SPA, Buffett, worker, backend or finance file', () => {
    expect(fs.existsSync(path.join(dist, '_redirects'))).toBe(false);
    for (const f of listAll(dist)) {
      expect(f, f).not.toMatch(/(^|\/)(sw|manifest|service-worker|_redirects|_routes)[^/]*\.(js|json)?$/i);
      expect(f, f).not.toMatch(/buffett|backend|ledger|portfolio|\.py$|\.sql$|\.env|supabase-js\.manifest|^functions\//i);
    }
    for (const p of ['index.html', 'sw.js', 'js', 'buffett', 'backend', 'frontend', 'functions']) expect(fs.existsSync(path.join(dist, p)), p).toBe(false);
  });
  it('404.html is a self-contained dead end: no script, no link, no external reference, no owner UI', () => {
    const html = fs.readFileSync(path.join(dist, '404.html'), 'utf8');
    expect(html).not.toMatch(/<script|<link|<iframe|<form|<input|src=|href=|https?:\/\//i);
    expect(html).not.toMatch(/Sentinax Sahip|signin|password|owner/i);
  });
  it('no privileged credential, user token, owner id or service worker in any shipped file', () => {
    for (const f of listAll(dist)) {
      const src = fs.readFileSync(path.join(dist, f), 'utf8');
      expect(src, f).not.toMatch(/sb_secret_[A-Za-z0-9_-]{16,}|service_role|SUPABASE_JWT_SECRET|SERVICE_ROLE|-----BEGIN [A-Z ]*PRIVATE KEY/i);
      expect(src, f).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}/);
      expect(src, f).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[1-9a-f][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i);
      expect(src, f).not.toMatch(/serviceWorker\s*\.\s*register|importScripts\s*\(/);
      expect(src, f).not.toMatch(/<script[^>]+src="https?:|from\s+['"]https?:|import\(\s*['"]https?:/i);
    }
  });
});

describe('routing model (Cloudflare Pages semantics, unverified on the platform)', () => {
  for (const url of REQUIRED_URLS) {
    it(`${url} ends at an owner file with 200`, () => {
      const r = follow(url);
      expect(r.status).toBe(200);
      expect(path.relative(dist, r.file).startsWith('owner' + path.sep)).toBe(true);
    });
  }
  it('/owner and /owner/index.html canonicalise to /owner/ (folder index is served with a trailing slash)', () => {
    for (const u of ['/owner', '/owner/index.html']) {
      const r = model.resolveRequest(dist, u);
      expect(r.status, u).toBe(308);
      expect(r.location, u).toBe('/owner/');
    }
    expect(model.resolveRequest(dist, '/owner/')).toMatchObject({ status: 200, file: path.join(dist, 'owner/index.html') });
  });
  it('root, legacy, API, worker, metadata and traversal probes return 404 with the dead-end page, never owner HTML (no SPA fallback)', () => {
    for (const u of ['/', '/index.html', '/sw.js', '/manifest.json', '/js/core/config.js', '/frontend/index.html', '/frontend/owner/index.html', '/buffett/', '/api/v1/portfolio', '/api/buffett',
      '/owner/js/missing.js', '/owner/js/', '/owner/vendor/supabase-js.manifest.json', '/owner/js/owner_app.js.map', '/owner/../index.html', '/owner/%2e%2e/sw.js', '/_headers', '/_redirects', '/404.html', '/404', '/owner/404.html']) {
      const r = follow(u);
      expect(r.status, u).toBe(404);
      expect(r.file, u).toBe(path.join(dist, '404.html'));
    }
  });
  it('a missing top-level 404.html would make Pages treat the project as an SPA (documented), so it must always ship', () => {
    expect(model.ASSUMPTIONS.join('\n')).toMatch(/SPA/);
    const noFallback = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-cf-nofb-'));
    fs.cpSync(dist, noFallback, { recursive: true });
    fs.rmSync(path.join(noFallback, '404.html'));
    expect(model.resolveRequest(noFallback, '/anything').spaFallback).toBe(true); // model marks the hazard the build must prevent
    fs.rmSync(noFallback, { recursive: true, force: true });
  });
});

describe('_headers contract', () => {
  const finalHeaders = (u = '/owner/') => model.headersFor(headersText(), u);
  const csp = (h) => Object.fromEntries(h['content-security-policy'].split(';').map((d) => d.trim().split(/\s+/)).filter((d) => d[0]).map(([k, ...v]) => [k, v]));
  it('has no indented comment lines (the platform could parse them as headers) and a rule-level detach is the last line', () => {
    const lines = headersText().split('\n').filter(Boolean);
    for (const l of lines) if (/^\s/.test(l)) expect(l.trim().startsWith('#'), l).toBe(false);
    expect(lines[lines.length - 1].trim()).toBe('! Access-Control-Allow-Origin');
  });
  it('uses a single catch-all rule and no placeholder/splat tricks', () => {
    const rules = model.parseHeaders(headersText());
    expect(rules).toHaveLength(1);
    expect(rules[0].pattern).toBe('/*');
  });
  it('every path (owner, assets, unknown) gets the same strict header set after Cloudflare defaults are applied', () => {
    for (const u of ['/', '/owner/', '/owner/js/owner_app.js', '/nope']) {
      const h = finalHeaders(u);
      expect(h['cache-control'], u).toBe('no-store');
      expect(h['x-content-type-options']).toBe('nosniff');
      expect(h['x-frame-options']).toBe('DENY');
      expect(h['referrer-policy']).toBe('no-referrer');
      expect(h['cross-origin-opener-policy']).toBe('same-origin');
      expect(h['cross-origin-resource-policy']).toBe('same-origin');
      expect(h['strict-transport-security']).toMatch(/max-age=\d{8,}/);
      expect(h['x-robots-tag']).toMatch(/noindex/);
      expect(h['permissions-policy']).toMatch(/camera=\(\)/);
    }
  });
  it('Cloudflare\'s always-added "Access-Control-Allow-Origin: *" is detached', () => {
    expect(model.PAGES_DEFAULT_HEADERS['access-control-allow-origin']).toBe('*'); // documented default
    expect(finalHeaders()['access-control-allow-origin']).toBeUndefined();
  });
  it('CSP is strict, pinned to the project origin, and equals the CSP of the legacy-Vercel owner package and the meta CSP', async () => {
    const h = finalHeaders();
    const d = csp(h);
    const pub = (await import('../../owner/js/owner_public_config.js')).OWNER_PUBLIC_CONFIG;
    expect(h['content-security-policy']).not.toMatch(/unsafe-inline|unsafe-eval|\*|https?:\/\/(?!zlggrmsolklhfgijcjnz)/);
    expect(d['script-src']).toEqual(["'self'"]);
    expect(d['connect-src']).toEqual(["'self'", pub.supabaseUrl]);
    expect(d['frame-ancestors']).toEqual(["'none'"]);
    for (const k of ['default-src', 'base-uri', 'form-action', 'object-src']) expect(d[k]).toEqual(["'none'"]);
    const vercel = JSON.parse(fs.readFileSync(path.join(repo, 'deploy/owner-bootstrap/vercel.json'), 'utf8')).headers[0].headers;
    const vh = Object.fromEntries(vercel.map((x) => [x.key.toLowerCase(), x.value]));
    for (const k of Object.keys(vh)) expect(h[k], `drift vs vercel.json: ${k}`).toBe(vh[k]);
    const meta = fs.readFileSync(path.join(ownerSrc, 'index.html'), 'utf8').match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
    for (const dir of ['default-src', 'script-src', 'style-src', 'connect-src', 'base-uri', 'form-action', 'object-src']) expect(meta, dir).toContain(`${dir} ${d[dir].join(' ')}`);
  });
});

describe('build fails closed', () => {
  const copy = (src, name) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), name)); fs.cpSync(src, d, { recursive: true }); return d; };
  const tryBuild = async (opts = {}) => {
    const o = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-cf-out-'));
    return build.buildOwnerCloudflare({ outDir: path.join(o, 'dist'), ...opts }).then(() => 'ok', (e) => String(e.message));
  };
  it('missing 404.html source fails (would enable the SPA fallback)', async () => {
    const cf = copy(cfDir, 'cf-src-'); fs.rmSync(path.join(cf, '404.html'));
    expect(await tryBuild({ cfDir: cf })).toMatch(/missing required .*404\.html/i);
  });
  it('missing _headers source fails', async () => {
    const cf = copy(cfDir, 'cf-src-'); fs.rmSync(path.join(cf, '_headers'));
    expect(await tryBuild({ cfDir: cf })).toMatch(/missing required .*_headers/i);
  });
  it('a missing owner file or a tampered SDK still fails (shared owner build checks)', async () => {
    const s1 = copy(ownerSrc, 'owner-src-'); fs.rmSync(path.join(s1, 'js/mfa_flow.js'));
    expect(await tryBuild({ sourceDir: s1 })).toMatch(/missing required owner file/i);
    const s2 = copy(ownerSrc, 'owner-src-'); fs.appendFileSync(path.join(s2, 'vendor/supabase-js.2.117.1.mjs'), '\n// x');
    expect(await tryBuild({ sourceDir: s2 })).toMatch(/integrity/i);
  });
  it('a weakened _headers (unsafe-inline CSP, extra rule, missing CORS detach, caching) fails', async () => {
    const variants = {
      'unsafe-inline': (t) => t.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'"),
      'extra rule': (t) => `${t}\n/owner/*\n  Cache-Control: public, max-age=31536000\n`,
      'no CORS detach': (t) => t.replace(/^\s*! Access-Control-Allow-Origin\s*$/m, ''),
      'caching': (t) => t.replace('Cache-Control: no-store', 'Cache-Control: public, max-age=3600'),
    };
    for (const [name, mutate] of Object.entries(variants)) {
      const cf = copy(cfDir, 'cf-src-');
      fs.writeFileSync(path.join(cf, '_headers'), mutate(fs.readFileSync(path.join(cf, '_headers'), 'utf8')));
      expect(await tryBuild({ cfDir: cf }), name).toMatch(/_headers/);
    }
  });
  it('a 404.html with a script or link fails', async () => {
    const cf = copy(cfDir, 'cf-src-');
    fs.writeFileSync(path.join(cf, '404.html'), '<!doctype html><script src="/owner/js/owner_app.js"></script>');
    expect(await tryBuild({ cfDir: cf })).toMatch(/404\.html/);
  });
});

describe('no Cloudflare credentials or deploy step in CI; legacy configs stay out of the Cloudflare path', () => {
  it('workflows contain no Cloudflare token, wrangler or pages deploy', () => {
    for (const f of fs.readdirSync(path.join(repo, '.github/workflows'))) {
      expect(fs.readFileSync(path.join(repo, '.github/workflows', f), 'utf8'), f).not.toMatch(/CLOUDFLARE_(API_TOKEN|ACCOUNT_ID)|wrangler|pages-action|pages deploy/i);
    }
  });
  it('the Pages build output is under an ignored directory and the build is repo-root runnable', () => {
    expect(build.DEFAULT_OUT).toBe(path.join(repo, 'deploy/owner-cloudflare/dist'));
    expect(fs.readFileSync(path.join(repo, '.gitignore'), 'utf8')).toMatch(/^dist\/$/m);
    expect(fs.existsSync(path.join(cfDir, 'vercel.json'))).toBe(false);
    expect(fs.existsSync(path.join(cfDir, 'package.json'))).toBe(false);
  });
});
