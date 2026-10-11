// Phase 25D1C2: deterministic local build of the pinned Supabase SDK for the owner bootstrap page.
//
//   node scripts/owner_sdk_build.mjs --write   regenerate frontend/owner/vendor/ (bundle + manifest)
//   node scripts/owner_sdk_build.mjs --check   fail unless the committed bundle/manifest equal a fresh build
//
// Input is exactly the package-lock.json-pinned @supabase/supabase-js; the only export is createClient.
// The build performs no network access and no cryptography of its own.
import { build, version as esbuildVersion } from 'esbuild';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const VENDOR = path.join(REPO, 'frontend/owner/vendor');
const SDK_VERSION = '2.117.1';
export const BUNDLE_FILE = path.join(VENDOR, `supabase-js.${SDK_VERSION}.mjs`);
export const MANIFEST_FILE = path.join(VENDOR, 'supabase-js.manifest.json');

const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

export async function buildOwnerSdk() {
    const out = await build({
        stdin: { contents: "export { createClient } from '@supabase/supabase-js';", resolveDir: REPO, loader: 'js' },
        absWorkingDir: REPO,
        bundle: true,
        format: 'esm',
        platform: 'browser',
        target: 'es2020',
        minify: true,
        legalComments: 'none',
        write: false,
        logLevel: 'silent',
    });
    return Buffer.from(out.outputFiles[0].contents);
}

export function expectedManifest(bundle) {
    const lock = JSON.parse(fs.readFileSync(path.join(REPO, 'package-lock.json'), 'utf8')).packages;
    const pin = (name) => {
        const entry = lock[`node_modules/${name}`];
        if (!entry || !entry.integrity) throw new Error(`lockfile has no integrity for ${name}`);
        return entry.integrity;
    };
    const supabase = lock['node_modules/@supabase/supabase-js'];
    if (supabase.version !== SDK_VERSION) throw new Error(`lockfile pins supabase-js ${supabase.version}, expected ${SDK_VERSION}`);
    return {
        package: '@supabase/supabase-js',
        version: SDK_VERSION,
        entry: "export { createClient } from '@supabase/supabase-js'",
        esbuild: esbuildVersion,
        build: 'node scripts/owner_sdk_build.mjs --write',
        lockfileIntegrity: {
            '@supabase/supabase-js': pin('@supabase/supabase-js'),
            '@supabase/auth-js': pin('@supabase/auth-js'),
        },
        bytes: bundle.length,
        sha256: sha256(bundle),
    };
}

async function main(mode) {
    const bundle = await buildOwnerSdk();
    const manifest = `${JSON.stringify(expectedManifest(bundle), null, 2)}\n`;
    if (mode === '--write') {
        fs.mkdirSync(VENDOR, { recursive: true });
        fs.writeFileSync(BUNDLE_FILE, bundle);
        fs.writeFileSync(MANIFEST_FILE, manifest);
        console.log(`wrote ${path.relative(REPO, BUNDLE_FILE)} sha256=${sha256(bundle)}`);
        return 0;
    }
    if (mode === '--check') {
        const same = fs.existsSync(BUNDLE_FILE) && fs.existsSync(MANIFEST_FILE)
            && Buffer.compare(fs.readFileSync(BUNDLE_FILE), bundle) === 0
            && fs.readFileSync(MANIFEST_FILE, 'utf8') === manifest;
        console.log(same ? 'owner SDK bundle matches a fresh build' : 'owner SDK bundle is STALE or missing');
        return same ? 0 : 1;
    }
    console.error('usage: node scripts/owner_sdk_build.mjs --write | --check');
    return 2;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    process.exitCode = await main(process.argv[2]);
}
