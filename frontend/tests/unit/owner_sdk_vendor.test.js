// @vitest-environment node
// Phase 25D1C2: the vendored Supabase SDK is a deterministic, reviewable build of the pinned lockfile version.
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

const repo = path.resolve(__dirname, '../../..');
const vendorDir = path.join(repo, 'frontend/owner/vendor');
const bundlePath = path.join(vendorDir, 'supabase-js.2.117.1.mjs');
const manifestPath = path.join(vendorDir, 'supabase-js.manifest.json');
const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');
const pkg = JSON.parse(fs.readFileSync(path.join(repo, 'package.json'), 'utf8'));
const lock = JSON.parse(fs.readFileSync(path.join(repo, 'package-lock.json'), 'utf8')).packages;

describe('vendored Supabase SDK contract', () => {
  it('manifest records package, lockfile integrity, toolchain and the artifact hash', () => {
    const m = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    expect(m.package).toBe('@supabase/supabase-js');
    expect(m.version).toBe('2.117.1');
    expect(pkg.devDependencies['@supabase/supabase-js']).toBe(m.version);
    expect(lock['node_modules/@supabase/supabase-js'].version).toBe(m.version);
    expect(m.lockfileIntegrity['@supabase/supabase-js']).toBe(lock['node_modules/@supabase/supabase-js'].integrity);
    expect(m.lockfileIntegrity['@supabase/auth-js']).toBe(lock['node_modules/@supabase/auth-js'].integrity);
    expect(m.esbuild).toBe(lock['node_modules/esbuild'].version);
    expect(m.sha256).toBe(sha256(fs.readFileSync(bundlePath)));
    expect(m.sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  it('esbuild is an explicit, exactly pinned direct dev dependency', () => {
    expect(pkg.devDependencies.esbuild).toMatch(/^\d+\.\d+\.\d+$/);
    expect(pkg.devDependencies.esbuild).toBe(lock['node_modules/esbuild'].version);
    expect(lock[''].devDependencies.esbuild).toBe(pkg.devDependencies.esbuild);
  });

  it('the committed bundle equals a fresh deterministic build from the lockfile', async () => {
    const { buildOwnerSdk } = await import('../../../scripts/owner_sdk_build.mjs');
    const a = await buildOwnerSdk();
    const b = await buildOwnerSdk();
    expect(sha256(a)).toBe(sha256(b));
    expect(sha256(a)).toBe(sha256(fs.readFileSync(bundlePath)));
  });

  it('the bundle fetches no code from other origins and exports createClient', () => {
    const src = fs.readFileSync(bundlePath, 'utf8');
    expect(src).not.toMatch(/\bfrom\s*["']https?:/);
    expect(src).not.toMatch(/\bimport\s*\(\s*["']https?:/);
    expect(src).not.toMatch(/\bimportScripts\b|\beval\s*\(|new Function\(/);
    expect(src).toMatch(/export\s*\{[^}]*\bcreateClient\b/);
  });
});
