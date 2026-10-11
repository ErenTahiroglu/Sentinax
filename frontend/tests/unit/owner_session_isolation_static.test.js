import { describe, it, expect, vi } from 'vitest';
import fs from 'fs';
import path from 'path';

const root = path.resolve(__dirname, '../..');
const repo = path.resolve(root, '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

describe('owner bootstrap session isolation (static)', () => {
  it('uses an in-memory-only session: no persistence, no URL session detection, no custom storage', async () => {
    const cfg = await import('../../owner/js/client_config.js');
    expect(cfg.OWNER_AUTH_OPTIONS).toEqual({ persistSession: false, autoRefreshToken: true, detectSessionInUrl: false });
    expect(Object.isFrozen(cfg.OWNER_AUTH_OPTIONS)).toBe(true);
    expect(cfg.SUPABASE_SDK_PATH).toMatch(/^\.\.\/vendor\/supabase-js\.\d+\.\d+\.\d+\.mjs$/);
  });

  it('owner_app takes the client options from the frozen config and never re-enables persistence', () => {
    const app = read('owner/js/owner_app.js');
    expect(app).toContain("from './client_config.js'");
    expect(app).toMatch(/createClient\(cfg\.url, cfg\.key, \{ auth: \{ \.\.\.OWNER_AUTH_OPTIONS \} \}\)/);
    expect(app).not.toMatch(/persistSession\s*:\s*true/);
    expect(app).not.toMatch(/storage(Key)?\s*:/);
  });

  it('onAuthStateChange callback is synchronous and only notifies the flow', () => {
    const app = read('owner/js/owner_app.js');
    expect(app).toMatch(/onAuthStateChange\(\(event\) => flow\.notifyAuthEvent\(event\)\)/);
    expect(app).not.toMatch(/onAuthStateChange\(\s*async/);
  });

  it('the page never claims client-side state establishes ownership', () => {
    const html = read('owner/index.html');
    expect(html).toContain('sunucu tarafı sahip yetkilendirmesi değildir');
    for (const claim of ['Sahip doğrulandı', 'Yetkili sahip', 'owner verified']) expect(html).not.toContain(claim);
    expect(read('owner/js/owner_app.js')).not.toMatch(/Sahip doğrulandı|owner verified/);
  });

  it('service worker never handles the owner page, cross-origin, authenticated, auth or non-GET requests', () => {
    const src = fs.readFileSync(path.join(root, 'sw.js'), 'utf8');
    const handlers = {};
    const cache = { put: vi.fn(), addAll: vi.fn() };
    const caches = { open: vi.fn(async () => cache), match: vi.fn(async () => undefined), keys: vi.fn(async () => []), delete: vi.fn() };
    const fetchMock = vi.fn(async () => ({ ok: true, headers: new Headers(), clone() { return this; } }));
    const self_ = { addEventListener: (t, fn) => { handlers[t] = fn; }, skipWaiting() {}, clients: { claim() {} }, location: { origin: 'https://app.example' } };
    new Function('self', 'caches', 'fetch', 'console', src)(self_, caches, fetchMock, { log() {}, warn() {} });
    const fire = (url, { method = 'GET', headers = {} } = {}) => {
      const respondWith = vi.fn();
      handlers.fetch({ request: { url, method, headers: new Headers(headers) }, respondWith });
      return respondWith;
    };
    for (const u of ['https://app.example/owner', 'https://app.example/owner/', 'https://app.example/owner/js/mfa_flow.js', 'https://app.example/frontend/owner/index.html',
      'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.1/+esm', 'https://abcdefghijklmnopqrst.supabase.co/auth/v1/user', 'https://app.example/auth/v1/token']) {
      expect(fire(u), u).not.toHaveBeenCalled();
    }
    expect(fire('https://app.example/js/app.js', { method: 'POST' })).not.toHaveBeenCalled();
    expect(fire('https://app.example/js/app.js', { headers: { Authorization: 'Bearer x' } })).not.toHaveBeenCalled();
    expect(fire('https://app.example/js/app.js')).toHaveBeenCalled(); // legacy static caching unchanged
    expect(src).toMatch(/CACHE_NAME = 'ai-portfoy-v([3-9]|\d{2,})'/);
  });

  it('service worker does not store no-store responses', async () => {
    const src = fs.readFileSync(path.join(root, 'sw.js'), 'utf8');
    const handlers = {};
    const cache = { put: vi.fn(), addAll: vi.fn() };
    const caches = { open: vi.fn(async () => cache), match: vi.fn(async () => undefined), keys: vi.fn(async () => []), delete: vi.fn() };
    const res = { ok: true, headers: new Headers({ 'Cache-Control': 'no-store' }), clone() { return this; } };
    const self_ = { addEventListener: (t, fn) => { handlers[t] = fn; }, skipWaiting() {}, clients: { claim() {} }, location: { origin: 'https://app.example' } };
    new Function('self', 'caches', 'fetch', 'console', src)(self_, caches, async () => res, { log() {}, warn() {} });
    let p;
    handlers.fetch({ request: { url: 'https://app.example/js/app.js', method: 'GET', headers: new Headers() }, respondWith: (x) => { p = x; } });
    await p;
    await new Promise((r) => setTimeout(r, 0));
    expect(cache.put).not.toHaveBeenCalled();
  });

  it.each(['vercel.json', 'frontend/vercel.json'])('%s: owner assets are no-store, opener-isolated, and not matched by immutable cache rules', (file) => {
    const cfg = JSON.parse(fs.readFileSync(path.join(repo, file), 'utf8'));
    const matches = (source, p) => new RegExp(`^${source}$`).test(p);
    const ownerPaths = ['/owner', '/owner/', '/owner/js/mfa_flow.js', '/owner/css/owner.css', '/frontend/owner/', '/frontend/owner/js/mfa_flow.js', '/frontend/owner/css/owner.css'];
    for (const p of ownerPaths) {
      const rules = cfg.headers.filter((h) => matches(h.source, p));
      const get = (k) => rules.flatMap((r) => r.headers).filter((h) => h.key === k).map((h) => h.value);
      expect(get('Cache-Control'), p).toEqual(['no-store']);
      expect(get('Cross-Origin-Opener-Policy'), p).toEqual(['same-origin']);
      expect(get('Cross-Origin-Resource-Policy'), p).toEqual(['same-origin']);
      expect(get('X-Robots-Tag'), p).toEqual(['noindex, nofollow']);
    }
    // legacy assets keep their caching
    const legacy = cfg.headers.filter((h) => matches(h.source, '/frontend/js/app.js'));
    expect(JSON.stringify(legacy)).toContain('immutable');
  });
});
