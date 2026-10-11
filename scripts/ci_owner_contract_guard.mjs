// Phase 25D1C2 CI widening: fail CI if the owner-bootstrap security contracts are not actually discovered, are skipped,
// or the vendored SDK no longer equals a fresh build. It lists tests (it does not run them twice); `npm test` and
// `npm run test:e2e` run them. Floors are minimums: raising them is normal, lowering them needs review.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const VITEST_FLOORS = {
    'owner_mfa_flow.test.js': 35,
    'owner_mfa_races.test.js': 17,
    'owner_sdk_contract.test.js': 11,
    'owner_sdk_vendor.test.js': 4,
    'owner_selfcontained_static.test.js': 19,
    'owner_session_isolation_static.test.js': 8,
    'owner_static_boundaries.test.js': 5,
    'owner_deploy_artifact.test.js': 36, // Phase 25D1C3: isolated deployment artifact, routing, headers, build fail-closed, gitleaks scope
    'owner_cloudflare_artifact.test.js': 29, // Phase 25D1C5: Cloudflare Pages package (artifact, 404 vs SPA fallback, _headers, fail-closed build, no CF credentials in CI)
};
const PLAYWRIGHT_FLOORS = [
    { file: 'owner_bootstrap.spec.js', projects: ['chromium', 'Mobile Chrome'], perProject: 6 },
    { file: 'owner_deploy_artifact.spec.js', projects: ['chromium', 'Mobile Chrome'], perProject: 10 }, // 5 tests x (vercel legacy package + cloudflare package)
];
const failures = [];
const fail = (m) => failures.push(m);
const run = (args) => execFileSync('npx', args, { cwd: REPO, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });

// 1. Vitest discovery with per-file floors.
const listed = JSON.parse(run(['vitest', 'list', '--json']));
const perFile = {};
for (const t of listed) perFile[path.basename(t.file)] = (perFile[path.basename(t.file)] || 0) + 1;
for (const [file, floor] of Object.entries(VITEST_FLOORS)) {
    if ((perFile[file] || 0) < floor) fail(`vitest: ${file} discovered ${perFile[file] || 0} tests, expected at least ${floor}`);
}

// 2. Playwright discovery: the owner spec must run in both projects.
const pw = JSON.parse(run(['playwright', 'test', '--list', '--reporter=json']));
const pwCount = {};
const walk = (suite) => {
    for (const spec of suite.specs || []) {
        const name = path.basename(spec.file);
        for (const t of spec.tests || []) {
            const key = `${name}:${t.projectName}`;
            pwCount[key] = (pwCount[key] || 0) + 1;
        }
    }
    for (const s of suite.suites || []) walk(s);
};
for (const s of pw.suites || []) walk(s);
for (const { file, projects, perProject } of PLAYWRIGHT_FLOORS) {
    for (const p of projects) {
        const n = pwCount[`${file}:${p}`] || 0;
        if (n < perProject) fail(`playwright: ${file} has ${n} tests in project ${p}, expected at least ${perProject}`);
    }
}

// 3. No skipped / focused / TODO owner tests, and no config that excludes them.
const ownerTestFiles = [...Object.keys(VITEST_FLOORS).map((f) => `frontend/tests/unit/${f}`), ...PLAYWRIGHT_FLOORS.map((f) => `tests/e2e/${f.file}`)];
for (const rel of ownerTestFiles) {
    const src = fs.readFileSync(path.join(REPO, rel), 'utf8');
    if (/\b(?:it|test|describe)\.(?:skip|only|todo|fixme|skipIf|runIf)\b|\btest\.fail\b/.test(src)) fail(`${rel}: contains skip/only/todo/fixme/conditional test markers`);
}
const vitestCfg = fs.readFileSync(path.join(REPO, 'vitest.config.js'), 'utf8');
if (/exclude:[^\]]*owner/s.test(vitestCfg)) fail('vitest.config.js excludes owner tests');

// 4. Isolated deployment artifact: it must build, contain exactly the owner allowlist and nothing else, and be served by the
// two-server Playwright config (the e2e spec above needs the artifact server on port 3001).
try {
    execFileSync('node', ['scripts/owner_deploy_build.mjs'], { cwd: REPO, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const dist = path.join(REPO, 'deploy/owner-bootstrap/dist');
    const listAll = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listAll(path.join(d, e.name)) : [path.relative(dist, path.join(d, e.name))]));
    const expected = ['owner/css/owner.css', 'owner/index.html', 'owner/js/client_config.js', 'owner/js/mfa_flow.js', 'owner/js/owner_app.js', 'owner/js/owner_public_config.js', 'owner/vendor/supabase-js.2.117.1.mjs'];
    const actual = listAll(dist).sort();
    if (JSON.stringify(actual) !== JSON.stringify(expected)) fail(`deploy artifact files differ from the owner allowlist: ${JSON.stringify(actual)}`);
} catch (e) {
    fail(`owner deploy artifact build failed (${String(e.stderr || e.message).trim()})`);
}
// 4b. Cloudflare Pages package: exact allowlist (7 owner files + 404.html + _headers; never _redirects/Functions/Worker/index.html at root).
try {
    execFileSync('node', ['scripts/owner_cloudflare_build.mjs'], { cwd: REPO, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const cfDist = path.join(REPO, 'deploy/owner-cloudflare/dist');
    const listCf = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listCf(path.join(d, e.name)) : [path.relative(cfDist, path.join(d, e.name))]));
    const cfExpected = ['404.html', '_headers', 'owner/css/owner.css', 'owner/index.html', 'owner/js/client_config.js', 'owner/js/mfa_flow.js', 'owner/js/owner_app.js', 'owner/js/owner_public_config.js', 'owner/vendor/supabase-js.2.117.1.mjs'];
    const cfActual = listCf(cfDist).sort();
    if (JSON.stringify(cfActual) !== JSON.stringify(cfExpected)) fail(`cloudflare artifact files differ from the allowlist: ${JSON.stringify(cfActual)}`);
} catch (e) {
    fail(`owner cloudflare artifact build failed (${String(e.stderr || e.message).trim()})`);
}
for (const f of fs.readdirSync(path.join(REPO, '.github/workflows'))) {
    if (/CLOUDFLARE_(API_TOKEN|ACCOUNT_ID)|wrangler|pages-action|pages deploy/i.test(fs.readFileSync(path.join(REPO, '.github/workflows', f), 'utf8'))) fail(`${f}: Cloudflare credentials/deploy step are not allowed in CI`);
}
const pwCfg = fs.readFileSync(path.join(REPO, 'playwright.config.js'), 'utf8');
if (!pwCfg.includes('scripts/owner_deploy_serve.mjs') || !pwCfg.includes('127.0.0.1:3001')) fail('playwright.config.js does not start the owner artifact smoke server');
if (!pwCfg.includes('owner_deploy_serve.mjs cloudflare') || !pwCfg.includes('127.0.0.1:3002')) fail('playwright.config.js does not start the Cloudflare owner artifact smoke server');
const deployCfg = JSON.parse(fs.readFileSync(path.join(REPO, 'deploy/owner-bootstrap/vercel.json'), 'utf8'));
const hdr = Object.fromEntries((deployCfg.headers || []).flatMap((r) => r.headers.map((h) => [h.key.toLowerCase(), h.value])));
for (const k of ['content-security-policy', 'cache-control', 'x-frame-options', 'x-content-type-options', 'referrer-policy', 'cross-origin-opener-policy', 'cross-origin-resource-policy']) if (!hdr[k]) fail(`deploy vercel.json lacks header ${k}`);
if (/unsafe-inline|unsafe-eval|\*/.test(hdr['content-security-policy'] || '')) fail('deploy vercel.json CSP contains unsafe-inline/unsafe-eval/wildcard');
if ((deployCfg.rewrites || []).length || (deployCfg.routes || []).length) fail('deploy vercel.json declares rewrites/routes (fallbacks are forbidden)');

// 5. Vendored SDK equals a fresh deterministic build of the lockfile version.
try {
    execFileSync('node', ['scripts/owner_sdk_build.mjs', '--check'], { cwd: REPO, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
} catch (e) {
    fail(`owner SDK bundle/manifest do not match a fresh build (${String(e.stdout || e.message).trim()})`);
}

if (failures.length) {
    console.error(`OWNER CONTRACT GUARD FAILED:\n - ${failures.join('\n - ')}`);
    process.exit(1);
}
console.log(`owner contract guard OK: vitest ${JSON.stringify(Object.fromEntries(Object.keys(VITEST_FLOORS).map((f) => [f, perFile[f]])))}; playwright ${JSON.stringify(pwCount)}; deploy + cloudflare artifacts built and allowlisted; SDK bundle matches fresh build`);
