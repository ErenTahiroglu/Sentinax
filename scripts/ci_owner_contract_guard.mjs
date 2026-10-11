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
    'owner_deploy_artifact.test.js': 44, // Phase 25D1C3: isolated deployment artifact, routing, headers, build fail-closed, gitleaks scope
    'owner_private_session.test.js': 66, // Phase 25D1E3: private-session lifecycle (server authorization gate, stale-result suppression, identity change, revalidation)
    'owner_integrated_app_static.test.js': 24, // Phase 25D1E3: integrated document contract (one client, no storage/handoff, exact CSP, honest states, deployment isolation)
    'buffett_owner_entry.test.js': 58, // Phase 25D1E4: public "Sahip Girişi" entry (pinned-origin validation, no input/token, disabled when unconfigured)
    'owner_cloudflare_artifact.test.js': 33, // Phase 25D1C5: Cloudflare Pages package (artifact, 404 vs SPA fallback, _headers, fail-closed build, no CF credentials in CI)
};
const PLAYWRIGHT_FLOORS = [
    { file: 'owner_bootstrap.spec.js', projects: ['chromium', 'Mobile Chrome'], perProject: 6 },
    { file: 'owner_integrated_app.spec.js', projects: ['chromium', 'Mobile Chrome'], perProject: 14 }, // Phase 25D1E3: sign-in -> MFA -> server authorization -> Deep Ocean -> sign-out, failure modes, races
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
const ownerTestFiles = ['tests/connected/owner_connected_flow.spec.js', ...Object.keys(VITEST_FLOORS).map((f) => `frontend/tests/unit/${f}`), ...PLAYWRIGHT_FLOORS.map((f) => `tests/e2e/${f.file}`)];
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
    const expected = ['owner/css/deep_ocean.css', 'owner/css/owner.css', 'owner/index.html', 'owner/js/client_config.js', 'owner/js/mfa_flow.js', 'owner/js/owner_app.js', 'owner/js/owner_public_config.js', 'owner/js/private_api_config.js', 'owner/js/private_session.js', 'owner/js/private_shell.js', 'owner/vendor/supabase-js.2.117.1.mjs'];
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
    const cfExpected = ['404.html', '_headers', 'owner/css/deep_ocean.css', 'owner/css/owner.css', 'owner/index.html', 'owner/js/client_config.js', 'owner/js/mfa_flow.js', 'owner/js/owner_app.js', 'owner/js/owner_public_config.js', 'owner/js/private_api_config.js', 'owner/js/private_session.js', 'owner/js/private_shell.js', 'owner/vendor/supabase-js.2.117.1.mjs'];
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

// 4c. Phase 25D1E3 isolation checks that must hold even if a test file were weakened: legacy project roots exclude the owner tree, exactly one Supabase client exists,
// and no owner module outside owner_app.js touches the SDK or the network.
const ign = (f) => (fs.existsSync(path.join(REPO, f)) ? fs.readFileSync(path.join(REPO, f), 'utf8').split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#')) : []);
if (!ign('.vercelignore').includes('frontend/owner/') || !ign('frontend/.vercelignore').includes('owner/')) fail('legacy Vercel roots do not exclude the owner tree (.vercelignore)');
{
    const ownerJs = fs.readdirSync(path.join(REPO, 'frontend/owner/js')).filter((f) => f.endsWith('.js'));
    const clients = ownerJs.reduce((n, f) => n + (fs.readFileSync(path.join(REPO, 'frontend/owner/js', f), 'utf8').match(/createClient\(/g) || []).length, 0);
    if (clients !== 1) fail(`exactly one Supabase client is allowed in the owner tree, found ${clients}`);
    for (const f of ['private_session.js', 'private_shell.js', 'private_api_config.js']) {
        const src = fs.readFileSync(path.join(REPO, 'frontend/owner/js', f), 'utf8');
        if (/localStorage|sessionStorage|indexedDB|document\.cookie|postMessage|BroadcastChannel|createClient|\bfetch\(/.test(src)) fail(`${f} uses storage, messaging, the SDK or the network directly`);
    }
}

// 4d. Phase 25D1E4: the CONNECTED suite (real browser -> real FastAPI) must be discovered in both projects (it runs in the backend job, which has Python), its config must keep
// retries off and start the real-API harness, the harness must stay out of production code, and the public entry must not have become a second login.
{
    const connected = JSON.parse(run(['playwright', 'test', '-c', 'playwright.connected.config.js', '--list', '--reporter=json']));
    const cc = {};
    const walkC = (suite) => {
        for (const spec of suite.specs || []) for (const t of spec.tests || []) cc[t.projectName] = (cc[t.projectName] || 0) + 1;
        for (const sub of suite.suites || []) walkC(sub);
    };
    for (const sub of connected.suites || []) walkC(sub);
    for (const p of ['chromium', 'Mobile Chrome']) if ((cc[p] || 0) < 14) fail(`connected suite: ${cc[p] || 0} tests discovered in project ${p}, expected at least 14`);
    const cfgC = fs.readFileSync(path.join(REPO, 'playwright.connected.config.js'), 'utf8');
    if (!/retries:\s*0/.test(cfgC) || !cfgC.includes('backend.tests.e2e_harness.real_api_server') || !cfgC.includes('SENTINAX_TEST_API_ORIGIN')) fail('playwright.connected.config.js must keep retries:0 and start the real FastAPI harness with the forwarder');
    const specC = fs.readFileSync(path.join(REPO, 'tests/connected/owner_connected_flow.spec.js'), 'utf8');
    if (!specC.includes('x-correlation-id') || !specC.includes("/private/portfolio/summary") || !/foreign user/.test(specC)) fail('connected spec no longer asserts that the real backend answered (correlation id) / the foreign-user case');
    const walkPy = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.name === 'tests' || e.name === '__pycache__' ? [] : e.isDirectory() ? walkPy(path.join(d, e.name)) : [path.join(d, e.name)]));
    for (const f of walkPy(path.join(REPO, 'backend')).filter((x) => x.endsWith('.py'))) {
        if (/e2e_harness|__harness|SENTINAX_HARNESS|SENTINAX_TEST_API_ORIGIN/.test(fs.readFileSync(f, 'utf8'))) fail(`${path.relative(REPO, f)}: production backend code must not reference the test harness`);
    }
    const buf = fs.readFileSync(path.join(REPO, 'frontend/buffett/index.html'), 'utf8');
    if (/<(form|input)\b|supabase|createClient/i.test(buf)) fail('public Buffett page must not contain a form, input or authentication code');
}

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
