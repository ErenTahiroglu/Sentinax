// @vitest-environment node
// Phase 25D1E5: deterministic Cloudflare Pages artifact of the PUBLIC Buffett interface (separate Pages project, repo-root build:
//   node scripts/buffett_cloudflare_build.mjs  ->  deploy/buffett-cloudflare/dist, ignored by git).
// Routing/headers are MODELLED from Cloudflare's documented behaviour (same model as the owner package); nothing here proves behaviour on the platform.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import vm from 'vm';

const repo = path.resolve(__dirname, '../../..');
const src = path.join(repo, 'frontend/buffett');
const cfDir = path.join(repo, 'deploy/buffett-cloudflare');
const EXPECTED = ['404.html', '_headers', 'css/styles.css', 'index.html', 'js/api.js', 'js/app.js', 'js/owner_entry.js', 'js/owner_entry_config.js', 'js/ui.js'];
const DEAD_ENDS = [
  '/owner', '/owner/', '/owner/index.html', '/owner/js/owner_app.js', '/owner/vendor/supabase-js.2.117.1.mjs', '/private/portfolio/summary', '/private/', '/frontend/index.html', '/frontend/owner/index.html',
  '/js/core/config.js', '/sw.js', '/manifest.json', '/api/health', '/api/v1/portfolio', '/buffett/portfolio', '/buffett/history', '/_headers', '/_redirects', '/404', '/404.html', '/css/', '/js/', '/js/missing.js',
  '/owner/../index.html', '/%2e%2e/sw.js', '/js/ui.js.map',
];

let build, model, out, dist;
const listAll = (d, base = d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listAll(path.join(d, e.name), base) : [path.relative(base, path.join(d, e.name))]));
const sha = (d) => crypto.createHash('sha256').update(listAll(d).sort().map((f) => `${f}\n${crypto.createHash('sha256').update(fs.readFileSync(path.join(d, f))).digest('hex')}`).join('\n')).digest('hex');
const follow = (pathname) => {
  for (let i = 0; i < 4; i++) {
    const r = model.resolveRequest(dist, pathname);
    if (r.status === 308) { pathname = r.location; continue; }
    return { ...r, finalPath: pathname };
  }
  throw new Error('redirect loop');
};
const copy = (from, prefix) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), prefix)); fs.cpSync(from, d, { recursive: true }); return d; };
const tryBuild = async (opts = {}) => {
  const o = fs.mkdtempSync(path.join(os.tmpdir(), 'bcf-out-'));
  return build.buildBuffettCloudflare({ outDir: path.join(o, 'dist'), ...opts }).then(() => 'ok', (e) => String(e.message));
};
const emittedConfig = (d) => {
  const sandbox = {};
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(d, 'js/owner_entry_config.js'), 'utf8'), sandbox);
  return sandbox.OWNER_ENTRY_CONFIG;
};

beforeAll(async () => {
  build = await import('../../../scripts/buffett_cloudflare_build.mjs');
  model = await import('../../../scripts/owner_cloudflare_model.mjs');
  out = fs.mkdtempSync(path.join(os.tmpdir(), 'bcf-'));
  dist = path.join(out, 'dist');
  await build.buildBuffettCloudflare({ outDir: dist });
});
afterAll(() => out && fs.rmSync(out, { recursive: true, force: true }));

describe('exact allowlisted contents', () => {
  it('is exactly the public Buffett files plus the two platform files, nothing else', () => {
    expect(listAll(dist).sort()).toEqual(EXPECTED);
  });
  it('files are byte-identical to the reviewed sources, except the generated owner-entry configuration', () => {
    for (const f of EXPECTED.filter((x) => !['404.html', '_headers', 'js/owner_entry_config.js'].includes(x))) {
      expect(fs.readFileSync(path.join(dist, f)).equals(fs.readFileSync(path.join(src, f))), f).toBe(true);
    }
    expect(fs.readFileSync(path.join(dist, '404.html')).equals(fs.readFileSync(path.join(cfDir, '404.html')))).toBe(true);
    expect(fs.readFileSync(path.join(dist, '_headers')).equals(fs.readFileSync(path.join(cfDir, '_headers')))).toBe(true);
  });
  it('contains no owner, MFA, SDK, private-application, legacy-auth, backend, database, fixture, credential or worker file', () => {
    for (const f of listAll(dist)) {
      expect(f, f).not.toMatch(/owner_app|mfa_flow|client_config|owner_public_config|private_|supabase|deep_ocean|owner\.css|\.mjs$|sw\.js|manifest|service-worker|_redirects|_routes|functions\/|wrangler|\.py$|\.sql$|\.env|fixture|\.map$/i);
    }
    for (const p of ['owner', 'private', 'vendor', 'frontend', 'backend', 'supabase', 'tests', 'functions']) expect(fs.existsSync(path.join(dist, p)), p).toBe(false);
    for (const f of listAll(dist)) {
      const text = fs.readFileSync(path.join(dist, f), 'utf8');
      expect(text, f).not.toMatch(/sb_secret_|sb_publishable_|service_role|SUPABASE|createClient|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|owner_app|persistSession|signInWithPassword|\/owner\/js\/|private_session/);
    }
  });
  it('the build script names its files explicitly: no recursive copy of frontend/ and no directory walk of the source tree', () => {
    const s = fs.readFileSync(path.join(repo, 'scripts/buffett_cloudflare_build.mjs'), 'utf8');
    expect(s).toContain('export const PUBLIC_ALLOWLIST');
    expect(s).not.toMatch(/\bcpSync\b|\bcp\(|readdirSync|opendirSync|\bglob(Sync)?\(|\bcopyFileSync\(path\.join\(sourceDir, \w+\)\s*,|walk\(/);
    expect(s).toMatch(/for \(const rel of PUBLIC_ALLOWLIST\) put\(rel, fs\.readFileSync\(path\.join\(sourceDir, rel\)\)\);/);
    for (const f of ['index.html', 'css/styles.css', 'js/api.js', 'js/ui.js', 'js/app.js', 'js/owner_entry.js']) expect(s).toContain(`'${f}'`);
  });
  it('the output directory is ignored by git and no Worker, Function or _redirects source exists', () => {
    expect(build.DEFAULT_OUT).toBe(path.join(repo, 'deploy/buffett-cloudflare/dist'));
    expect(fs.readFileSync(path.join(repo, '.gitignore'), 'utf8')).toMatch(/^dist\/$/m);
    for (const p of ['functions', '_redirects', '_routes.json', 'wrangler.toml', 'wrangler.jsonc', 'package.json']) expect(fs.existsSync(path.join(cfDir, p)), p).toBe(false);
  });
  it('is deterministic: two independent builds produce byte-identical output', async () => {
    const o = fs.mkdtempSync(path.join(os.tmpdir(), 'bcf-det-'));
    await build.buildBuffettCloudflare({ outDir: path.join(o, 'dist') });
    expect(sha(path.join(o, 'dist'))).toBe(sha(dist));
    fs.rmSync(o, { recursive: true, force: true });
  });
});

describe('public page content is CSP-ready and self-contained', () => {
  const html = () => fs.readFileSync(path.join(dist, 'index.html'), 'utf8');
  it('has no external URL, web font, inline script, inline style or inline handler', () => {
    expect(html()).not.toMatch(/https?:\/\//i);
    expect(html()).not.toMatch(/fonts\.(googleapis|gstatic)|preconnect/i);
    expect(html()).not.toMatch(/<script(?![^>]*\bsrc=)/i);
    expect(html()).not.toMatch(/\son[a-z]+\s*=|\sstyle\s*=|<style/i);
    const css = fs.readFileSync(path.join(dist, 'css/styles.css'), 'utf8');
    expect(css).not.toMatch(/@import|https?:\/\/|url\(\s*['"]?(?!data:)/i);
  });
  it('loads only its own five scripts, in order, and its stylesheet', () => {
    expect([...html().matchAll(/<script src="([^"]+)"/g)].map((m) => m[1])).toEqual(['js/owner_entry_config.js', 'js/owner_entry.js', 'js/api.js', 'js/ui.js', 'js/app.js']);
    expect([...html().matchAll(/<link[^>]*href="([^"]+)"/g)].map((m) => m[1])).toEqual(['css/styles.css']);
  });
  it('shows the owner entry without registration or any form', () => {
    expect(html()).toMatch(/<a id="owner-entry"[^>]*>Sahip Girişi<\/a>/);
    expect(html()).not.toMatch(/<(form|input|textarea|select|dialog)\b|kayıt ol|üye ol|sign ?up|register/i);
  });
});

describe('routing model (Cloudflare Pages semantics, unverified on the platform)', () => {
  it('the root serves the public Buffett page and /index.html canonicalises to /', () => {
    expect(model.resolveRequest(dist, '/')).toMatchObject({ status: 200, file: path.join(dist, 'index.html') });
    expect(model.resolveRequest(dist, '/index.html')).toMatchObject({ status: 308, location: '/' });
    for (const u of ['/css/styles.css', '/js/api.js', '/js/ui.js', '/js/app.js', '/js/owner_entry.js', '/js/owner_entry_config.js']) expect(follow(u), u).toMatchObject({ status: 200 });
  });
  it.each(DEAD_ENDS)('%s fails closed with the dead-end 404 page and never returns the application HTML (no SPA fallback)', (u) => {
    const r = follow(u);
    expect(r.status, u).toBe(404);
    expect(r.file, u).toBe(path.join(dist, '404.html'));
  });
  it('the 404 page is a self-contained dead end', () => {
    const page = fs.readFileSync(path.join(dist, '404.html'), 'utf8');
    expect(page).not.toMatch(/<script|<link|<iframe|<form|<input|src=|href=|https?:\/\//i);
    expect(page).not.toMatch(/Buffett|Sahip|owner|Sentinax/i);
  });
  it('without 404.html the model flags the documented SPA-fallback hazard, which the build must therefore never allow', () => {
    const d = copy(dist, 'bcf-nofb-');
    fs.rmSync(path.join(d, '404.html'));
    expect(model.resolveRequest(d, '/owner/').spaFallback).toBe(true);
    fs.rmSync(d, { recursive: true, force: true });
  });
});

describe('_headers contract', () => {
  const text = () => fs.readFileSync(path.join(cfDir, '_headers'), 'utf8');
  const final = (u = '/') => model.headersFor(text(), u);
  const csp = () => Object.fromEntries(final()['content-security-policy'].split(';').map((d) => d.trim().split(/\s+/)).filter((d) => d[0]).map(([k, ...v]) => [k, v]));
  it('is one catch-all rule with no indented comments', () => {
    const rules = model.parseHeaders(text());
    expect(rules).toHaveLength(1);
    expect(rules[0].pattern).toBe('/*');
  });
  it('script execution is limited to the own origin: no inline, no eval, no wildcard, no third party, no unrestricted connections', () => {
    const c = csp();
    expect(c['default-src']).toEqual(["'none'"]);
    expect(c['script-src']).toEqual(["'self'"]);
    expect(c['style-src']).toEqual(["'self'"]);
    expect(c['connect-src']).toEqual(["'self'"]);
    expect(c['font-src']).toEqual(["'self'"]);
    expect(c['img-src']).toEqual(["'self'", 'data:']);
    for (const d of ['base-uri', 'form-action', 'object-src', 'frame-src', 'frame-ancestors']) expect(c[d], d).toEqual(["'none'"]);
    expect(final()['content-security-policy']).not.toMatch(/unsafe-inline|unsafe-eval|unsafe-hashes|strict-dynamic|\*|https?:|blob:|wss?:/);
  });
  it('sets anti-framing, nosniff, no-referrer, transport and isolation headers and removes the platform CORS default', () => {
    expect(model.PAGES_DEFAULT_HEADERS['access-control-allow-origin']).toBe('*');
    for (const u of ['/', '/css/styles.css', '/owner/', '/nope']) {
      const h = final(u);
      expect(h['x-content-type-options']).toBe('nosniff');
      expect(h['x-frame-options']).toBe('DENY');
      expect(h['referrer-policy']).toBe('no-referrer');
      expect(h['strict-transport-security']).toMatch(/max-age=\d{8,}/);
      expect(h['cross-origin-opener-policy']).toBe('same-origin');
      expect(h['cross-origin-resource-policy']).toBe('same-origin');
      expect(h['permissions-policy']).toMatch(/camera=\(\)/);
      expect(h['access-control-allow-origin']).toBeUndefined();
    }
  });
  it('contains no connection target other than the own origin (the future /buffett/* route is a separate gate, no Oracle host is committed)', () => {
    const all = text() + fs.readFileSync(path.join(cfDir, '404.html'), 'utf8') + fs.readFileSync(path.join(dist, 'js/api.js'), 'utf8');
    expect(all).not.toMatch(/onrender\.com|oracle|\.pages\.dev|workers\.dev|ai-portfoy|supabase\.co/i);
  });
});

describe('owner-entry destination is a validated BUILD-TIME value; absent means disabled', () => {
  it('by default the emitted configuration is the inspectable null destination with the approved path', () => {
    expect(emittedConfig(dist)).toEqual({ origin: null, path: '/owner/' });
    const file = fs.readFileSync(path.join(dist, 'js/owner_entry_config.js'), 'utf8');
    expect(file).toContain("window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: null, path: '/owner/' });");
    expect(file).toContain('Generated by scripts/buffett_cloudflare_build.mjs');
  });
  it('a reviewed https origin is emitted verbatim and nothing else changes', async () => {
    const o = fs.mkdtempSync(path.join(os.tmpdir(), 'bcf-own-'));
    await build.buildBuffettCloudflare({ outDir: path.join(o, 'dist'), ownerOrigin: 'https://sentinax-owner.example.org', publicOrigin: 'https://sentinax-public.example.org' });
    expect(emittedConfig(path.join(o, 'dist'))).toEqual({ origin: 'https://sentinax-owner.example.org', path: '/owner/' });
    expect(listAll(path.join(o, 'dist')).sort()).toEqual(EXPECTED);
    fs.rmSync(o, { recursive: true, force: true });
  });
  it('the same destination may come from the reviewed source configuration, but an explicit build value wins', async () => {
    const s = copy(src, 'bcf-src-');
    fs.writeFileSync(path.join(s, 'js/owner_entry_config.js'), "window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: 'https://from-source.example.org', path: '/owner/' });\n");
    const o = fs.mkdtempSync(path.join(os.tmpdir(), 'bcf-own2-'));
    await build.buildBuffettCloudflare({ outDir: path.join(o, 'dist'), sourceDir: s });
    expect(emittedConfig(path.join(o, 'dist')).origin).toBe('https://from-source.example.org');
    await build.buildBuffettCloudflare({ outDir: path.join(o, 'dist2'), sourceDir: s, ownerOrigin: 'https://override.example.org' });
    expect(emittedConfig(path.join(o, 'dist2')).origin).toBe('https://override.example.org');
    fs.rmSync(o, { recursive: true, force: true });
    fs.rmSync(s, { recursive: true, force: true });
  });
  it.each([
    'http://owner.example.org', 'javascript:alert(1)', 'data:text/html,x', '//owner.example.org', 'owner.example.org', 'https://owner.example.org:8443', 'https://owner.example.org/', 'https://owner.example.org/owner/',
    'https://owner.example.org?x=1', 'https://owner.example.org#x', 'https://user:pw@owner.example.org', 'https://OWNER.example.org', 'https://localhost', 'https://127.0.0.1', 'https://owner.example.org ', ' ',
    'https://owner.example.org/?access_token=abc', 'https://-bad.example.org',
  ])('rejects the unsafe owner origin %j and fails the build instead of silently disabling', async (origin) => {
    expect(await tryBuild({ ownerOrigin: origin })).toMatch(/owner origin/i);
  });
  it('rejects an owner origin equal to the public origin (they must differ) and a malformed public origin', async () => {
    expect(await tryBuild({ ownerOrigin: 'https://same.example.org', publicOrigin: 'https://same.example.org' })).toMatch(/owner origin|public origin/i);
    expect(await tryBuild({ ownerOrigin: 'https://owner.example.org', publicOrigin: 'http://public.example.org' })).toMatch(/public origin/i);
  });
  it('an invalid pinned source configuration also fails closed', async () => {
    const s = copy(src, 'bcf-bad-');
    fs.writeFileSync(path.join(s, 'js/owner_entry_config.js'), "window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: 'http://evil.example', path: '/owner/' });\n");
    expect(await tryBuild({ sourceDir: s })).toMatch(/owner origin/i);
    fs.rmSync(s, { recursive: true, force: true });
  });
});

describe('the build fails closed', () => {
  it('on a missing source or platform file', async () => {
    for (const f of ['index.html', 'css/styles.css', 'js/api.js', 'js/ui.js', 'js/app.js', 'js/owner_entry.js', 'js/owner_entry_config.js']) {
      const s = copy(src, 'bcf-miss-');
      fs.rmSync(path.join(s, f));
      expect(await tryBuild({ sourceDir: s }), f).toMatch(/missing required/i);
      fs.rmSync(s, { recursive: true, force: true });
    }
    for (const f of ['404.html', '_headers']) {
      const c = copy(cfDir, 'bcf-cf-');
      fs.rmSync(path.join(c, f), { force: true });
      expect(await tryBuild({ cfDir: c }), f).toMatch(new RegExp(`missing required.*${f.replace('.', '\\.')}`, 'i'));
      fs.rmSync(c, { recursive: true, force: true });
    }
  });
  it.each([
    ['an HTML-injection sink in a public script', 'js/ui.js', (t) => `${t}\nlist.innerHTML = x;`],
    ['an eval', 'js/app.js', (t) => `${t}\neval('1');`],
    ['an external script', 'index.html', (t) => t.replace('</head>', '<script src="https://cdn.example/x.js"></script></head>')],
    ['a web-font dependency', 'index.html', (t) => t.replace('</head>', '<link href="https://fonts.googleapis.com/css2?family=X" rel="stylesheet"></head>')],
    ['an inline script', 'index.html', (t) => t.replace('</body>', '<script>1</script></body>')],
    ['an inline handler', 'index.html', (t) => t.replace('<body>', '<body onload="1">')],
    ['authentication code', 'js/api.js', (t) => `${t}\nconst h = { Authorization: 'x' };`],
    ['a Supabase reference', 'js/app.js', (t) => `${t}\n// supabase`],
    ['a form', 'index.html', (t) => t.replace('</main>', '<form></form></main>')],
  ])('on %s', async (_n, file, mutate) => {
    const s = copy(src, 'bcf-mut-');
    fs.writeFileSync(path.join(s, file), mutate(fs.readFileSync(path.join(s, file), 'utf8')));
    expect(await tryBuild({ sourceDir: s })).toMatch(/forbidden|external|inline|injection/i);
    fs.rmSync(s, { recursive: true, force: true });
  });
  it.each([
    ['unsafe-inline script', (t) => t.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'")],
    ['wildcard script origin', (t) => t.replace("script-src 'self'", "script-src 'self' https://*.example.org")],
    ['third-party script', (t) => t.replace("script-src 'self'", "script-src 'self' https://cdn.jsdelivr.net")],
    ['open connect-src', (t) => t.replace("connect-src 'self'", "connect-src 'self' https:")],
    ['framing allowed', (t) => t.replace("frame-ancestors 'none'", "frame-ancestors *")],
    ['second rule', (t) => `${t}\n/owner/*\n  Cache-Control: public, max-age=31536000\n`],
    ['no CORS detach', (t) => t.replace(/^\s*! Access-Control-Allow-Origin\s*$/m, '')],
    ['sniffing allowed', (t) => t.replace('X-Content-Type-Options: nosniff', 'X-Content-Type-Options: sniff')],
    ['indented comment', (t) => t.replace('  X-Frame-Options', '  # x\n  X-Frame-Options')],
  ])('on a weakened _headers (%s)', async (_n, mutate) => {
    const c = copy(cfDir, 'bcf-hdr-');
    fs.writeFileSync(path.join(c, '_headers'), mutate(fs.readFileSync(path.join(c, '_headers'), 'utf8')));
    expect(await tryBuild({ cfDir: c })).toMatch(/_headers/);
    fs.rmSync(c, { recursive: true, force: true });
  });
  it('on a 404.html with a script or link', async () => {
    const c = copy(cfDir, 'bcf-404-');
    fs.writeFileSync(path.join(c, '404.html'), '<!doctype html><script src="/js/app.js"></script>');
    expect(await tryBuild({ cfDir: c })).toMatch(/404\.html/);
    fs.rmSync(c, { recursive: true, force: true });
  });
});

describe('CI and legacy boundaries', () => {
  it('no workflow carries Cloudflare credentials, wrangler or a deploy step', () => {
    for (const f of fs.readdirSync(path.join(repo, '.github/workflows'))) expect(fs.readFileSync(path.join(repo, '.github/workflows', f), 'utf8'), f).not.toMatch(/CLOUDFLARE_(API_TOKEN|ACCOUNT_ID)|wrangler|pages-action|pages deploy/i);
  });
  it('the owner artifacts never contain a public Buffett file and the public artifact never contains an owner one (separate allowlists)', async () => {
    const own = await import('../../../scripts/owner_deploy_build.mjs');
    expect(own.OWNER_ALLOWLIST.some((f) => /buffett|ui\.js|api\.js|app\.js$/.test(f) && !/owner_app/.test(f))).toBe(false);
    expect(build.PUBLIC_ALLOWLIST.some((f) => /owner_app|mfa|private|deep_ocean|vendor|client_config|owner_public_config/.test(f))).toBe(false);
  });
});
