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
};
const PLAYWRIGHT_FLOOR = { file: 'owner_bootstrap.spec.js', projects: ['chromium', 'Mobile Chrome'], perProject: 6 };
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
        if (path.basename(spec.file) !== PLAYWRIGHT_FLOOR.file) continue;
        for (const t of spec.tests || []) pwCount[t.projectName] = (pwCount[t.projectName] || 0) + 1;
    }
    for (const s of suite.suites || []) walk(s);
};
for (const s of pw.suites || []) walk(s);
for (const p of PLAYWRIGHT_FLOOR.projects) {
    if ((pwCount[p] || 0) < PLAYWRIGHT_FLOOR.perProject) fail(`playwright: ${PLAYWRIGHT_FLOOR.file} has ${pwCount[p] || 0} tests in project ${p}, expected at least ${PLAYWRIGHT_FLOOR.perProject}`);
}

// 3. No skipped / focused / TODO owner tests, and no config that excludes them.
const ownerTestFiles = [...Object.keys(VITEST_FLOORS).map((f) => `frontend/tests/unit/${f}`), `tests/e2e/${PLAYWRIGHT_FLOOR.file}`];
for (const rel of ownerTestFiles) {
    const src = fs.readFileSync(path.join(REPO, rel), 'utf8');
    if (/\b(?:it|test|describe)\.(?:skip|only|todo|fixme|skipIf|runIf)\b|\btest\.fail\b/.test(src)) fail(`${rel}: contains skip/only/todo/fixme/conditional test markers`);
}
const vitestCfg = fs.readFileSync(path.join(REPO, 'vitest.config.js'), 'utf8');
if (/exclude:[^\]]*owner/s.test(vitestCfg)) fail('vitest.config.js excludes owner tests');

// 4. Vendored SDK equals a fresh deterministic build of the lockfile version.
try {
    execFileSync('node', ['scripts/owner_sdk_build.mjs', '--check'], { cwd: REPO, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
} catch (e) {
    fail(`owner SDK bundle/manifest do not match a fresh build (${String(e.stdout || e.message).trim()})`);
}

if (failures.length) {
    console.error(`OWNER CONTRACT GUARD FAILED:\n - ${failures.join('\n - ')}`);
    process.exit(1);
}
console.log(`owner contract guard OK: vitest ${JSON.stringify(Object.fromEntries(Object.keys(VITEST_FLOORS).map((f) => [f, perFile[f]])))}; playwright ${JSON.stringify(pwCount)}; SDK bundle matches fresh build`);
