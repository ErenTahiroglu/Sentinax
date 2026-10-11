// @vitest-environment node
// Phase 25D1C3: the isolated owner deployment package (dedicated Vercel project, Root Directory deploy/owner-bootstrap).
// The artifact is built by scripts/owner_deploy_build.mjs into an ignored dist/; routing/headers are modelled by
// scripts/owner_deploy_model.mjs. This is a static-package contract, NOT a Vercel deployment check.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import crypto from 'crypto';

const repo = path.resolve(__dirname, '../../..');
const deployDir = path.join(repo, 'deploy/owner-bootstrap');
const ownerSrc = path.join(repo, 'frontend/owner');
const cfg = () => JSON.parse(fs.readFileSync(path.join(deployDir, 'vercel.json'), 'utf8'));

const REQUIRED_URLS = [
  '/owner', '/owner/', '/owner/index.html',
  '/owner/css/owner.css', '/owner/js/owner_app.js', '/owner/js/client_config.js',
  '/owner/js/owner_public_config.js', '/owner/js/mfa_flow.js', '/owner/vendor/supabase-js.2.117.1.mjs',
  '/owner/css/deep_ocean.css', '/owner/js/private_api_config.js', '/owner/js/private_session.js', '/owner/js/private_shell.js', '/owner/js/private_import_preview.js',
];
const EXPECTED_FILES = [
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
  'owner/js/private_import_preview.js',
  'owner/vendor/supabase-js.2.117.1.mjs',
];

let build, model, out, dist;
const listAll = (d, base = d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listAll(path.join(d, e.name), base) : [path.relative(base, path.join(d, e.name))]));
const follow = (pathname) => {
  for (let i = 0; i < 4; i++) {
    const r = model.resolveRequest(dist, cfg(), pathname);
    if (r.status === 308) { pathname = r.location; continue; }
    return { ...r, finalPath: pathname };
  }
  throw new Error('redirect loop');
};

beforeAll(async () => {
  build = await import('../../../scripts/owner_deploy_build.mjs');
  model = await import('../../../scripts/owner_deploy_model.mjs');
  out = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-deploy-'));
  dist = path.join(out, 'dist');
  await build.buildOwnerDeploy({ outDir: dist });
});
afterAll(() => out && fs.rmSync(out, { recursive: true, force: true }));

describe('artifact contents (allowlist)', () => {
  it('contains exactly the owner bootstrap files and nothing else', () => {
    expect(listAll(dist).sort()).toEqual([...EXPECTED_FILES].sort());
  });
  it('exposes no legacy SPA, Buffett app, service worker, backend or finance files', () => {
    const files = listAll(dist);
    for (const f of files) {
      expect(f.startsWith('owner/'), f).toBe(true);
      expect(f, f).not.toMatch(/(^|\/)(sw|manifest|service-worker)[^/]*\.(js|json)$/i);
      expect(f, f).not.toMatch(/buffett|backend|ledger|portfolio|\.py$|\.sql$|\.env|supabase-js\.manifest/i);
    }
    for (const forbidden of ['index.html', 'sw.js', 'js/core/config.js', 'buffett', 'backend']) {
      expect(fs.existsSync(path.join(dist, forbidden)), forbidden).toBe(false);
    }
  });
  it('is byte-identical to the owner sources (no duplicated or altered SDK)', () => {
    for (const f of EXPECTED_FILES) {
      const a = fs.readFileSync(path.join(dist, f));
      const b = fs.readFileSync(path.join(ownerSrc, f.replace(/^owner\//, '')));
      expect(a.equals(b), f).toBe(true);
    }
  });
  it('SDK bundle matches its recorded integrity manifest', () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(ownerSrc, 'vendor/supabase-js.manifest.json'), 'utf8'));
    const sha = crypto.createHash('sha256').update(fs.readFileSync(path.join(dist, 'owner/vendor/supabase-js.2.117.1.mjs'))).digest('hex');
    expect(sha).toBe(manifest.sha256 ?? manifest.bundle?.sha256);
  });
});

describe('content safety of the artifact', () => {
  const text = () => listAll(dist).map((f) => [f, fs.readFileSync(path.join(dist, f), 'utf8')]);
  it('loads no external executable JavaScript and registers no service worker', () => {
    for (const [f, src] of text()) {
      expect(src, f).not.toMatch(/<script[^>]+src="https?:/i);
      expect(src, f).not.toMatch(/from\s+['"]https?:/);
      expect(src, f).not.toMatch(/import\(\s*['"]https?:/);
      expect(src, f).not.toMatch(/serviceWorker\s*\.\s*register/);
      expect(src, f).not.toMatch(/importScripts\s*\(/);
    }
  });
  it('contains no privileged credential, user token, owner id or password material', () => {
    for (const [f, src] of text()) {
      expect(src, f).not.toMatch(/sb_secret_[A-Za-z0-9_-]{16,}/);
      expect(src, f).not.toMatch(/service_role/i);
      expect(src, f).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}/);
      expect(src, f).not.toMatch(/SUPABASE_JWT_SECRET|SERVICE_ROLE|-----BEGIN [A-Z ]*PRIVATE KEY/);
      expect(src, f).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[1-9a-f][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i);
    }
  });
  it('the only sb_ key shipped is the publishable one', () => {
    const keys = text().flatMap(([, s]) => s.match(/sb_[a-z]+_[A-Za-z0-9_-]{16,}/g) || []);
    expect(keys.length).toBeGreaterThan(0);
    for (const k of keys) expect(k.startsWith('sb_publishable_'), 'non-publishable sb_ key').toBe(true);
  });
});

describe('routing (modelled; unknown routes fail closed)', () => {
  for (const url of REQUIRED_URLS) {
    it(`${url} resolves to an owner file with 200`, () => {
      const r = follow(url);
      expect(r.status).toBe(200);
      expect(path.relative(dist, r.file).startsWith('owner' + path.sep)).toBe(true);
    });
  }
  it('/owner, /owner/ and /owner/index.html all end at the owner page', () => {
    for (const u of ['/owner', '/owner/', '/owner/index.html']) expect(follow(u).file).toBe(path.join(dist, 'owner/index.html'));
  });
  it('root and legacy/app/API/worker paths are 404 and never return owner HTML', () => {
    for (const u of ['/', '/index.html', '/sw.js', '/manifest.json', '/js/core/config.js', '/frontend/index.html', '/frontend/owner/index.html', '/buffett/index.html',
      '/api/v1/portfolio', '/api/buffett', '/owner/js/missing.js', '/owner/../index.html', '/owner/js/', '/owner/vendor/supabase-js.manifest.json', '/owner/%2e%2e/sw.js', '/owner/js/owner_app.js.map']) {
      const r = follow(u);
      expect(r.status, u).toBe(404);
      expect(r.file, u).toBeUndefined();
    }
  });
  it('config declares no rewrites or fallbacks', () => {
    const c = cfg();
    expect(c.rewrites ?? []).toEqual([]);
    expect(c.routes ?? []).toEqual([]);
    expect(c.redirects ?? []).toEqual([]);
  });
});

describe('deployment config (deploy/owner-bootstrap/vercel.json)', () => {
  it('builds only the owner artifact into an ignored directory without extra install steps', () => {
    const c = cfg();
    expect(c.framework ?? null).toBeNull();
    expect(c.buildCommand).toBe('node ../../scripts/owner_deploy_build.mjs');
    expect(c.outputDirectory).toBe('dist');
    expect(c.cleanUrls).toBe(true);
    expect(c.trailingSlash).toBe(false);
    expect(fs.existsSync(path.join(deployDir, 'package.json'))).toBe(false);
    expect(fs.readFileSync(path.join(repo, '.gitignore'), 'utf8')).toMatch(/^dist\/$/m);
  });
  const headersFor = (url) => model.headersFor(cfg(), url);
  const csp = (h) => Object.fromEntries(h['content-security-policy'].split(';').map((d) => d.trim().split(/\s+/)).filter((d) => d[0]).map(([k, ...v]) => [k, v]));
  it('every path (including 404s) receives the same strict header set', () => {
    for (const u of ['/', '/owner', '/owner/js/owner_app.js', '/anything/else']) {
      const h = headersFor(u);
      expect(h['cache-control']).toBe('no-store');
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
  it('header CSP is strict, matches the meta CSP and pins connect-src to the project origin', async () => {
    const h = headersFor('/owner');
    const d = csp(h);
    const pub = (await import('../../owner/js/owner_public_config.js')).OWNER_PUBLIC_CONFIG;
    expect(h['content-security-policy']).not.toMatch(/unsafe-inline|unsafe-eval|\*|https?:\/\/(?!zlggrmsolklhfgijcjnz)/);
    expect(d['default-src']).toEqual(["'none'"]);
    expect(d['script-src']).toEqual(["'self'"]);
    expect(d['style-src']).toEqual(["'self'"]);
    expect(d['connect-src']).toEqual(["'self'", pub.supabaseUrl]);
    expect(d['frame-ancestors']).toEqual(["'none'"]);
    for (const k of ['base-uri', 'form-action', 'object-src']) expect(d[k]).toEqual(["'none'"]);
    const meta = fs.readFileSync(path.join(ownerSrc, 'index.html'), 'utf8').match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
    for (const dir of ['default-src', 'script-src', 'style-src', 'connect-src', 'base-uri', 'form-action', 'object-src']) {
      expect(meta, dir).toContain(`${dir} ${d[dir].join(' ')}`);
    }
  });
});

describe('build fails closed', () => {
  const copyOwner = () => {
    const src = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-src-'));
    fs.cpSync(ownerSrc, src, { recursive: true });
    return src;
  };
  const tryBuild = (src) => {
    const o = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-out-'));
    return build.buildOwnerDeploy({ outDir: path.join(o, 'dist'), sourceDir: src }).then(() => 'ok', (e) => String(e.message));
  };
  for (const missing of ['index.html', 'css/owner.css', 'js/owner_app.js', 'js/client_config.js', 'js/owner_public_config.js', 'js/mfa_flow.js', 'css/deep_ocean.css', 'js/private_api_config.js', 'js/private_session.js', 'js/private_shell.js', 'js/private_import_preview.js', 'vendor/supabase-js.2.117.1.mjs']) {
    it(`missing ${missing} fails the build`, async () => {
      const src = copyOwner();
      fs.rmSync(path.join(src, missing));
      expect(await tryBuild(src)).toMatch(/missing required owner file/i);
    });
  }
  it('a tampered SDK bundle fails the integrity check', async () => {
    const src = copyOwner();
    fs.appendFileSync(path.join(src, 'vendor/supabase-js.2.117.1.mjs'), '\n// x');
    expect(await tryBuild(src)).toMatch(/integrity/i);
  });
  it('an external script or a legacy config reference fails the build', async () => {
    const src = copyOwner();
    const f = path.join(src, 'index.html');
    fs.writeFileSync(f, fs.readFileSync(f, 'utf8').replace('</head>', '<script src="https://cdn.jsdelivr.net/x.js"></script></head>'));
    expect(await tryBuild(src)).toMatch(/external|forbidden/i);
  });
  it('a secret-looking key in the artifact fails the build', async () => {
    const src = copyOwner();
    const f = path.join(src, 'js/owner_public_config.js');
    fs.appendFileSync(f, "\nexport const X = 'sb_secret_" + 'a'.repeat(30) + "';\n");
    expect(await tryBuild(src)).toMatch(/forbidden|secret/i);
  });
  it('an unexpected extra file in the source tree is not copied', async () => {
    const src = copyOwner();
    fs.writeFileSync(path.join(src, 'js/legacy_extra.js'), 'export {}');
    const o = fs.mkdtempSync(path.join(os.tmpdir(), 'owner-out-'));
    await build.buildOwnerDeploy({ outDir: path.join(o, 'dist'), sourceDir: src });
    expect(fs.existsSync(path.join(o, 'dist/owner/js/legacy_extra.js'))).toBe(false);
  });
});

describe('gitleaks exception scope', () => {
  const tracked = () => {
    const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => {
      if (['node_modules', '.git', 'graphify-out', 'Phase 27', 'dist', 'coverage'].includes(e.name)) return [];
      return e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)];
    });
    return ['frontend', 'scripts', 'deploy', '.github', 'backend/api', 'backend/infrastructure'].flatMap((d) => (fs.existsSync(path.join(repo, d)) ? walk(path.join(repo, d)) : []));
  };
  it('the inline gitleaks:allow marker exists only on the publishable-key line of owner_public_config.js', () => {
    const hits = [];
    for (const f of tracked()) {
      if (!/\.(js|mjs|json|ya?ml|html|py|ts|md|toml)$/.test(f)) continue;
      if (f.endsWith('owner_deploy_artifact.test.js')) continue; // this file names the marker in its own assertions
      fs.readFileSync(f, 'utf8').split('\n').forEach((line, i) => { if (line.includes('gitleaks:allow')) hits.push([path.relative(repo, f), line]); });
    }
    expect(hits.length).toBe(1);
    expect(hits[0][0]).toBe('frontend/owner/js/owner_public_config.js');
    expect(hits[0][1]).toMatch(/publishableKey:\s*'sb_publishable_[A-Za-z0-9_-]+',/);
    expect(hits[0][1]).not.toMatch(/sb_secret_|service_role|eyJ|password/i);
  });
  it('the exempted value is a well-formed publishable key and nothing else in that file carries a key-like literal', () => {
    const src = fs.readFileSync(path.join(ownerSrc, 'js/owner_public_config.js'), 'utf8');
    const values = src.match(/sb_[a-z]+_[A-Za-z0-9_-]{16,}/g);
    expect(values).toHaveLength(1);
    expect(values[0]).toMatch(/^sb_publishable_/);
    expect(src).not.toMatch(/eyJ|sb_secret_|service_role|password|token/i);
  });
  it('.gitleaksignore stays a list of exact fingerprints (no path-wide or pattern rules)', () => {
    const lines = fs.readFileSync(path.join(repo, '.gitleaksignore'), 'utf8').split('\n').filter((l) => l.trim() && !l.startsWith('#'));
    for (const l of lines) expect(l, l).toMatch(/^[0-9a-f]{40}:[^:]+:[\w-]+:\d+$/);
  });
});
