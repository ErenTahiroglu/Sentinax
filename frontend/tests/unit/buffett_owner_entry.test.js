// @vitest-environment node
// Phase 25D1E4: the public Buffett page's "Sahip Girişi" entry. A plain, validated navigation link to the isolated owner origin: no form, no registration, no
// credential/token/identity in the URL, no user-controlled destination, no cross-window messaging. Destination = a pinned HTTPS origin from a committed config
// (null today: the production hostname is not configured, so the entry is disabled). A loopback http origin is honoured ONLY when the public page itself is served
// from loopback (offline harness), never in production.
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import vm from 'vm';

const repo = path.resolve(__dirname, '../../..');
const buf = path.join(repo, 'frontend/buffett');
const read = (p) => fs.readFileSync(path.join(buf, p), 'utf8');
const html = read('index.html');

function loadEntry() {
  const sandbox = {};
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read('js/owner_entry.js'), sandbox);
  return sandbox.ownerEntry;
}
function loadConfig() {
  const sandbox = {};
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read('js/owner_entry_config.js'), sandbox);
  return sandbox.OWNER_ENTRY_CONFIG;
}
const PUBLIC = { protocol: 'https:', hostname: 'public.example.org', origin: 'https://public.example.org' };
const LOCAL_PAGE = { protocol: 'http:', hostname: '127.0.0.1', origin: 'http://127.0.0.1:3006' };
const cfg = (origin, ...rest) => ({ origin, path: rest.length ? rest[0] : '/owner/' });

describe('destination validation (pure, no input other than the pinned config and the page origin)', () => {
  const e = () => loadEntry();

  it('accepts a pinned https origin plus the one approved login path', () => {
    expect(e().resolve(cfg('https://owner.example.org'), PUBLIC)).toBe('https://owner.example.org/owner/');
    expect(e().resolve(cfg('https://sentinax-owner.pages.dev'), PUBLIC)).toBe('https://sentinax-owner.pages.dev/owner/');
    expect(e().resolve(cfg('https://a-b.c-d.example.co.uk'), PUBLIC)).toBe('https://a-b.c-d.example.co.uk/owner/');
  });

  it('is disabled (null) when nothing is configured', () => {
    for (const bad of [cfg(null), cfg(undefined), undefined, null, {}, [], 'x', 3]) expect(e().resolve(bad, PUBLIC), JSON.stringify(bad)).toBeNull();
  });

  it.each([
    'javascript:alert(1)', 'data:text/html,x', 'vbscript:x', 'file:///etc/passwd', '//evil.example', '/owner/', 'owner.example.org', 'ftp://owner.example.org',
    'http://owner.example.org', 'https://user@owner.example.org', 'https://user:pw@owner.example.org', 'https://owner.example.org:8443', 'https://owner.example.org/',
    'https://owner.example.org/x', 'https://owner.example.org?x=1', 'https://owner.example.org#frag', 'https://owner.example.org/?access_token=abc',
    'https://OWNER.example.org', 'https://owner.example.org ', ' https://owner.example.org', 'https://owner.example.org\\@evil.example', 'https://owner.example.org\n',
    'https://localhost', 'https://127.0.0.1', 'https://[::1]', 'https://1.2.3.4', 'https://-bad.example.org', 'https://bad-.example.org', 'https://a..example.org',
    'https://owner.example.org.', 'https://xn--.example.org\u0000', 'https://ownér.example.org', 'https://owner.example.org%2f@evil.example',
  ])('rejects the unsafe or malformed origin %j', (origin) => {
    expect(e().resolve(cfg(origin), PUBLIC), String(origin)).toBeNull();
  });

  it.each(['/owner', '/Owner/', '/owner/index.html', '/owner/?x=1', '/owner/#a', '//evil.example/', '/owner/../x', '/owner//', '', '/', '/other/', 'owner/', undefined, null, 5])(
    'only the exact approved path is accepted (rejects %j)', (p) => {
      expect(e().resolve(cfg('https://owner.example.org', p), PUBLIC)).toBeNull();
    });

  it('rejects extra or missing config keys', () => {
    expect(e().resolve({ origin: 'https://owner.example.org', path: '/owner/', next: 'https://evil.example' }, PUBLIC)).toBeNull();
    expect(e().resolve({ origin: 'https://owner.example.org' }, PUBLIC)).toBeNull();
    expect(e().resolve({ path: '/owner/' }, PUBLIC)).toBeNull();
  });

  it('never navigates to its own origin (the owner login must be a SEPARATE origin)', () => {
    expect(e().resolve(cfg('https://public.example.org'), PUBLIC)).toBeNull();
  });

  it('a loopback http origin is accepted only when the public page itself is loopback http and the ports differ', () => {
    expect(e().resolve(cfg('http://127.0.0.1:3005'), LOCAL_PAGE)).toBe('http://127.0.0.1:3005/owner/');
    expect(e().resolve(cfg('http://localhost:3005'), { protocol: 'http:', hostname: 'localhost', origin: 'http://localhost:3006' })).toBe('http://localhost:3005/owner/');
    expect(e().resolve(cfg('http://127.0.0.1:3006'), LOCAL_PAGE)).toBeNull();                       // same origin
    expect(e().resolve(cfg('http://127.0.0.1:3005'), PUBLIC)).toBeNull();                           // production page: loopback target refused
    expect(e().resolve(cfg('http://evil.example:3005'), LOCAL_PAGE)).toBeNull();                    // loopback page, non-loopback target
    expect(e().resolve(cfg('http://127.0.0.1'), LOCAL_PAGE)).toBeNull();                            // port required for the harness form
    expect(e().resolve(cfg('http://127.0.0.1:3005/x'), LOCAL_PAGE)).toBeNull();
    expect(e().resolve(cfg('http://user@127.0.0.1:3005'), LOCAL_PAGE)).toBeNull();
    expect(e().resolve(cfg('http://127.0.0.1:99999999'), LOCAL_PAGE)).toBeNull();
  });
});

describe('shipped configuration and sources', () => {
  it('no hostname is invented: the committed destination is null and the path is the approved one', () => {
    const c = loadConfig();
    expect(c).toEqual({ origin: null, path: '/owner/' });
    expect(Object.isFrozen(c)).toBe(true);
    expect(loadEntry().resolve(c, PUBLIC)).toBeNull();
  });

  it('the destination cannot be influenced by the visitor: no URL, referrer, storage, name or message input is read anywhere', () => {
    for (const f of ['js/owner_entry.js', 'js/owner_entry_config.js']) {
      const src = read(f);
      expect(src, f).not.toMatch(/location\.(search|hash|href|pathname)|URLSearchParams|document\.referrer|document\.cookie|localStorage|sessionStorage|window\.name|postMessage|BroadcastChannel|eval\(|new Function|innerHTML|import\(|fetch\(|XMLHttpRequest|WebSocket/);
      expect(src, f).not.toMatch(/token|supabase|createClient|password|owner_id|aal2|totp|authorization|bearer/i);
    }
    const app = read('js/app.js');
    expect(app.match(/setAttribute\('href'/g)).toHaveLength(1);
    expect(app).toMatch(/ownerEntry\.resolve\(window\.OWNER_ENTRY_CONFIG, window\.location\)/);
    expect(app).toContain("ownerLink.setAttribute('href', ownerTarget);");                // the validated URL is used verbatim: nothing is appended to it
    expect(app).not.toMatch(/ownerTarget\s*\+|\+\s*ownerTarget|\$\{ownerTarget/);
  });

  it('the page markup: one plain "Sahip Girişi" link, disabled until a destination resolves, no form, no registration, no auth code', () => {
    expect(html).toMatch(/<a id="owner-entry"[^>]*>Sahip Girişi<\/a>/);
    const link = html.match(/<a id="owner-entry"[^>]*>/)[0];
    expect(link).not.toMatch(/\bhref=/);
    expect(link).toContain('aria-disabled="true"');
    expect(link).toContain('rel="noopener noreferrer"');
    expect(link).toContain('referrerpolicy="no-referrer"');
    expect(link).not.toMatch(/target=|onclick|download|ping=/);
    expect(html).not.toMatch(/<(form|input|textarea|select|dialog)\b/i);
    expect(html).not.toMatch(/kayıt ol|üye ol|sign ?up|register|şifre|password|giriş yap/i);
    expect(html).not.toMatch(/supabase|createClient|auth\.js|owner_app|\/owner\/js|private_session/i);
    const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
    expect(scripts).toEqual(['js/owner_entry_config.js', 'js/owner_entry.js', 'js/api.js', 'js/ui.js', 'js/app.js']);
  });

  it('the header wraps on narrow screens so the added link can never push the layout off-screen', () => {
    const css = read('css/styles.css');
    expect(css).toMatch(/@media \(max-width: 720px\)[\s\S]*\.app-header \{ flex-wrap: wrap;[\s\S]*\.top-nav \{ flex-wrap: wrap;/);
  });

  it('the public page still contains only public Buffett research code and the fail-closed API client', () => {
    const files = [];
    const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).forEach((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : files.push(path.relative(buf, path.join(d, e.name)))));
    walk(buf);
    expect(files.sort()).toEqual(['css/styles.css', 'index.html', 'js/api.js', 'js/app.js', 'js/owner_entry.js', 'js/owner_entry_config.js', 'js/ui.js']);
    expect(read('js/api.js')).toContain("const API_BASE = '/buffett';");
    expect(read('js/api.js')).not.toMatch(/private|owner|Authorization/);
  });
});
