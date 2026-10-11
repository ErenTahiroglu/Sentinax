// Phase 25D1C3: builds the isolated owner-bootstrap deployment artifact (dedicated Vercel project, Root Directory
// deploy/owner-bootstrap, build command `node ../../scripts/owner_deploy_build.mjs`, output directory `dist`).
//
// Only the allowlisted owner files are copied, byte for byte, into <outDir>/owner/. Nothing else from frontend/ (legacy SPA,
// Buffett app, service worker, config.js), backend/ or any data directory can enter the artifact. The build fails closed on a
// missing file, an SDK bundle that does not match its recorded manifest, external executable code or secret-looking material.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const DEFAULT_SOURCE = path.join(REPO, 'frontend/owner');
export const DEFAULT_OUT = path.join(REPO, 'deploy/owner-bootstrap/dist');
export const SDK_FILE = 'vendor/supabase-js.2.117.1.mjs';
export const MANIFEST_FILE = 'vendor/supabase-js.manifest.json';
export const OWNER_ALLOWLIST = Object.freeze([
    'index.html',
    'css/owner.css',
    'css/deep_ocean.css',
    'js/owner_app.js',
    'js/client_config.js',
    'js/owner_public_config.js',
    'js/mfa_flow.js',
    'js/private_api_config.js',
    'js/private_session.js',
    'js/private_shell.js',
    SDK_FILE,
]);

const FORBIDDEN = [
    [/<script[^>]+src\s*=\s*["']?(?:https?:)?\/\//i, 'external script'],
    [/\bfrom\s+['"](?:https?:)?\/\//, 'external module import'],
    [/\bimport\(\s*['"](?:https?:)?\/\//, 'external dynamic import'],
    [/serviceWorker\s*\.\s*register/, 'service worker registration'],
    [/importScripts\s*\(/, 'importScripts'],
    [/js\/core\/config\.js|\.\.\/js\/core/, 'legacy config reference'],
    [/sb_secret_[A-Za-z0-9_-]{16,}/, 'secret key'],
    [/service_role/i, 'service-role reference'],
    [/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}/, 'JWT-shaped token'],
    [/SUPABASE_JWT_SECRET|-----BEGIN [A-Z ]*PRIVATE KEY/, 'signing secret'],
    [/[0-9a-f]{8}-[0-9a-f]{4}-[1-9a-f][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i, 'UUID (user identifier)'],
];
const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

export async function buildOwnerDeploy({ outDir = DEFAULT_OUT, sourceDir = DEFAULT_SOURCE } = {}) {
    for (const rel of [...OWNER_ALLOWLIST, MANIFEST_FILE]) {
        if (!fs.existsSync(path.join(sourceDir, rel))) throw new Error(`missing required owner file: ${rel}`);
    }
    const manifest = JSON.parse(fs.readFileSync(path.join(sourceDir, MANIFEST_FILE), 'utf8'));
    const sdk = fs.readFileSync(path.join(sourceDir, SDK_FILE));
    if (sha256(sdk) !== manifest.sha256 || sdk.length !== manifest.bytes) {
        throw new Error('SDK bundle integrity check failed: does not match vendor/supabase-js.manifest.json');
    }
    const keys = new Set();
    for (const rel of OWNER_ALLOWLIST) {
        const text = fs.readFileSync(path.join(sourceDir, rel), 'utf8');
        for (const [re, what] of FORBIDDEN) if (re.test(text)) throw new Error(`forbidden content in ${rel}: ${what}`);
        for (const k of text.match(/sb_[a-z]+_[A-Za-z0-9_-]{16,}/g) || []) keys.add(k);
    }
    for (const k of keys) if (!k.startsWith('sb_publishable_')) throw new Error(`forbidden key class in artifact: ${k.slice(0, 8)}…`);

    fs.rmSync(outDir, { recursive: true, force: true });
    for (const rel of OWNER_ALLOWLIST) {
        const dest = path.join(outDir, 'owner', rel);
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.copyFileSync(path.join(sourceDir, rel), dest);
    }
    return { outDir, files: OWNER_ALLOWLIST.map((r) => `owner/${r}`) };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    buildOwnerDeploy().then(
        (r) => console.log(`owner deploy artifact: ${r.files.length} files -> ${path.relative(REPO, r.outDir)}`),
        (e) => { console.error(`OWNER DEPLOY BUILD FAILED: ${e.message}`); process.exit(1); },
    );
}
