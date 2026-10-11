// @vitest-environment node
// Phase 25D1C2: the owner bootstrap page must be self-contained and route-safe under BOTH Vercel root configurations.
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const repo = path.resolve(__dirname, '../../..');
const frontend = path.join(repo, 'frontend');
const read = (p) => fs.readFileSync(path.join(frontend, p), 'utf8');
const html = read('owner/index.html');

// Minimal, explicit model of Vercel routing for the two candidate project roots:
//   root = repository root  -> vercel.json          (cleanUrls, trailingSlash:false, rewrite /(.*) -> /frontend/$1)
//   root = frontend/        -> frontend/vercel.json (static build, rewrite /(.*) -> /$1)
// A path "exists" if it is a file, a directory with index.html (directory index) or, with cleanUrls, <path>.html.
const CONFIGS = [
  { name: 'root vercel.json', file: path.join(repo, 'vercel.json'), base: repo },
  { name: 'frontend/vercel.json', file: path.join(frontend, 'vercel.json'), base: frontend },
];

function route(config, pathname) {
  const cfg = JSON.parse(fs.readFileSync(config.file, 'utf8'));
  let target = pathname;
  for (const r of cfg.rewrites || []) {
    const m = new RegExp(`^${r.source}$`).exec(pathname);
    if (!m) continue;
    if (/^https?:/.test(r.destination)) continue; // external proxy rewrites never serve static files
    target = r.destination.replace(/\$(\d)/g, (_, i) => m[Number(i)] ?? '');
    break;
  }
  const fsPath = path.join(config.base, target);
  if (fs.existsSync(fsPath) && fs.statSync(fsPath).isFile()) return fsPath;
  if (fs.existsSync(fsPath) && fs.statSync(fsPath).isDirectory() && fs.existsSync(path.join(fsPath, 'index.html'))) return path.join(fsPath, 'index.html');
  if (cfg.cleanUrls && fs.existsSync(`${fsPath}.html`)) return `${fsPath}.html`;
  return null;
}

const assetRefs = () => [...html.matchAll(/\b(?:src|href)="([^"]+)"/g)].map((m) => m[1]).filter((r) => !r.startsWith('#'));

describe('owner page routing (both Vercel project roots)', () => {
  for (const config of CONFIGS) {
    for (const pagePath of ['/owner', '/owner/', '/owner/index.html']) {
      it(`${config.name}: ${pagePath} serves the owner page`, () => {
        expect(route(config, pagePath)).toBe(path.join(frontend, 'owner/index.html'));
      });
      it(`${config.name}: every asset referenced from ${pagePath} resolves to an existing owner file`, () => {
        for (const ref of assetRefs()) {
          const resolved = new URL(ref, `https://app.example${pagePath}`);
          expect(resolved.origin, ref).toBe('https://app.example');
          const file = route(config, resolved.pathname);
          expect(file, `${ref} -> ${resolved.pathname}`).not.toBeNull();
          expect(file.startsWith(path.join(frontend, 'owner') + path.sep), `${ref} must be an owner file`).toBe(true);
        }
      });
    }
  }

  it('asset URLs are explicit root-absolute owner paths, independent of the page URL form', () => {
    for (const ref of assetRefs()) expect(ref).toMatch(/^\/owner\/(css|js|vendor)\/[\w.-]+\.(css|js|mjs)$/);
  });
});

describe('owner page is independent of the legacy SPA and of third-party code', () => {
  const ownerSources = () => ['owner/js/owner_app.js', 'owner/js/client_config.js', 'owner/js/mfa_flow.js'].map(read).join('\n');

  it('does not load the legacy configuration or read its globals', () => {
    expect(html).not.toMatch(/core\/config\.js/);
    expect(ownerSources()).not.toMatch(/window\.SUPABASE_|core\/config|API_BASE/);
  });

  it('loads no executable JavaScript from any other origin', () => {
    for (const m of html.matchAll(/<script\b[^>]*>/gi)) expect(m[0]).toMatch(/src="\/owner\//);
    expect(html).not.toMatch(/https?:\/\/cdn|jsdelivr|unpkg|cdnjs|esm\.sh/i);
    expect(ownerSources()).not.toMatch(/jsdelivr|unpkg|cdnjs|esm\.sh|import\(\s*['"]https?:/i);
    const csp = html.match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
    expect(csp).toMatch(/script-src 'self'(;|$)/);
    expect(csp).not.toMatch(/jsdelivr|unpkg|cdnjs|https:\/\/(?!.*supabase)/);
  });

  it('CSP connect-src allows exactly the own origin (same-origin private API path) and the configured Supabase origin', async () => {
    const { OWNER_PUBLIC_CONFIG } = await import('../../owner/js/owner_public_config.js');
    const csp = html.match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
    expect(csp).toContain(`connect-src 'self' ${OWNER_PUBLIC_CONFIG.supabaseUrl};`);
  });
});

describe('owner public configuration fails closed', () => {
  it('holds only the project URL and a publishable key, consistent with the legacy public values', async () => {
    const { OWNER_PUBLIC_CONFIG } = await import('../../owner/js/owner_public_config.js');
    expect(Object.keys(OWNER_PUBLIC_CONFIG).sort()).toEqual(['publishableKey', 'supabaseUrl']);
    expect(Object.isFrozen(OWNER_PUBLIC_CONFIG)).toBe(true);
    expect(OWNER_PUBLIC_CONFIG.publishableKey).toMatch(/^sb_publishable_/);
    const legacy = read('js/core/config.js');
    expect(legacy).toContain(OWNER_PUBLIC_CONFIG.supabaseUrl);
    expect(legacy).toContain(OWNER_PUBLIC_CONFIG.publishableKey);
  });

  it('resolveOwnerClientConfig accepts the shipped config and rejects anything else', async () => {
    const { resolveOwnerClientConfig } = await import('../../owner/js/client_config.js');
    const { OWNER_PUBLIC_CONFIG: ok } = await import('../../owner/js/owner_public_config.js');
    expect(resolveOwnerClientConfig(ok)).toEqual({ url: ok.supabaseUrl, key: ok.publishableKey });
    const bad = [
      undefined, null, {}, 'x', [],
      { supabaseUrl: ok.supabaseUrl },
      { publishableKey: ok.publishableKey },
      { ...ok, supabaseUrl: '' },
      { ...ok, publishableKey: '' },
      { ...ok, supabaseUrl: 'http://abcdefghijklmnopqrst.supabase.co' },
      { ...ok, supabaseUrl: 'https://evil.example' },
      { ...ok, supabaseUrl: 'https://abcdefghijklmnopqrst.supabase.co.evil.example' },
      { ...ok, supabaseUrl: 'https://u:p@abcdefghijklmnopqrst.supabase.co' },
      { ...ok, supabaseUrl: 'https://abcdefghijklmnopqrst.supabase.co/rest/v1' },
      { ...ok, supabaseUrl: 'https://abcdefghijklmnopqrst.supabase.co/' },
      { ...ok, publishableKey: 'sb_secret_' + 'a'.repeat(30) },
      { ...ok, publishableKey: ['eyJhbGciOiJIUzI1NiJ9', 'eyJyb2xlIjoic2VydmljZV9yb2xlIn0', 'a'.repeat(20)].join('.') }, // JWT-shaped (legacy service_role style), assembled at runtime
      { ...ok, publishableKey: 'sb_publishable_short' },
      { ...ok, extra: 'service' },
    ];
    for (const c of bad) expect(resolveOwnerClientConfig(c), JSON.stringify(c)).toBeNull();
  });

  it('owner_app creates no client when the configuration does not resolve', () => {
    const app = read('owner/js/owner_app.js');
    expect(app).toContain('resolveOwnerClientConfig(OWNER_PUBLIC_CONFIG)');
    const cfgAt = app.indexOf('resolveOwnerClientConfig(OWNER_PUBLIC_CONFIG)');
    expect(cfgAt).toBeGreaterThan(-1);
    expect(app.indexOf('createClient(')).toBeGreaterThan(cfgAt);
    expect(app).toMatch(/if \(!cfg\)[^\n]*LOCKED/);
  });
});
