// @vitest-environment node
// Phase 25D1E1: the OFFLINE Deep Ocean private shell (private_ui/). Static contract: honest data states, no invented figures, no session,
// storage, network or login, no external code, and it must never enter the public Buffett, legacy or owner/Cloudflare deployment artifacts.
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

const repo = path.resolve(__dirname, '../../..');
const ui = path.join(repo, 'private_ui');
const read = (p) => fs.readFileSync(path.join(ui, p), 'utf8');
const html = read('index.html');
const js = read('js/shell.js');
const css = read('css/deep_ocean.css');
const listAll = (d, base = d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listAll(path.join(d, e.name), base) : [path.relative(base, path.join(d, e.name))]));
const text = (s) => s.replace(/<script[\s\S]*?<\/script>/gi, ' ').replace(/<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&[a-z]+;/g, ' ').replace(/\s+/g, ' ');
const SECTIONS = ['overview', 'opportunities', 'portfolio', 'allocation', 'research', 'risk'];
const LABELS = ['Genel Bakış', 'Yatırım Fırsatları', 'Portföy', 'Dağılım ve Senaryolar', 'Piyasa Araştırması', 'Risk ve Kanıt'];

describe('structure and honest data states', () => {
  it('contains exactly the six private areas, each with a navigation entry and a panel', () => {
    for (const s of SECTIONS) {
      expect(html, s).toContain(`data-target="${s}"`);
      expect(html, s).toContain(`data-panel="${s}"`);
    }
    expect((html.match(/data-target="/g) || []).length).toBe(6);
    expect((html.match(/data-panel="/g) || []).length).toBe(6);
    const navText = text(html.match(/<nav[\s\S]*?<\/nav>/)[0]);
    for (const l of LABELS) expect(navText).toContain(l);
  });
  it('every panel declares an UNAVAILABLE state card (no source is connected) and nothing claims EMPTY or AVAILABLE data', () => {
    const panels = html.split(/<section /).slice(1);
    expect(panels).toHaveLength(6);
    for (const p of panels) {
      expect(p).toContain('chip chip-unavailable">UNAVAILABLE<');
      expect(p).not.toMatch(/state-card[^>]*state-(empty|available)/);
    }
    const legend = text(html.match(/<dl class="legend">[\s\S]*?<\/dl>/)[0]);
    for (const w of ['UNAVAILABLE', 'EMPTY', 'AVAILABLE']) expect(legend).toContain(w);
    expect(legend).toMatch(/“yok” demek değildir/);
  });
  it('states the prototype, decision-support and no-order nature visibly', () => {
    const t = text(html);
    expect(t).toMatch(/Çevrimdışı arayüz prototipi/);
    expect(t).toMatch(/oturum yok/);
    expect(t).toMatch(/yatırım tavsiyesi veya emir değildir/);
  });
  it('has the simple/advanced mode architecture, defaulting to simple, with provenance only in advanced mode', () => {
    expect(html).toContain('data-mode="simple"');
    expect(html).toContain('data-mode-set="simple"');
    expect(html).toContain('data-mode-set="advanced"');
    expect(css).toMatch(/body\[data-mode="simple"\] \.adv\s*\{\s*display:\s*none/);
    for (const f of ['contract_version', 'data_status', 'reason_code', 'source', 'as_of']) expect(html).toContain(f);
  });
  it('is responsive (grid collapses under 880px) and uses the Deep Ocean palette', () => {
    expect(css).toMatch(/@media \(max-width: 880px\)/);
    expect(css).toMatch(/grid-template-columns:\s*240px/);
    for (const c of ['#0b1120', '#38d6c8', '#0ea5e9', '#8b6cf6']) expect(css).toContain(c);
  });
});

describe('no invented financial figures', () => {
  it('visible text contains no numbers, percentages or currency amounts (the contract version string is the only digit)', () => {
    const visible = text(html.replace(/<tr><th scope="row">contract_version<\/th><td>[^<]*<\/td><\/tr>/, ''));
    expect(visible).not.toMatch(/\d/);
    expect(visible).not.toMatch(/[%₺$€£]/);
  });
  it('has no charts, canvases, images or illustrative curves', () => {
    expect(html).not.toMatch(/<(canvas|svg|img|picture|video|audio|iframe|object|embed)\b/i);
    expect(css).not.toMatch(/url\(/i);
    expect(js).not.toMatch(/Chart|canvas|Math\.random/);
  });
  it('does not render profit/return/price/score vocabulary as data', () => {
    const t = text(html).toLowerCase();
    for (const w of ['+%', 'kâr:', 'getiri:', 'fiyat:', 'skor:', 'al/sat', 'hedef fiyat']) expect(t).not.toContain(w);
  });
});

describe('no session, storage, network, login or external code', () => {
  const all = [['index.html', html], ['shell.js', js], ['deep_ocean.css', css]];
  it('has no storage, cookie, network, token or messaging API', () => {
    for (const [f, src] of all) {
      for (const banned of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'fetch(', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', 'postMessage', 'window.open', 'import(', 'eval(', 'new Function', 'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'serviceWorker']) {
        expect(src, `${f} must not contain ${banned}`).not.toContain(banned);
      }
      expect(src, f).not.toMatch(/access_token|refresh_token|Authorization|Bearer|supabase|createClient|sb_publishable_|sb_secret_|eyJ[A-Za-z0-9_-]{10,}|service_role|password/i);
      expect(src, f).not.toMatch(/location\.(hash|search)\s*=|history\.(push|replace)State/);
    }
  });
  it('has no login form, input, owner-page import or owner SDK reference', () => {
    expect(html).not.toMatch(/<(form|input|textarea|select)\b/i);
    expect(html).not.toMatch(/owner\/|owner_app|supabase-js/i);
  });
  it('loads only its own two assets and no external or inline executable code, with a strict CSP', () => {
    expect([...html.matchAll(/\b(?:src|href)="([^"#]+)"/g)].map((m) => m[1]).sort()).toEqual(['css/deep_ocean.css', 'js/shell.js']);
    expect(html).not.toMatch(/https?:\/\//);
    expect(css).not.toMatch(/https?:|@import/);
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)/i);
    expect(html).not.toMatch(/\son[a-z]+\s*=|\sstyle=/i);
    const csp = html.match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
    expect(csp).not.toMatch(/unsafe-inline|unsafe-eval|\*/);
    for (const d of ["default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'none'", "base-uri 'none'", "form-action 'none'", "object-src 'none'"]) expect(csp).toContain(d);
    expect(html).toContain('name="referrer" content="no-referrer"');
  });
  it('the only files are the shell, its stylesheet and its script', () => {
    expect(listAll(ui).sort()).toEqual(['css/deep_ocean.css', 'index.html', 'js/shell.js']);
  });
});

describe('deployment isolation: the private shell is in no artifact', () => {
  it('lives outside frontend/ (legacy hosting root) and is referenced by no deployed tree', () => {
    expect(fs.existsSync(path.join(repo, 'frontend/private_ui'))).toBe(false);
    for (const dir of ['frontend', 'frontend/buffett', 'frontend/owner', 'deploy/owner-cloudflare', 'deploy/owner-bootstrap']) {
      const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.name === 'node_modules' || e.name === 'dist' ? [] : e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
      for (const f of walk(path.join(repo, dir))) {
        if (/\.(png|jpg|jpeg|svg|ico|woff2|mjs)$/.test(f) || f.includes('/tests/')) continue;
        expect(fs.readFileSync(f, 'utf8'), f).not.toContain('private_ui');
      }
    }
    for (const f of ['vercel.json', 'frontend/vercel.json', 'deploy/owner-bootstrap/vercel.json']) expect(fs.readFileSync(path.join(repo, f), 'utf8'), f).not.toContain('private_ui');
  });
  it('the owner allowlist and both owner builds exclude it', async () => {
    const dep = await import('../../../scripts/owner_deploy_build.mjs');
    const cf = await import('../../../scripts/owner_cloudflare_build.mjs');
    expect(dep.OWNER_ALLOWLIST.some((f) => f.includes('private'))).toBe(false);
    const out = fs.mkdtempSync(path.join(os.tmpdir(), 'pui-'));
    try {
      await cf.buildOwnerCloudflare({ outDir: path.join(out, 'dist') });
      const files = listAll(path.join(out, 'dist'));
      expect(files.some((f) => /private|shell\.js|deep_ocean/.test(f))).toBe(false);
      expect(files).toHaveLength(9);
    } finally { fs.rmSync(out, { recursive: true, force: true }); }
  });
  it('no workflow deploys it and no public page links to it', () => {
    for (const f of fs.readdirSync(path.join(repo, '.github/workflows'))) expect(fs.readFileSync(path.join(repo, '.github/workflows', f), 'utf8'), f).not.toMatch(/private_ui.*(deploy|publish|upload|pages)/is);
    expect(fs.readFileSync(path.join(repo, 'frontend/index.html'), 'utf8')).not.toContain('private_ui');
  });
});
