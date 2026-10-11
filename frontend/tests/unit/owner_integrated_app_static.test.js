// @vitest-environment node
// Phase 25D1E3: static contract of the integrated owner document (sign-in + MFA + Deep Ocean private application in ONE document, ONE memory-only client).
// Replaces the offline-prototype shell contract (private_ui) with equal-or-greater coverage on the real integrated page.
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

const repo = path.resolve(__dirname, '../../..');
const owner = path.join(repo, 'frontend/owner');
const read = (p) => fs.readFileSync(path.join(owner, p), 'utf8');
const html = read('index.html');
const css = read('css/deep_ocean.css');
const PRIVATE_JS = ['js/private_session.js', 'js/private_shell.js', 'js/private_api_config.js', 'js/private_import_preview.js'];
const ALL_JS = ['js/owner_app.js', 'js/mfa_flow.js', 'js/client_config.js', 'js/owner_public_config.js', ...PRIVATE_JS];
const appHtml = html.match(/<div id="app-root"[\s\S]*?<script type="module"/)[0];
const text = (s) => s.replace(/<script[\s\S]*?<\/script>/gi, ' ').replace(/<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&[a-z]+;/g, ' ').replace(/\s+/g, ' ');
const SECTIONS = ['overview', 'opportunities', 'portfolio', 'allocation', 'research', 'risk'];
const LABELS = ['Genel Bakış', 'Yatırım Fırsatları', 'Portföy', 'Dağılım ve Senaryolar', 'Piyasa Araştırması', 'Risk ve Kanıt'];
const listAll = (d, base = d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listAll(path.join(d, e.name), base) : [path.relative(base, path.join(d, e.name))]));

describe('one document, no contradictory views', () => {
  it('the application root is hidden by default and lives in the owner document (no second page, no reload)', () => {
    expect(html).toMatch(/<div id="app-root" class="app" data-mode="simple" hidden>/);
    expect(fs.existsSync(path.join(repo, 'private_ui'))).toBe(false);
    expect(html.match(/<script\b[^>]*>/gi)).toHaveLength(1);
    expect(html).not.toMatch(/location\.(assign|replace|href)|<meta[^>]+http-equiv="refresh"|window\.open/i);
  });
  it('showing the application hides the sign-in shell and hiding it restores the shell; everything session-derived is cleared on hide', () => {
    const shell = read('js/private_shell.js');
    expect(shell).toMatch(/function hide\(\)[\s\S]*root\.hidden = true[\s\S]*shell\.hidden = false[\s\S]*clearFields\(\)/);
    expect(shell).toMatch(/function reveal\(summary\)[\s\S]*shell\.hidden = true[\s\S]*root\.hidden = false/);
    expect(shell).toMatch(/if \(status === AUTHZ\.AUTHORIZED && summary\) reveal\(summary\); else hide\(\);/);
  });
  it('the controller is wired to the same flow and the same client; sign-out buttons call the existing flow', () => {
    const app = read('js/owner_app.js');
    expect(app).toContain("flow.onChange((view) => privateSession.onFlowChange(view));");
    expect(app).toContain("onSignOut: () => flow.signOut('local')");
    expect(app).toContain("onSignOutAll: () => flow.signOut('global')");
    expect(app).toContain('getSession: async () => (await client.auth.getSession()).data?.session ?? null');
  });
});

describe('exactly one Supabase client with the memory-only session options', () => {
  it('createClient is called once in the whole owner tree and the private modules never touch the SDK', () => {
    const calls = ALL_JS.map((f) => (read(f).match(/createClient\(/g) || []).length);
    expect(calls.reduce((a, b) => a + b, 0)).toBe(1);
    for (const f of PRIVATE_JS) {
      const src = read(f);
      expect(src, f).not.toMatch(/createClient|supabase|SUPABASE_SDK_PATH|auth\.(signIn|signOut|mfa|getSession|refreshSession)|OWNER_AUTH_OPTIONS/i);
    }
  });
  it('the preserved auth options are unchanged', async () => {
    const { OWNER_AUTH_OPTIONS } = await import('../../owner/js/client_config.js');
    expect(OWNER_AUTH_OPTIONS).toEqual({ persistSession: false, autoRefreshToken: true, detectSessionInUrl: false });
    expect(Object.isFrozen(OWNER_AUTH_OPTIONS)).toBe(true);
  });
});

describe('no storage, no handoff, no leakage of the access token', () => {
  const BANNED = ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', 'postMessage', 'BroadcastChannel', 'window.open',
    'window.name', 'eval(', 'new Function', 'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'serviceWorker', 'console.', 'import('];
  it('the private modules use no storage, messaging, HTML-injection or logging API', () => {
    for (const f of PRIVATE_JS) for (const b of BANNED) expect(read(f), `${f} must not contain ${b}`).not.toContain(b);
    for (const f of PRIVATE_JS) expect(read(f), f).not.toMatch(/location\.|history\.|URLSearchParams|document\.referrer/);
  });
  it('the only network call in the whole owner tree besides the SDK is the single fetch wrapper in owner_app.js', () => {
    const fetchCalls = ALL_JS.flatMap((f) => (read(f).match(/\bfetch\(/g) || []).map(() => f));
    expect(fetchCalls).toEqual(['js/owner_app.js']);
  });
  it('the Bearer header is built in exactly one place and the request is credential-less, uncached, redirect-free and referrer-free', () => {
    const where = ALL_JS.filter((f) => /Bearer|Authorization/.test(read(f)));
    expect(where).toEqual(['js/private_session.js']);
    const src = read('js/private_session.js');
    for (const flag of ["credentials: 'omit'", "cache: 'no-store'", "redirect: 'error'", "referrerPolicy: 'no-referrer'", "mode: 'same-origin'", "method: 'GET'"]) expect(src).toContain(flag);
    expect(src).not.toMatch(/\bbody:|POST|PUT|PATCH|DELETE|[?#]token|access_token=/);
  });
  it('no module logs, and the state object handed to listeners can never contain the token', () => {
    const src = read('js/private_session.js');
    expect(src).toMatch(/const snap = \{ status, summary \};/);
    expect(src).not.toMatch(/state\s*=\s*\{[^}]*token/i);
  });
});

describe('private API target and CSP are exact', () => {
  it('the private API is a fixed same-origin read-only path; no hostname, URL or wildcard exists anywhere in the owner tree', () => {
    expect(read('js/private_api_config.js').replace(/\/\*[\s\S]*?\*\//g, '')).not.toMatch(/https?:|\/\//);
    for (const f of PRIVATE_JS) expect(read(f), f).not.toMatch(/['"`]https?:\/\//);
    expect(read('js/private_session.js')).toContain("export const PRIVATE_SUMMARY_PATH = '/private/portfolio/summary';");
  });
  it.each([['meta CSP', () => html.match(/Content-Security-Policy"\s+content="([^"]+)"/)[1]], ['Cloudflare _headers CSP', () => fs.readFileSync(path.join(repo, 'deploy/owner-cloudflare/_headers'), 'utf8').match(/Content-Security-Policy: (.+)/)[1]]])(
    '%s allows exactly the own origin and the Supabase project for connections, no wildcard, no unsafe-*',
    async (_n, get) => {
      const { OWNER_PUBLIC_CONFIG } = await import('../../owner/js/owner_public_config.js');
      const csp = get();
      expect(csp).toContain(`connect-src 'self' ${OWNER_PUBLIC_CONFIG.supabaseUrl};`);
      expect(csp).not.toMatch(/unsafe-inline|unsafe-eval|\*|connect-src[^;]*https?:\/\/(?!zlggrmsolklhfgijcjnz\.supabase\.co)/);
      expect(csp).toMatch(/script-src 'self'(;|$)/);
    },
  );
  it('global backend CORS is not widened by this phase', () => {
    const main = fs.readFileSync(path.join(repo, 'backend/api/main.py'), 'utf8');
    expect(main).not.toMatch(/allow_origins\s*=\s*\[\s*["']\*["']/);
    expect(main).toContain('allow_origins=origins');
  });
});

describe('Deep Ocean application content (carried over from the offline prototype)', () => {
  it('contains the six areas with navigation entries and panels', () => {
    for (const s of SECTIONS) { expect(appHtml).toContain(`data-target="${s}"`); expect(appHtml).toContain(`data-panel="${s}"`); }
    expect((appHtml.match(/data-target="/g) || []).length).toBe(6);
    expect((appHtml.match(/data-panel="/g) || []).length).toBe(6);
    const nav = text(appHtml.match(/<nav[\s\S]*?<\/nav>/)[0]);
    for (const l of LABELS) expect(nav).toContain(l);
  });
  it('every panel declares an UNAVAILABLE state card and nothing claims EMPTY or AVAILABLE data; the legend defines all three', () => {
    const panels = appHtml.split(/<section /).slice(1);
    expect(panels).toHaveLength(6);
    for (const p of panels) { expect(p).toContain('chip chip-unavailable">UNAVAILABLE<'); expect(p).not.toMatch(/state-card[^>]*state-(empty|available)/); }
    const legend = text(appHtml.match(/<dl class="legend">[\s\S]*?<\/dl>/)[0]);
    for (const w of ['UNAVAILABLE', 'EMPTY', 'AVAILABLE']) expect(legend).toContain(w);
  });
  it('the banner states what is true: server authorization done, no data source connected, no advice or orders', () => {
    const t = text(appHtml);
    expect(t).toMatch(/Sunucu sahip yetkilendirmesi doğrulandı/);
    expect(t).toMatch(/hiçbir özel veri kaynağı bağlı değil/);
    expect(t).toMatch(/yatırım tavsiyesi veya emir değildir/);
    expect(t).not.toMatch(/oturum yok|çevrimdışı arayüz prototipi/i);
  });
  it('has the simple/advanced architecture (default simple) and the responsive Deep Ocean palette, all scoped under .app', () => {
    expect(appHtml).toContain('data-mode-set="simple"');
    expect(appHtml).toContain('data-mode-set="advanced"');
    expect(css).toMatch(/\.app\[data-mode="simple"\] \.adv\s*\{\s*display:\s*none/);
    expect(css).toMatch(/@media \(max-width: 880px\)/);
    for (const c of ['#0b1120', '#38d6c8', '#0ea5e9', '#8b6cf6']) expect(css).toContain(c);
    for (const rule of css.split('}').map((r) => r.trim()).filter((r) => r && !r.startsWith('@') && !r.startsWith('/*'))) {
      const sel = rule.split('{')[0].trim();
      if (sel && !sel.startsWith('body.app-active') && !/^(from|to|\d+%)/.test(sel)) expect(sel, 'selector must be scoped').toMatch(/\.app\b/);
    }
  });
  it('visible text of the application contains no numbers, percentages or currency amounts (live fields are filled only from the server envelope)', () => {
    const t = text(appHtml);
    expect(t).not.toMatch(/\d/);
    expect(t).not.toMatch(/[%₺$€£]/);
  });
  it('has no charts, images, canvases or illustrative curves, and no form inside the application', () => {
    expect(appHtml).not.toMatch(/<(canvas|svg|img|picture|video|audio|iframe|object|embed|form|input|textarea|select)\b/i);
    expect(css).not.toMatch(/url\(|@import|https?:/i);
    for (const f of PRIVATE_JS) expect(read(f), f).not.toMatch(/Chart|canvas|Math\.random/);
  });
  it('live fields are written as text nodes only and the data-status is shown as received', () => {
    const shell = read('js/private_shell.js');
    expect((shell.match(/\.textContent = /g) || []).length).toBeGreaterThanOrEqual(8);
    expect(shell).not.toMatch(/\.(inner|outer)HTML|insertAdjacentHTML|createContextualFragment/);
  });
});

describe('deployment isolation and artifact contents', () => {
  it('legacy Vercel project roots exclude the owner tree, so a legacy host cannot ship the sign-in or the private application', () => {
    const ign = (f) => fs.readFileSync(path.join(repo, f), 'utf8').split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
    expect(ign('.vercelignore')).toEqual(expect.arrayContaining(['frontend/owner/', 'deploy/']));
    expect(ign('frontend/.vercelignore')).toEqual(['owner/']);
  });
  it('neither the legacy SPA nor the public Buffett frontend references the owner application', () => {
    const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.name === 'node_modules' || e.name === 'owner' || e.name === 'tests' ? [] : e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
    for (const f of walk(path.join(repo, 'frontend'))) {
      if (!/\.(js|html|css|json)$/.test(f)) continue;
      expect(fs.readFileSync(f, 'utf8'), f).not.toMatch(/private_session|private_shell|private_api_config|deep_ocean|\/owner\/js\/|app-root/);
    }
  });
  it('the Cloudflare artifact is exactly the 12 owner files + 404.html + _headers (an explicit, reviewed allowlist) and contains no legacy or public-Buffett file', async () => {
    const cf = await import('../../../scripts/owner_cloudflare_build.mjs');
    const out = fs.mkdtempSync(path.join(os.tmpdir(), 'iapp-'));
    try {
      await cf.buildOwnerCloudflare({ outDir: path.join(out, 'dist') });
      const files = listAll(path.join(out, 'dist')).sort();
      expect(files).toEqual(['404.html', '_headers', 'owner/css/deep_ocean.css', 'owner/css/owner.css', 'owner/index.html', 'owner/js/client_config.js', 'owner/js/mfa_flow.js', 'owner/js/owner_app.js',
        'owner/js/owner_public_config.js', 'owner/js/private_api_config.js', 'owner/js/private_import_preview.js', 'owner/js/private_session.js', 'owner/js/private_shell.js', 'owner/vendor/supabase-js.2.117.1.mjs']);
      expect(files.some((f) => /buffett|sw\.js|manifest|private_ui|fixtures/i.test(f))).toBe(false);
    } finally { fs.rmSync(out, { recursive: true, force: true }); }
  });
  it('no workflow deploys the owner application and the SDK integrity contract is still enforced by the build', () => {
    for (const f of fs.readdirSync(path.join(repo, '.github/workflows'))) expect(fs.readFileSync(path.join(repo, '.github/workflows', f), 'utf8'), f).not.toMatch(/CLOUDFLARE_|wrangler|pages deploy|vercel (--prod|deploy)/i);
    expect(fs.readFileSync(path.join(repo, 'scripts/owner_deploy_build.mjs'), 'utf8')).toContain('SDK bundle integrity check failed');
  });
});
